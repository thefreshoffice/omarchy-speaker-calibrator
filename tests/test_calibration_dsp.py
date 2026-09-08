#!/usr/bin/python3

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy import signal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration_dsp import (  # noqa: E402
    LEVEL_SEARCH_ABORT_STATUSES,
    LevelSearchPolicy,
    SweepSpec,
    analyse_capture,
    analyse_level_probe,
    build_measurement_signal,
    combine_microphone_measurements,
    level_search_advice,
    parse_mic_calibration,
    plan_probe_level,
    search_measurement_level,
)


class CalibrationDspTests(unittest.TestCase):
    def setUp(self):
        self.spec = SweepSpec(
            rate=8_000,
            start_hz=100.0,
            end_hz=3_000.0,
            seconds=0.75,
            level_dbfs=-20.0,
            repeats=3,
            pre_silence=0.15,
            block_gap=0.12,
            response_tail=0.08,
        )

    def synthetic_capture(self, *, clip=False, drift_ratio=None):
        program, schedule = build_measurement_signal(self.spec)
        mono = np.sum(program, axis=1)
        impulse = np.zeros(90)
        impulse[32] = 0.72
        impulse[46] = 0.13
        response = signal.fftconvolve(mono, impulse)
        lead = np.zeros(int(0.25 * self.spec.rate))
        capture = np.concatenate((lead, response, np.zeros(self.spec.rate // 2)))
        if drift_ratio is not None:
            numerator = int(round(drift_ratio * 10_000))
            capture = signal.resample_poly(capture, numerator, 10_000)
        rng = np.random.default_rng(42)
        capture += rng.normal(0.0, 2e-5, capture.size)
        if clip:
            capture = np.clip(capture * 30.0, -1.0, 1.0)
        return capture, schedule

    def test_signal_alternates_left_and_right_three_times(self):
        program, schedule = build_measurement_signal(self.spec)
        self.assertEqual(len(schedule), 6)
        self.assertEqual([event["output_channel"] for event in schedule], [0, 1, 0, 1, 0, 1])
        self.assertGreater(np.max(np.abs(program[:, 0])), 0.0)
        self.assertGreater(np.max(np.abs(program[:, 1])), 0.0)

    def test_clean_repeated_sweeps_pass_quality_gate(self):
        capture, schedule = self.synthetic_capture()
        result = analyse_capture(
            capture,
            schedule,
            self.spec,
            record_lead_seconds=0.25,
            internal_mic=False,
            calibration={
                "path": "/synthetic/calibration.txt",
                "frequency_hz": [20.0, 1_000.0, 20_000.0],
                "correction_db": [0.0, 0.0, 0.0],
                "sensitivity_dbfs": None,
            },
        )
        self.assertTrue(result["quality"]["accepted"], result["quality"])
        self.assertEqual(len(result["channels"]), 2)
        self.assertEqual(len(result["channels"][0]["sweeps"]), 3)
        self.assertEqual(len(result["validation_curves"]), 6)
        self.assertLess(result["quality"]["metrics"]["worst_repeatability_db"], 0.5)

    def test_clock_drift_is_estimated_and_corrected(self):
        capture, schedule = self.synthetic_capture(drift_ratio=1.001)
        result = analyse_capture(
            capture,
            schedule,
            self.spec,
            record_lead_seconds=0.25,
            internal_mic=True,
        )
        ppm = result["quality"]["metrics"]["clock_drift_ppm"]
        self.assertGreater(ppm, 600.0)
        self.assertLess(ppm, 1_400.0)

    def test_clipped_capture_is_not_installable(self):
        capture, schedule = self.synthetic_capture(clip=True)
        result = analyse_capture(
            capture,
            schedule,
            self.spec,
            record_lead_seconds=0.25,
            internal_mic=True,
        )
        self.assertFalse(result["quality"]["accepted"])
        self.assertGreater(result["quality"]["metrics"]["clipped_samples"], 0)

    def test_one_clipped_repeat_is_discarded_when_two_clean_repeats_remain(self):
        capture, schedule = self.synthetic_capture()
        event = next(item for item in schedule
                     if item["output_channel"] == 0 and item["repeat"] == 2)
        start = int(0.25 * self.spec.rate) + event["start_frame"] + 80
        capture[start:start + 8] = 1.0
        result = analyse_capture(
            capture,
            schedule,
            self.spec,
            record_lead_seconds=0.25,
            internal_mic=True,
        )
        self.assertTrue(result["quality"]["accepted"], result["quality"])
        self.assertEqual(result["channels"][0]["excluded_repeats"], [2])
        self.assertEqual(result["quality"]["metrics"]["clipped_samples"], 0)

    def test_scalar_builtin_mic_gain_movement_is_warning_not_failure(self):
        capture, schedule = self.synthetic_capture()
        lead = int(0.25 * self.spec.rate)
        block = self.spec.frames + int(self.spec.response_tail * self.spec.rate)
        scales = {0: 1.0, 1: 0.75, 2: 0.55}
        for event in schedule:
            start = lead + event["start_frame"]
            capture[start:start + block] *= scales[event["repeat"]]
        result = analyse_capture(
            capture,
            schedule,
            self.spec,
            record_lead_seconds=0.25,
            internal_mic=True,
        )
        self.assertTrue(result["quality"]["accepted"], result["quality"])
        self.assertGreater(result["quality"]["metrics"]["worst_gain_stability_db"], 4.0)
        self.assertTrue(any(
            "repeat shape" in warning for warning in result["quality"]["warnings"]
        ))

    def test_one_contaminated_repeat_is_discarded(self):
        capture, schedule = self.synthetic_capture()
        event = next(item for item in schedule
                     if item["output_channel"] == 0 and item["repeat"] == 2)
        lead = int(0.25 * self.spec.rate)
        start = lead + event["start_frame"]
        rng = np.random.default_rng(91)
        capture[start:start + self.spec.frames] += rng.normal(
            0.0, 0.15, self.spec.frames
        )
        result = analyse_capture(
            capture,
            schedule,
            self.spec,
            record_lead_seconds=0.25,
            internal_mic=True,
        )
        left = result["channels"][0]
        self.assertTrue(result["quality"]["accepted"], result["quality"])
        self.assertEqual(left["excluded_repeats"], [2])
        self.assertGreaterEqual(left["stable_band_percent"], 90.0)

    def test_common_microphone_calibration_text_is_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mic.txt"
            path.write_text(
                '"Sens Factor = -18.3dBFS"\n20 1.20 0\n1000 -0.15 0\n20000 2.4 0\n'
            )
            result = parse_mic_calibration(path)
        self.assertEqual(result["frequency_hz"], [20.0, 1000.0, 20000.0])
        self.assertEqual(result["correction_db"], [1.2, -0.15, 2.4])
        self.assertEqual(result["sensitivity_dbfs"], -18.3)

    def clean_measurement(self):
        capture, schedule = self.synthetic_capture()
        return analyse_capture(
            capture,
            schedule,
            self.spec,
            record_lead_seconds=0.25,
            internal_mic=True,
        )

    @staticmethod
    def add_response_shape(measurement, shape):
        measurement["level_dbfs"] = (
            np.asarray(measurement["level_dbfs"]) + shape
        ).tolist()
        for channel in measurement["channels"]:
            channel["response_db"] = (
                np.asarray(channel["response_db"]) + shape
            ).tolist()
        for curve in measurement.get("validation_curves", []):
            curve["response_db"] = (
                np.asarray(curve["response_db"]) + shape
            ).tolist()

    def test_two_microphones_are_combined_after_independent_analysis(self):
        first = self.clean_measurement()
        second = copy.deepcopy(first)
        frequencies = np.asarray(first["frequency_hz"])
        shape = 6.0 + 2.0 * np.log2(frequencies / 1000.0)
        self.add_response_shape(second, shape)
        combined = combine_microphone_measurements([first, second], [0, 1])
        array = combined["microphone_array"]
        json.dumps(combined)
        self.assertTrue(combined["quality"]["accepted"])
        self.assertEqual(array["used_channels"], [0, 1])
        self.assertFalse(array["raw_waveforms_mixed"])
        self.assertEqual(len(combined["validation_curves"]), 12)
        self.assertEqual(
            {curve["input_channel"] for curve in combined["validation_curves"]},
            {0, 1},
        )
        low = np.argmin(np.abs(frequencies - 250.0))
        middle = np.argmin(np.abs(frequencies - 1000.0))
        high = np.argmin(np.abs(frequencies - 2000.0))
        uncertainty = np.asarray(combined["channels"][0]["uncertainty_db"])
        self.assertGreater(
            max(uncertainty[low], uncertainty[high]), uncertainty[middle] + 1.0
        )

    def test_failed_microphone_is_ignored_when_another_channel_is_reliable(self):
        reliable = self.clean_measurement()
        failed = copy.deepcopy(reliable)
        failed["quality"]["accepted"] = False
        failed["quality"]["verdict"] = "fail"
        failed["quality"]["failures"] = ["Synthetic microphone failure."]
        combined = combine_microphone_measurements([reliable, failed], [0, 1])
        self.assertTrue(combined["quality"]["accepted"])
        self.assertEqual(combined["microphone_array"]["used_channels"], [0])
        self.assertEqual(
            combined["quality"]["metrics"]["microphone_channels_rejected"], 1
        )

    def test_clear_response_outlier_is_rejected_when_three_mics_exist(self):
        first = self.clean_measurement()
        second = copy.deepcopy(first)
        outlier = copy.deepcopy(first)
        frequencies = np.asarray(first["frequency_hz"])
        shape = np.where(frequencies < 700.0, -7.0, 7.0)
        self.add_response_shape(outlier, shape)
        combined = combine_microphone_measurements(
            [first, second, outlier], [0, 1, 2]
        )
        self.assertEqual(combined["microphone_array"]["used_channels"], [0, 1])
        self.assertEqual(
            combined["microphone_array"]["rejected_channels"][0]["input_channel"], 2
        )


class LevelSearchTests(unittest.TestCase):
    rate = 8_000

    def probe_capture(self, amplitude, *, noise=1e-4, channels=1, clip=False,
                      click=0.0, background=0.0):
        """0.3 s of room sound, a 0.5 s tone burst, and 0.4 s of room sound."""
        rng = np.random.default_rng(7)
        frames = int(1.2 * self.rate)
        capture = rng.normal(0.0, noise, (frames, channels))
        if background:
            capture += rng.normal(0.0, background, (frames, channels))
        t = np.arange(int(0.5 * self.rate)) / self.rate
        burst = amplitude * np.sin(2.0 * np.pi * 440.0 * t)
        start = int(0.4 * self.rate)
        capture[start:start + burst.size, 0] += burst
        if click:
            at = int(1.0 * self.rate)
            capture[at:at + 8, 0] += click
        if clip:
            capture = np.clip(capture, -1.0, 1.0)
        return capture

    def test_probe_reports_peak_noise_prominence_and_background(self):
        probe = analyse_level_probe(self.probe_capture(0.3), self.rate)
        self.assertAlmostEqual(probe["peak_dbfs"], 20 * np.log10(0.3), delta=0.2)
        self.assertLess(probe["noise_dbfs"], -70.0)
        self.assertLess(probe["background_rms_dbfs"], -70.0)
        self.assertGreater(probe["prominence_db"], 40.0)
        self.assertGreaterEqual(probe["tonal_blocks"], 8)
        self.assertEqual(probe["clipped_samples"], 0)

    def test_probe_counts_clipping_on_any_channel(self):
        capture = self.probe_capture(1.4, channels=2, clip=True)
        probe = analyse_level_probe(capture, self.rate)
        self.assertGreater(probe["clipped_samples"], 0)
        self.assertAlmostEqual(probe["peak_dbfs"], 0.0, delta=0.1)

    def test_probe_uses_best_channel_for_prominence(self):
        capture = self.probe_capture(0.1, channels=2)
        probe = analyse_level_probe(capture, self.rate)
        self.assertGreater(probe["prominence_db"], 30.0)

    def test_probe_ignores_an_isolated_click_for_the_sweep_peak(self):
        probe = analyse_level_probe(self.probe_capture(0.1, click=0.9), self.rate)
        self.assertAlmostEqual(probe["peak_dbfs"], -20.0, delta=0.3)
        self.assertGreater(probe["transient_peak_dbfs"], -2.0)

    def test_probe_measures_room_sound_before_the_probe(self):
        probe = analyse_level_probe(self.probe_capture(0.3, background=0.05), self.rate)
        self.assertAlmostEqual(probe["background_rms_dbfs"], -26.0, delta=1.0)

    def test_silence_has_no_prominence(self):
        probe = analyse_level_probe(self.probe_capture(0.0), self.rate)
        self.assertLess(probe["prominence_db"], 3.0)

    def test_plan_raises_a_quiet_probe_proportionally(self):
        probe = {"peak_dbfs": -32.0, "noise_dbfs": -80.0, "prominence_db": 40.0, "clipped_samples": 0}
        plan = plan_probe_level(-24.0, probe, (-36.0, -6.0))
        self.assertFalse(plan["done"])
        self.assertEqual(plan["status"], "adjusting")
        # -24 + (-6 - -32) = +2 dBFS, clamped to the upper bound.
        self.assertAlmostEqual(plan["level_dbfs"], -6.0)

    def test_plan_backs_off_after_clipping(self):
        probe = {"peak_dbfs": 0.0, "noise_dbfs": -80.0, "prominence_db": 60.0, "clipped_samples": 12}
        plan = plan_probe_level(-24.0, probe, (-36.0, -6.0))
        self.assertFalse(plan["done"])
        self.assertEqual(plan["status"], "clipped")
        self.assertAlmostEqual(plan["level_dbfs"], -36.0)

    def test_plan_converges_near_target(self):
        probe = {"peak_dbfs": -7.0, "noise_dbfs": -80.0, "prominence_db": 50.0, "clipped_samples": 0}
        plan = plan_probe_level(-14.0, probe, (-36.0, -6.0))
        self.assertTrue(plan["done"])
        self.assertEqual(plan["status"], "converged")
        self.assertAlmostEqual(plan["level_dbfs"], -13.0)

    def test_plan_reports_a_bound_that_stops_it_short(self):
        quiet = {"peak_dbfs": -20.0, "noise_dbfs": -80.0, "prominence_db": 40.0, "clipped_samples": 0}
        plan = plan_probe_level(-6.0, quiet, (-36.0, -6.0))
        self.assertTrue(plan["done"])
        self.assertEqual(plan["status"], "limited-by-maximum-level")
        hot = {"peak_dbfs": -1.0, "noise_dbfs": -80.0, "prominence_db": 60.0, "clipped_samples": 0}
        plan = plan_probe_level(-36.0, hot, (-36.0, -6.0))
        self.assertTrue(plan["done"])
        self.assertEqual(plan["status"], "limited-by-minimum-level")

    def test_plan_steps_up_blindly_when_nothing_is_heard(self):
        silent = {"peak_dbfs": -70.0, "noise_dbfs": -72.0, "prominence_db": 1.0, "clipped_samples": 0}
        plan = plan_probe_level(-24.0, silent, (-36.0, -6.0))
        self.assertFalse(plan["done"])
        self.assertEqual(plan["status"], "not-heard")
        self.assertAlmostEqual(plan["level_dbfs"], -12.0)
        plan = plan_probe_level(-6.0, silent, (-36.0, -6.0))
        self.assertTrue(plan["done"])
        self.assertEqual(plan["status"], "no-signal")

    def test_plan_refuses_a_loud_room_before_anything_else(self):
        loud = {"peak_dbfs": 0.0, "noise_dbfs": -30.0, "prominence_db": 10.0,
                "clipped_samples": 3, "background_rms_dbfs": -25.0}
        plan = plan_probe_level(-24.0, loud, (-36.0, -6.0))
        self.assertTrue(plan["done"])
        self.assertEqual(plan["status"], "background-too-loud")
        self.assertAlmostEqual(plan["level_dbfs"], -24.0)

    def fake_microphone(self, gain_db, *, noise_dbfs=-75.0, background_dbfs=-70.0, agc_peak=None):
        """A linear speaker/microphone path that clips at full scale."""
        def run_probe(level_dbfs):
            peak = min(0.0, level_dbfs + gain_db)
            if agc_peak is not None:
                peak = agc_peak
            return {
                "peak_dbfs": peak,
                "noise_dbfs": noise_dbfs,
                "prominence_db": max(0.0, peak - 3.0 - noise_dbfs),
                "clipped_samples": 5 if level_dbfs + gain_db >= 0.0 else 0,
                "background_rms_dbfs": background_dbfs,
                "background_peak_dbfs": background_dbfs + 10.0,
                "transient_peak_dbfs": -120.0,
                "tonal_blocks": 16,
            }
        return run_probe

    def test_search_lands_on_target_in_two_probes(self):
        search = search_measurement_level(
            self.fake_microphone(8.0), start_level_dbfs=-24.0, bounds=(-36.0, -6.0)
        )
        self.assertEqual(search["status"], "converged")
        self.assertTrue(search["confirmed"])
        self.assertEqual(len(search["attempts"]), 2)
        self.assertAlmostEqual(search["selected_level_dbfs"], -14.0)
        self.assertEqual(search["attempts"][0]["status"], "adjusting")
        self.assertEqual(search["attempts"][1]["status"], "converged")

    def test_search_retreats_from_a_hot_microphone(self):
        search = search_measurement_level(
            self.fake_microphone(40.0), start_level_dbfs=-24.0, bounds=(-36.0, -6.0)
        )
        self.assertEqual(search["status"], "clipping-at-minimum-level")
        self.assertAlmostEqual(search["selected_level_dbfs"], -36.0)
        warnings, guidance = level_search_advice(search)
        self.assertTrue(any("quietest" in item for item in warnings))
        self.assertTrue(any("Lower" in item for item in guidance))

    def test_search_stops_at_the_maximum_for_a_quiet_microphone(self):
        search = search_measurement_level(
            self.fake_microphone(-20.0), start_level_dbfs=-24.0, bounds=(-36.0, -6.0)
        )
        self.assertEqual(search["status"], "limited-by-maximum-level")
        self.assertTrue(search["confirmed"])
        self.assertAlmostEqual(search["selected_level_dbfs"], -6.0)
        warnings, guidance = level_search_advice(search)
        self.assertTrue(any("loudest" in item for item in warnings))
        self.assertTrue(any("Raise" in item for item in guidance))

    def test_search_reports_no_signal_from_a_dead_path(self):
        search = search_measurement_level(
            self.fake_microphone(-200.0), start_level_dbfs=-24.0, bounds=(-36.0, -6.0)
        )
        self.assertEqual(search["status"], "no-signal")
        self.assertFalse(search["confirmed"])
        self.assertIn(search["status"], LEVEL_SEARCH_ABORT_STATUSES)
        self.assertEqual([item["status"] for item in search["attempts"]], ["not-heard", "not-heard", "no-signal"])

    def test_search_stops_when_the_peak_ignores_the_level(self):
        # An automatic-gain microphone (or other audio) pins the peak at -1 dBFS
        # no matter how quiet the sweep is; the search must not chase it down.
        search = search_measurement_level(
            self.fake_microphone(8.0, agc_peak=-1.0), start_level_dbfs=-24.0, bounds=(-36.0, -6.0)
        )
        self.assertEqual(search["status"], "level-independent")
        self.assertIn(search["status"], LEVEL_SEARCH_ABORT_STATUSES)
        self.assertEqual(len(search["attempts"]), 2)
        self.assertAlmostEqual(search["selected_level_dbfs"], -29.0)
        warnings, guidance = level_search_advice(search)
        self.assertTrue(any("did not follow" in item for item in warnings))
        self.assertTrue(any("automatic gain control" in item for item in guidance))

    def test_search_stops_when_the_room_is_loud(self):
        search = search_measurement_level(
            self.fake_microphone(8.0, background_dbfs=-22.0), start_level_dbfs=-24.0, bounds=(-36.0, -6.0)
        )
        self.assertEqual(search["status"], "background-too-loud")
        self.assertEqual(len(search["attempts"]), 1)
        warnings, guidance = level_search_advice(search)
        self.assertTrue(any("-22.0 dBFS" in item for item in warnings))
        self.assertTrue(any("Pause other audio" in item for item in guidance))

    def test_search_honours_a_custom_policy(self):
        policy = LevelSearchPolicy(target_peak_dbfs=-12.0)
        search = search_measurement_level(
            self.fake_microphone(8.0), start_level_dbfs=-24.0, bounds=(-36.0, -6.0),
            policy=policy,
        )
        self.assertEqual(search["status"], "converged")
        self.assertAlmostEqual(search["selected_level_dbfs"], -20.0)
        self.assertEqual(search["target_peak_dbfs"], -12.0)

    def test_settled_search_has_no_advice(self):
        search = search_measurement_level(
            self.fake_microphone(8.0), start_level_dbfs=-24.0, bounds=(-36.0, -6.0)
        )
        self.assertEqual(level_search_advice(search), ([], []))

if __name__ == "__main__":
    unittest.main()
