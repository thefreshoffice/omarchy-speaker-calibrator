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
    SweepSpec,
    analyse_capture,
    build_measurement_signal,
    combine_microphone_measurements,
    parse_mic_calibration,
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


if __name__ == "__main__":
    unittest.main()
