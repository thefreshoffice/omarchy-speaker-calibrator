#!/usr/bin/python3

import sys
import unittest
import importlib.util
import tempfile
import json
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration_optimizer import (  # noqa: E402
    HIGHPASS_BOUNDS_HZ,
    MAKEUP_CAP_DB,
    estimate_highpass,
    loudness_makeup_db,
    pink_loudness_db,
    pleasant_in_room_target,
    optimize_peq,
)

_helper_spec = importlib.util.spec_from_file_location(
    "speaker_calibrate", Path(__file__).resolve().parents[1] / "speaker-calibrate.py"
)
speaker_calibrate = importlib.util.module_from_spec(_helper_spec)
_helper_spec.loader.exec_module(speaker_calibrate)


class CalibrationOptimizerTests(unittest.TestCase):
    def setUp(self):
        self.frequencies = np.geomspace(80.0, 16_000.0, 240)

    def measurement(
        self, response, *, uncertainty=0.2, calibrated=False, repeat_curves=None
    ):
        response = np.asarray(response, dtype=float)
        right = response + 0.08
        uncertainty_curve = np.full(response.size, uncertainty)
        measurement = {
            "frequency_hz": self.frequencies.tolist(),
            "level_dbfs": response.tolist(),
            "rate_hz": 48_000,
            "microphone_calibration": {"path": "/synthetic/mic.txt"}
            if calibrated else None,
            "channels": [
                {
                    "output_channel": "left",
                    "response_db": response.tolist(),
                    "uncertainty_db": uncertainty_curve.tolist(),
                },
                {
                    "output_channel": "right",
                    "response_db": right.tolist(),
                    "uncertainty_db": uncertainty_curve.tolist(),
                },
            ],
        }
        if repeat_curves is not None:
            measurement["validation_curves"] = [
                {
                    "repeat": repeat,
                    "output_channel": "left",
                    "response_db": np.asarray(curve, dtype=float).tolist(),
                }
                for repeat, curve in enumerate(repeat_curves)
            ]
        return measurement

    def test_target_has_bass_rise_flat_midband_and_treble_decline(self):
        target = pleasant_in_room_target(self.frequencies, "neutral")
        at = lambda hz: float(np.interp(np.log(hz), np.log(self.frequencies), target))
        self.assertGreater(at(100), at(1000) + 2.0)
        self.assertAlmostEqual(at(500), at(1000), delta=0.05)
        self.assertLess(at(10_000), at(1000) - 3.0)

    def rolled_off(self, knee_hz, slope_db_per_octave=24.0):
        """A flat speaker that gives up below ``knee_hz``."""
        response = np.full(self.frequencies.size, -30.0)
        low = self.frequencies < knee_hz
        response[low] -= slope_db_per_octave * np.log2(knee_hz / self.frequencies[low])
        return response

    def test_highpass_corner_follows_the_measured_roll_off(self):
        target = np.zeros(self.frequencies.size)
        shallow = estimate_highpass(self.frequencies, self.rolled_off(150.0) + 30.0, target)
        steep = estimate_highpass(self.frequencies, self.rolled_off(300.0) + 30.0, target)
        self.assertLess(shallow["frequency_hz"], steep["frequency_hz"])
        # 24 dB/octave: 15 dB short a bit more than half an octave below the knee.
        self.assertAlmostEqual(shallow["frequency_hz"], 150.0 / 2 ** (15.0 / 24.0), delta=8.0)
        self.assertAlmostEqual(steep["frequency_hz"], 300.0 / 2 ** (15.0 / 24.0), delta=8.0)
        self.assertEqual(shallow["knee_hz"], shallow["frequency_hz"])
        self.assertEqual(steep["stages"], 1, "a corner this high must stay 2nd order")
        self.assertEqual(shallow["stages"], 2, "a low corner may use the steeper slope")

    def test_full_range_speaker_keeps_the_minimum_corner(self):
        flat = np.full(self.frequencies.size, -30.0)
        highpass = estimate_highpass(self.frequencies, flat, np.full(self.frequencies.size, -30.0))
        self.assertEqual(highpass["frequency_hz"], HIGHPASS_BOUNDS_HZ[0])
        self.assertIsNone(highpass["knee_hz"])
        self.assertEqual(highpass["stages"], 2)

    def test_corner_never_leaves_its_bounds(self):
        target = np.zeros(self.frequencies.size)
        hopeless = estimate_highpass(self.frequencies, self.rolled_off(2000.0) + 30.0, target)
        self.assertLessEqual(hopeless["frequency_hz"], HIGHPASS_BOUNDS_HZ[1])
        self.assertGreaterEqual(hopeless["frequency_hz"], HIGHPASS_BOUNDS_HZ[0])

    def test_optimizer_does_not_correct_its_own_highpass(self):
        result = optimize_peq(
            self.measurement(self.rolled_off(300.0)), "neutral", internal_mic=True
        )
        highpass = result["highpass"]
        self.assertGreater(highpass["frequency_hz"], 100.0)
        self.assertEqual(result["highpass_hz"], highpass["frequency_hz"])
        # The target rolls off with the filter instead of asking for the bass back.
        target = np.asarray(result["target"]["aligned_db"])
        at = lambda curve, hz: float(np.interp(np.log(hz), np.log(self.frequencies), curve))
        self.assertLess(at(target, highpass["frequency_hz"] / 4.0),
                        at(target, 1000.0) - 12.0)
        # No section tries to boost the removed region back.
        for item in result["filters"]:
            if item["frequency_hz"] <= highpass["frequency_hz"]:
                self.assertLessEqual(item["gain_db"], 0.0, item)

    def test_full_bass_adds_a_paid_for_shelf_at_the_knee(self):
        response = np.full(self.frequencies.size, -30.0)
        # A speaker that gives up below 400 Hz: 12 dB/octave roll-off.
        low = self.frequencies < 400.0
        response[low] -= 12.0 * np.log2(400.0 / self.frequencies[low])
        normal = optimize_peq(self.measurement(response), "neutral", internal_mic=True)
        full = optimize_peq(
            self.measurement(response), "neutral", internal_mic=True, bass="full"
        )
        self.assertIsNone(normal["bass_shelf"])
        shelf = full["bass_shelf"]
        self.assertEqual(shelf["gain_db"], 3.0)
        self.assertGreaterEqual(shelf["frequency_hz"], 150.0)
        self.assertLessEqual(shelf["frequency_hz"], 600.0)
        self.assertEqual(full["gains_db"], normal["gains_db"])
        at = lambda result, hz: float(np.interp(
            np.log(hz), np.log(self.frequencies), result["correction_response_db"]
        ))
        self.assertAlmostEqual(at(full, 100) - at(normal, 100), 3.0, delta=0.4)
        self.assertAlmostEqual(at(full, 5000) - at(normal, 5000), 0.0, delta=0.1)
        # The shelf is a positive correction, so headroom and input trim pay for
        # it.  The high-pass cancels the part of it that sits below the
        # speaker's limit, so what is paid for is only the audible part.
        self.assertGreater(full["headroom_db"], normal["headroom_db"])
        self.assertLess(full["input_gain_linear"], normal["input_gain_linear"])

    def test_rising_treble_is_handled_by_a_high_shelf(self):
        response = np.full(self.frequencies.size, -30.0)
        high = self.frequencies > 2000.0
        response[high] += 6.0 * np.log2(self.frequencies[high] / 2000.0)
        result = optimize_peq(self.measurement(response), "neutral", internal_mic=False)
        shelves = [item for item in result["filters"] if item["type"] == "highshelf"]
        self.assertEqual(len(shelves), 1, result["filters"])
        self.assertLess(shelves[0]["gain_db"], -2.0)
        self.assertLessEqual(result["filter_count"], 3)
        at = lambda hz: float(np.interp(
            np.log(hz), np.log(self.frequencies), result["correction_response_db"]
        ))
        self.assertLess(at(12_000), -4.0)
        self.assertGreater(at(1000), -1.5)

    def test_deep_hump_uses_one_deep_cut_within_the_total_limit(self):
        response = np.full(self.frequencies.size, -30.0)
        response += 14.0 * np.exp(-0.5 * (np.log2(self.frequencies / 1000.0) / 0.45) ** 2)
        result = optimize_peq(self.measurement(response), "neutral", internal_mic=True)
        deepest = min(result["gains_db"])
        self.assertLess(deepest, -6.5)
        self.assertGreaterEqual(deepest, -12.0)
        self.assertLessEqual(result["filter_count"], 4, result["filters"])
        # Sections may share a hump, but they must not pile onto one frequency.
        cuts = [item["frequency_hz"] for item in result["filters"]
                if item["type"] == "peaking" and item["gain_db"] < -1.0]
        spacing = [abs(np.log2(a / b)) for i, a in enumerate(cuts) for b in cuts[i + 1:]]
        if spacing:
            self.assertGreaterEqual(min(spacing), 0.25, result["filters"])
        limit = np.asarray(result["total_cut_limit_db"])
        correction = np.asarray(result["correction_response_db"])
        self.assertGreaterEqual(float(np.min(correction - limit)), -0.5)
        self.assertLess(result["weighted_rmse_after_db"], result["weighted_rmse_before_db"] / 2.0)

    def test_graph_routes_shelves_and_the_bass_shelf_to_their_slots(self):
        fit = {
            "filters": [
                {"type": "lowshelf", "frequency_hz": 300, "q": 0.7, "gain_db": -3.0},
                {"type": "peaking", "frequency_hz": 1000, "q": 1.0, "gain_db": -2.5},
                {"type": "highshelf", "frequency_hz": 4000, "q": 0.8, "gain_db": -4.0},
            ],
            "bass_shelf": {"frequency_hz": 500, "q": 0.707, "gain_db": 3.0},
            "input_gain_linear": 0.5,
        }
        controls = speaker_calibrate.graph_controls(fit)
        self.assertEqual(controls["ls_l:Freq"], 300.0)
        self.assertEqual(controls["ls_r:Gain"], -3.0)
        self.assertEqual(controls["bs_l:Freq"], 500.0)
        self.assertEqual(controls["bs_r:Gain"], 3.0)
        self.assertEqual(controls["p1_l:Freq"], 1000.0)
        self.assertEqual(controls["p2_l:Gain"], 0.0)
        self.assertEqual(controls["hs_l:Freq"], 4000.0)
        self.assertEqual(controls["hs_r:Gain"], -4.0)
        graph = speaker_calibrate.filter_config("alsa_output.synthetic", fit)
        self.assertIn('name = bs_l label = bq_lowshelf control = { "Freq" = 500 "Q" = 0.707 "Gain" = 3 }', graph)
        # A 0.10.0 profile stored the bass shelf as low_shelf.
        legacy = {"centers_hz": [1000], "q": [1.0], "gains_db": [-2.5],
                  "low_shelf": {"frequency_hz": 500, "q": 0.707, "gain_db": 3.0},
                  "input_gain_linear": 0.5}
        legacy_controls = speaker_calibrate.graph_controls(legacy)
        self.assertEqual(legacy_controls["bs_l:Gain"], 3.0)
        self.assertEqual(legacy_controls["ls_l:Gain"], 0.0)

    def test_loudness_modes_pay_back_a_capped_share_of_the_loss(self):
        self.assertEqual(loudness_makeup_db(4.0, "protected"), 0.0)
        self.assertEqual(loudness_makeup_db(4.0, "balanced"), 2.0)
        self.assertEqual(loudness_makeup_db(4.0, "matched"), 4.0)
        self.assertEqual(loudness_makeup_db(9.0, "matched"), MAKEUP_CAP_DB)
        self.assertEqual(loudness_makeup_db(9.0, "balanced"), MAKEUP_CAP_DB / 2.0)
        self.assertEqual(loudness_makeup_db(-3.0, "matched"), 0.0)
        flat = np.zeros(self.frequencies.size)
        cut = flat.copy()
        cut[(self.frequencies > 600) & (self.frequencies < 1500)] -= 10.0
        self.assertGreater(
            pink_loudness_db(self.frequencies, flat) - pink_loudness_db(self.frequencies, cut),
            1.0,
        )

    def test_matched_loudness_raises_input_gain_by_the_estimated_loss(self):
        # A loud hump the optimizer will cut, so the corrected speaker loses loudness.
        response = np.full(self.frequencies.size, -30.0)
        response[(self.frequencies > 600) & (self.frequencies < 1500)] += 9.0
        protected = optimize_peq(self.measurement(response), "neutral", internal_mic=True)
        matched = optimize_peq(
            self.measurement(response), "neutral", internal_mic=True, loudness="matched"
        )
        self.assertEqual(protected["makeup_db"], 0.0)
        self.assertGreater(matched["loudness_loss_db"], 0.5)
        self.assertEqual(matched["makeup_db"], min(MAKEUP_CAP_DB, matched["loudness_loss_db"]))
        self.assertEqual(matched["gains_db"], protected["gains_db"])
        expected = 10.0 ** ((matched["makeup_db"] - matched["headroom_db"]) / 20.0)
        self.assertAlmostEqual(matched["input_gain_linear"], expected, places=5)
        self.assertGreater(matched["input_gain_linear"], protected["input_gain_linear"])

    def test_flat_response_is_solved_with_cuts_not_boosts(self):
        response = np.full(self.frequencies.size, -30.0)
        result = optimize_peq(
            self.measurement(response), "neutral", internal_mic=True
        )
        self.assertEqual(result["actual_maximum_boost_db"], 0.0)
        self.assertTrue(any(gain < -0.5 for gain in result["gains_db"]))
        self.assertLess(result["weighted_rmse_after_db"], result["weighted_rmse_before_db"])
        self.assertTrue(all(
            gain >= limit for gain, limit
            in zip(result["gains_db"], result["cut_limits_db"])
        ))

    def test_uncertain_dip_is_never_boosted(self):
        response = -30.0 - 7.0 * np.exp(
            -0.5 * (np.log2(self.frequencies / 1600.0) / 0.3) ** 2
        )
        result = optimize_peq(
            self.measurement(response, uncertainty=2.5, calibrated=True),
            "neutral",
            internal_mic=False,
        )
        self.assertEqual(result["actual_maximum_boost_db"], 0.0)
        decision = next(
            item for item in result["boost_decisions"]
            if item["center_hz"] == 1600
        )
        self.assertFalse(decision["permitted"])

    def test_reliable_broad_deficit_allows_only_a_bounded_boost(self):
        response = -30.0 - 5.0 * np.exp(
            -0.5 * (np.log2(self.frequencies / 1600.0) / 0.3) ** 2
        )
        result = optimize_peq(
            self.measurement(response, calibrated=True),
            "neutral",
            internal_mic=False,
        )
        positive = [
            item for item in result["filters"] if item["gain_db"] > 0.0
        ]
        self.assertEqual(len(positive), 1)
        boost_filter = positive[0]
        boost = boost_filter["gain_db"]
        self.assertAlmostEqual(boost_filter["frequency_hz"], 1600.0, delta=250.0)
        self.assertGreater(boost, 0.5)
        self.assertLessEqual(boost, result["maximum_allowed_boost_db"])

    def test_input_trim_covers_positive_filter_peak_plus_limiter_margin(self):
        response = -30.0 - 5.0 * np.exp(
            -0.5 * (np.log2(self.frequencies / 1600.0) / 0.3) ** 2
        )
        result = optimize_peq(
            self.measurement(response, calibrated=True),
            "neutral",
            internal_mic=False,
        )
        correction_peak = max(0.0, max(result["correction_response_db"]))
        self.assertGreaterEqual(result["headroom_db"] + 0.011, correction_peak + 1.0)
        expected_gain = 10.0 ** (-result["headroom_db"] / 20.0)
        self.assertAlmostEqual(result["input_gain_linear"], expected_gain, places=5)

    def test_pipewire_graph_uses_optimizer_bands_and_headroom(self):
        fit = {
            "centers_hz": [1000, 2500],
            "q": [1.0, 1.2],
            "gains_db": [-2.5, 0.75],
            "input_gain_linear": 0.812345,
        }
        graph = speaker_calibrate.filter_config("alsa_output.synthetic", fit)
        self.assertIn('"Freq" = 1000 "Q" = 1 "Gain" = -2.5', graph)
        self.assertIn('"Freq" = 2500 "Q" = 1.2 "Gain" = 0.75', graph)
        self.assertIn('"g_in" = 0.812345', graph)
        self.assertIn('Calibrated Speakers — Protected', graph)
        # The graph keeps its full fixed shape so profiles can be applied live.
        self.assertIn('name = p12_l label = bq_peaking', graph)
        self.assertIn('name = ls_r label = bq_lowshelf', graph)
        self.assertIn('name = hs_r label = bq_highshelf', graph)
        self.assertIn('{ output = "hs_r:Out" input = "limiter:in_r" }', graph)

    def test_graph_controls_fill_every_fixed_slot(self):
        fit = {
            "centers_hz": [1000, 2500],
            "q": [1.0, 1.2],
            "gains_db": [-2.5, 0.75],
            "input_gain_linear": 0.812345,
        }
        controls = speaker_calibrate.graph_controls(fit)
        slots = speaker_calibrate.PEAKING_SLOTS
        self.assertEqual(len(controls), 2 * (2 * 2 + 3 + 3 + 3 * slots + 3) + 1)
        self.assertEqual(controls["bs_l:Gain"], 0.0)
        self.assertEqual(controls["p1_l:Freq"], 1000.0)
        self.assertEqual(controls["p2_r:Gain"], 0.75)
        self.assertEqual(controls["p3_l:Gain"], 0.0)
        self.assertEqual(controls[f"p{slots}_r:Gain"], 0.0)
        self.assertEqual(controls["ls_l:Gain"], 0.0)
        self.assertEqual(controls["hs_r:Gain"], 0.0)
        self.assertEqual(controls["hp1_l:Freq"], 55.0)
        self.assertEqual(controls["limiter:g_in"], 0.812345)
        with self.assertRaises(ValueError):
            speaker_calibrate.graph_controls({
                "centers_hz": [1000] * (slots + 1),
                "q": [1.0] * (slots + 1),
                "gains_db": [-1.0] * (slots + 1),
                "input_gain_linear": 1.0,
            })

    def test_graph_parks_the_second_highpass_when_one_stage_is_enough(self):
        base = {"centers_hz": [1000], "q": [1.0], "gains_db": [-2.0], "input_gain_linear": 0.9}
        one = speaker_calibrate.graph_controls(dict(
            base, highpass={"frequency_hz": 160.0, "q": 0.707, "stages": 1}))
        self.assertEqual(one["hp1_l:Freq"], 160.0)
        self.assertEqual(one["hp2_l:Freq"], speaker_calibrate.PARKED_HIGHPASS_HZ)
        self.assertEqual(one["hp2_r:Freq"], speaker_calibrate.PARKED_HIGHPASS_HZ)
        two = speaker_calibrate.graph_controls(dict(
            base, highpass={"frequency_hz": 80.0, "q": 0.707, "stages": 2}))
        self.assertEqual(two["hp1_r:Freq"], 80.0)
        self.assertEqual(two["hp2_r:Freq"], 80.0)
        # A profile from before the high-pass was measured keeps its old chain.
        legacy = speaker_calibrate.graph_controls(base)
        self.assertEqual(legacy["hp1_l:Freq"], speaker_calibrate.DEFAULT_HIGHPASS_HZ)
        self.assertEqual(legacy["hp2_l:Freq"], speaker_calibrate.DEFAULT_HIGHPASS_HZ)

    def test_optimizer_payload_is_json_serializable(self):
        response = np.full(self.frequencies.size, -30.0)
        result = optimize_peq(
            self.measurement(response), "neutral", internal_mic=True
        )
        encoded = json.dumps(result)
        self.assertIn('"boost_decisions"', encoded)

    def test_broad_peak_uses_one_adaptive_filter_at_the_problem_frequency(self):
        target = pleasant_in_room_target(self.frequencies, "neutral") - 30.0
        response = target + 5.0 * np.exp(
            -0.5 * (np.log2(self.frequencies / 1100.0) / 0.38) ** 2
        )
        result = optimize_peq(
            self.measurement(response, repeat_curves=[response, response]),
            "neutral",
            internal_mic=True,
        )
        self.assertEqual(result["filter_count"], 1)
        self.assertAlmostEqual(result["centers_hz"][0], 1100.0, delta=150.0)
        self.assertLess(result["gains_db"][0], -2.0)
        self.assertEqual(result["cross_validation"]["mode"], "repeat-holdout")

    def test_two_separated_peaks_use_two_filters_not_a_dense_fixed_grid(self):
        target = pleasant_in_room_target(self.frequencies, "neutral") - 30.0
        response = (
            target
            + 4.0 * np.exp(-0.5 * (np.log2(self.frequencies / 700.0) / 0.30) ** 2)
            + 5.0 * np.exp(-0.5 * (np.log2(self.frequencies / 4000.0) / 0.28) ** 2)
        )
        result = optimize_peq(
            self.measurement(response, repeat_curves=[response, response]),
            "neutral",
            internal_mic=True,
        )
        self.assertEqual(result["filter_count"], 2)
        self.assertAlmostEqual(result["centers_hz"][0], 700.0, delta=120.0)
        self.assertAlmostEqual(result["centers_hz"][1], 4000.0, delta=350.0)

    def test_filter_seen_only_in_training_repeat_is_rejected(self):
        target = pleasant_in_room_target(self.frequencies, "neutral") - 30.0
        contaminated = target + 5.0 * np.exp(
            -0.5 * (np.log2(self.frequencies / 1800.0) / 0.12) ** 2
        )
        aggregate = np.median(np.vstack((contaminated, target)), axis=0)
        result = optimize_peq(
            self.measurement(
                aggregate, repeat_curves=[contaminated, target]
            ),
            "neutral",
            internal_mic=True,
        )
        self.assertEqual(result["filter_count"], 0)
        self.assertLessEqual(
            result["cross_validation"]["rmse_after_db"],
            result["cross_validation"]["rmse_before_db"] + 0.01,
        )

    def test_calibrated_external_mic_may_use_narrower_filter(self):
        target = pleasant_in_room_target(self.frequencies, "neutral") - 30.0
        response = target + 5.0 * np.exp(
            -0.5 * (np.log2(self.frequencies / 1800.0) / 0.10) ** 2
        )
        result = optimize_peq(
            self.measurement(
                response, calibrated=True, repeat_curves=[response, response]
            ),
            "neutral",
            internal_mic=False,
        )
        self.assertEqual(result["filter_count"], 1)
        self.assertGreater(result["q"][0], 2.0)
        self.assertLessEqual(result["q"][0], 4.0)


class CompareToggleTests(unittest.TestCase):
    def test_load_profile_requires_filters_and_a_speaker(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps({"speaker": {"name": "alsa_output.x"}, "fit": None}))
            self.assertIsNone(speaker_calibrate.load_profile(path))
            path.write_text(json.dumps({
                "speaker": {"name": "alsa_output.x"},
                "fit": {"centers_hz": [1000], "q": [1.0], "gains_db": [-1.0], "input_gain_linear": 1.0},
            }))
            self.assertIsNotNone(speaker_calibrate.load_profile(path))
            self.assertIsNone(speaker_calibrate.load_profile(Path(folder) / "missing.json"))

    def test_profile_summary_labels_a_saved_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps({
                "created_at": "2026-09-08T20:00:00+00:00",
                "voicing": "neutral",
                "fit": {"filter_count": 4},
                "plugin_version": "0.8.0",
            }))
            summary = speaker_calibrate.profile_summary(path)
            self.assertEqual(summary["label"], "2026-09-08 20:00 · 4 filters · flat · normal bass · protected")
            self.assertEqual(summary["plugin_version"], "0.8.0")
            self.assertIsNone(speaker_calibrate.profile_summary(Path(folder) / "missing.json"))


class BypassTests(unittest.TestCase):
    def test_bypass_regenerates_an_old_graph_before_zeroing_it(self):
        module = speaker_calibrate
        saved = {name: getattr(module, name) for name in (
            "DATA", "PROFILE", "PREVIOUS_PROFILE", "COMPARE_STATE", "FRAGMENT",
            "service_active", "apply_controls_live", "activate_profile", "restart_tuning",
        )}
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            module.DATA = base
            module.PROFILE = base / "active-profile.json"
            module.PREVIOUS_PROFILE = base / "previous-profile.json"
            module.COMPARE_STATE = base / "compare-state.json"
            module.FRAGMENT = base / "90-tuning.conf"
            module.PROFILE.write_text(json.dumps({
                "speaker": {"name": "alsa_output.x"}, "voicing": "warm",
                "fit": {"centers_hz": [1000], "q": [1.0], "gains_db": [-3.0], "input_gain_linear": 0.9},
            }))
            applied = []
            attempts = {"count": 0}
            def fake_apply(controls):
                attempts["count"] += 1
                applied.append(controls)
                return attempts["count"] > 1   # the old-shape graph refuses the first time
            activated = []
            def fake_activate(profile):
                activated.append(profile["speaker"]["name"])
                return "restart"
            try:
                module.service_active = lambda: True
                module.apply_controls_live = fake_apply
                module.activate_profile = fake_activate
                module.restart_tuning = lambda: (_ for _ in ()).throw(AssertionError("no bare restart"))
                payload = module.bypass_toggle()
            finally:
                for name, value in saved.items():
                    setattr(module, name, value)
            self.assertTrue(payload["bypass"])
            self.assertEqual(payload["method"], "restart")
            self.assertEqual(activated, ["alsa_output.x"])
            self.assertEqual(attempts["count"], 2)
            self.assertEqual(applied[-1]["limiter:g_in"], 1.0)
            self.assertEqual(applied[-1]["hp1_l:Freq"], 10.0)

    def test_transparent_controls_pass_audio_through(self):
        controls = speaker_calibrate.transparent_controls()
        self.assertEqual(controls["limiter:g_in"], 1.0)
        for name, value in controls.items():
            if name.endswith(":Gain"):
                self.assertEqual(value, 0.0, name)
            if name.startswith("hp") and name.endswith(":Freq"):
                self.assertEqual(value, 10.0, name)
        self.assertEqual(set(controls), set(speaker_calibrate.graph_controls(
            {"filters": [], "input_gain_linear": 1.0}
        )))


if __name__ == "__main__":
    unittest.main()
