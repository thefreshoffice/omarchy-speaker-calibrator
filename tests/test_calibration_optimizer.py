#!/usr/bin/python3

import subprocess
import sys
import unittest
import importlib.util
import tempfile
import json
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration_optimizer import (  # noqa: E402
    BOOST_HEADROOM_BUDGET_DB,
    EXCURSION_WEIGHT_CEILING,
    _window_boost_limit,
    boost_allowance_db,
    excursion_weight,
    BYPASS_MATCH_FLOOR_DB,
    CHANNEL_TRIM_LIMIT_DB,
    CHECK_REPEATABILITY_DB,
    HIGHPASS_BOUNDS_HZ,
    REFINEMENT_LIMIT_DB,
    apply_refinement,
    bypass_level_match_db,
    estimate_channel_trim,
    refinement_residual,
    verification_report,
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
        # Clear of the high-pass, or the lift would land where it was removed.
        self.assertGreater(shelf["frequency_hz"], full["highpass"]["frequency_hz"] * 2.0)
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
        controls = speaker_calibrate.graph_controls(fit, bass_enhancer=False)
        self.assertEqual(controls["ls_l:Freq"], 300.0)
        self.assertEqual(controls["ls_r:Gain"], -3.0)
        self.assertEqual(controls["bs_l:Freq"], 500.0)
        self.assertEqual(controls["bs_r:Gain"], 3.0)
        self.assertEqual(controls["p1_l:Freq"], 1000.0)
        self.assertEqual(controls["p2_l:Gain"], 0.0)
        self.assertEqual(controls["hs_l:Freq"], 4000.0)
        self.assertEqual(controls["hs_r:Gain"], -4.0)
        graph = speaker_calibrate.filter_config("alsa_output.synthetic", fit, bass_enhancer=False)
        self.assertIn('name = bs_l label = bq_lowshelf control = { "Freq" = 500 "Q" = 0.707 "Gain" = 3 }', graph)
        # A 0.10.0 profile stored the bass shelf as low_shelf.
        legacy = {"centers_hz": [1000], "q": [1.0], "gains_db": [-2.5],
                  "low_shelf": {"frequency_hz": 500, "q": 0.707, "gain_db": 3.0},
                  "input_gain_linear": 0.5}
        legacy_controls = speaker_calibrate.graph_controls(legacy, bass_enhancer=False)
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
        # Peak-weighted smoothing fills part of a dip on purpose, so the
        # boost it earns is smaller than the raw depth would suggest.
        self.assertGreater(boost, 0.4)
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
        graph = speaker_calibrate.filter_config("alsa_output.synthetic", fit, bass_enhancer=False)
        self.assertIn('"Freq" = 1000 "Q" = 1 "Gain" = -2.5', graph)
        self.assertIn('"Freq" = 2500 "Q" = 1.2 "Gain" = 0.75', graph)
        self.assertIn('"g_in" = 0.812345', graph)
        self.assertIn('Calibrated Speakers — Protected', graph)
        # The graph keeps its full fixed shape so profiles can be applied live.
        self.assertIn('name = p12_l label = bq_peaking', graph)
        self.assertIn('name = ls_r label = bq_lowshelf', graph)
        self.assertIn('name = hs_r label = bq_highshelf', graph)
        self.assertIn('{ output = "hs_r:Out" input = "bal_r:In" }', graph)
        self.assertIn('{ output = "bal_r:Out" input = "limiter:in_r" }', graph)
        self.assertIn('name = bal_l label = linear control = { "Mult" = 1 "Add" = 0 }', graph)

    def test_graph_controls_fill_every_fixed_slot(self):
        fit = {
            "centers_hz": [1000, 2500],
            "q": [1.0, 1.2],
            "gains_db": [-2.5, 0.75],
            "input_gain_linear": 0.812345,
        }
        controls = speaker_calibrate.graph_controls(fit, bass_enhancer=False)
        slots = speaker_calibrate.PEAKING_SLOTS
        # Per channel: two high-passes, three shelves, the parametric slots,
        # and the balance trim's two controls; plus the limiter's input gain
        # and the compensator's six, which are not per channel.
        self.assertEqual(len(controls), 2 * (2 * 2 + 3 + 3 + 3 * slots + 3 + 2) + 1 + 6)
        self.assertEqual(controls["bs_l:Gain"], 0.0)
        self.assertEqual(controls["bal_l:Mult"], 1.0)
        self.assertEqual(controls["bal_r:Add"], 0.0)
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
            }, bass_enhancer=False)

    def test_graph_parks_the_second_highpass_when_one_stage_is_enough(self):
        base = {"centers_hz": [1000], "q": [1.0], "gains_db": [-2.0], "input_gain_linear": 0.9}
        one = speaker_calibrate.graph_controls(dict(
            base, highpass={"frequency_hz": 160.0, "q": 0.707, "stages": 1}), bass_enhancer=False)
        self.assertEqual(one["hp1_l:Freq"], 160.0)
        self.assertEqual(one["hp2_l:Freq"], speaker_calibrate.PARKED_HIGHPASS_HZ)
        self.assertEqual(one["hp2_r:Freq"], speaker_calibrate.PARKED_HIGHPASS_HZ)
        two = speaker_calibrate.graph_controls(dict(
            base, highpass={"frequency_hz": 80.0, "q": 0.707, "stages": 2}), bass_enhancer=False)
        self.assertEqual(two["hp1_r:Freq"], 80.0)
        self.assertEqual(two["hp2_r:Freq"], 80.0)
        # A profile from before the high-pass was measured keeps its old chain.
        legacy = speaker_calibrate.graph_controls(base, bass_enhancer=False)
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

    def test_the_plain_speakers_are_matched_to_the_correction(self):
        # Cuts cost 13 dB of loudness and 4.4 dB was added back, so the
        # correction plays 8.6 dB below the raw speaker.
        self.assertAlmostEqual(
            bypass_level_match_db({"loudness_loss_db": 13.0, "net_input_gain_db": 4.4}),
            -8.6, places=2,
        )
        # Protected mode gives back nothing, so the gap is wider.
        self.assertAlmostEqual(
            bypass_level_match_db({"loudness_loss_db": 13.0, "net_input_gain_db": -1.0}),
            -14.0, places=2,
        )

    def test_the_plain_speakers_are_never_turned_up(self):
        # A correction that ends up louder than the raw speaker cannot be
        # matched by boosting the raw signal, which has no headroom to give.
        self.assertEqual(
            bypass_level_match_db({"loudness_loss_db": 0.5, "net_input_gain_db": 5.0}), 0.0
        )

    def test_an_absurd_match_is_floored(self):
        self.assertEqual(
            bypass_level_match_db({"loudness_loss_db": 60.0, "net_input_gain_db": 0.0}),
            BYPASS_MATCH_FLOOR_DB,
        )

    def test_a_profile_from_before_this_is_left_at_unity(self):
        self.assertEqual(bypass_level_match_db({"headroom_db": 1.0}), -1.0)
        self.assertEqual(bypass_level_match_db({}), -1.0)

    def test_the_match_reaches_the_graph_as_input_gain(self):
        controls = speaker_calibrate.transparent_controls(
            bass_enhancer=False, level_match_db=-8.6
        )
        self.assertAlmostEqual(controls["limiter:g_in"], 10 ** (-8.6 / 20.0), places=5)
        # Everything else is still flat, so only the level differs.
        for name, value in controls.items():
            if name.endswith(":Gain"):
                self.assertEqual(value, 0.0, name)

    def test_a_measurement_flattens_without_the_match(self):
        # The speaker has to be measured as it is, not as the correction
        # leaves it, so the flattening used for a calibration is unity.
        controls = speaker_calibrate.transparent_controls(bass_enhancer=False)
        self.assertEqual(controls["limiter:g_in"], 1.0)

    def test_transparent_controls_pass_audio_through(self):
        controls = speaker_calibrate.transparent_controls(bass_enhancer=False)
        self.assertEqual(controls["limiter:g_in"], 1.0)
        for name, value in controls.items():
            if name.endswith(":Gain"):
                self.assertEqual(value, 0.0, name)
            if name.startswith("hp") and name.endswith(":Freq"):
                self.assertEqual(value, 10.0, name)
        self.assertEqual(set(controls), set(speaker_calibrate.graph_controls(
            {"filters": [], "input_gain_linear": 1.0}, bass_enhancer=False
        )))


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.frequencies = np.geomspace(80.0, 16_000.0, 240)
        # A speaker with one fat resonance at 4 kHz, outside the 250-2000 Hz
        # band the curves are level-aligned on, so the bump cannot move the
        # alignment and hide itself.
        self.bump = 14.0 * np.exp(
            -0.5 * (np.log2(self.frequencies / 4000.0) / 0.5) ** 2
        )
        self.target = np.full(self.frequencies.size, -20.0)
        self.original = self.target + self.bump
        self.predicted = self.target.copy()

    def fit(self, highpass_hz=100.0):
        return {
            "measured_smoothed_db": self.original.tolist(),
            "predicted_response_db": self.predicted.tolist(),
            "target": {"aligned_db": self.target.tolist()},
            "highpass": {"frequency_hz": highpass_hz, "stages": 1},
        }

    def report(self, verified, snr=None, frequencies=None):
        grid = self.frequencies if frequencies is None else frequencies
        return verification_report(grid, verified, snr, self.fit(), self.frequencies)

    def test_a_correction_that_landed_passes(self):
        report = self.report(self.predicted)
        self.assertEqual(report["verdict"], "pass", report["notes"])
        self.assertLess(report["model_error_db"]["rms"], 0.2)
        errors = report["target_error_db"]
        self.assertGreater(errors["before"], 2.0)
        self.assertLess(errors["measured"], 0.2)
        self.assertEqual(report["notes"], [])

    def test_filters_that_never_reached_the_speaker_fail(self):
        # The sweeps went somewhere uncorrected, so the resonance is still there.
        report = self.report(self.original)
        self.assertEqual(report["verdict"], "fail")
        self.assertGreater(report["model_error_db"]["rms"], 4.0)
        self.assertAlmostEqual(report["model_error_db"]["worst_hz"], 4000.0, delta=400.0)
        self.assertTrue(any("away from what the filters" in note for note in report["notes"]))

    def test_a_correction_that_made_it_worse_fails(self):
        report = self.report(self.target + 2.0 * self.bump)
        self.assertEqual(report["verdict"], "fail")
        errors = report["target_error_db"]
        self.assertGreater(errors["measured"], errors["before"])
        self.assertTrue(any("worse than the raw" in note for note in report["notes"]))

    def test_the_verdict_ignores_playback_level(self):
        quiet = self.report(self.predicted - 12.0)
        loud = self.report(self.predicted + 7.0)
        self.assertEqual(quiet["verdict"], "pass")
        self.assertEqual(loud["verdict"], "pass")
        self.assertAlmostEqual(quiet["model_error_db"]["rms"], loud["model_error_db"]["rms"], places=6)

    def test_a_small_miss_is_only_a_warning(self):
        report = self.report(self.predicted + 0.5 * self.bump)
        self.assertEqual(report["verdict"], "warning")
        self.assertTrue(any("differs from the plan" in note for note in report["notes"]))

    def test_the_check_reads_its_own_frequency_grid(self):
        coarse = np.geomspace(80.0, 16_000.0, 97)
        verified = np.interp(np.log(coarse), np.log(self.frequencies), self.predicted)
        report = self.report(verified, frequencies=coarse)
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(len(report["frequency_hz"]), coarse.size)

    def test_noisy_and_high_passed_bands_are_left_out(self):
        snr = np.full(self.frequencies.size, 30.0)
        snr[self.frequencies > 2000.0] = 1.0
        report = self.report(self.original, snr=snr)
        # The resonance sat entirely in the band the check could not hear, so
        # what remains agrees with the plan and no verdict is drawn from noise.
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["analysis_band_hz"][0], 160.0)
        self.assertLess(report["analysed_points"], self.frequencies.size)
        self.assertNotIn("treble", report["model_error_db"]["bands"])
        self.assertIn("low mid", report["model_error_db"]["bands"])

    def test_the_high_pass_region_is_never_judged(self):
        report = verification_report(
            self.frequencies, self.predicted, None,
            dict(self.fit(), highpass={"frequency_hz": 400.0, "stages": 1}),
            self.frequencies,
        )
        self.assertEqual(report["analysis_band_hz"][0], 400.0)


class RawMeasurementTests(unittest.TestCase):
    def test_only_real_outputs_may_be_calibrated(self):
        self.assertTrue(speaker_calibrate.is_physical_sink("alsa_output.pci-0000_00_1f.3.analog-stereo"))
        self.assertFalse(speaker_calibrate.is_physical_sink(speaker_calibrate.VIRTUAL_SINK))
        self.assertFalse(speaker_calibrate.is_physical_sink("omarchy_speaker_tuning_output"))
        self.assertFalse(speaker_calibrate.is_physical_sink("bluez_output.AA_BB"))

    def test_calibrating_the_calibrated_sink_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            speaker_calibrate.build_profile(
                {"name": speaker_calibrate.VIRTUAL_SINK}, {"name": "alsa_input.x"}, 0, "warm"
            )
        self.assertIn("never the calibrated one", str(raised.exception))

    def test_the_filter_is_flattened_and_restored_around_a_measurement(self):
        module = speaker_calibrate
        saved = {name: getattr(module, name) for name in (
            "service_active", "tuning_node_id", "live_controls", "apply_controls_live",
            "compare_state", "transparent_controls",
        )}
        original_transparent = module.transparent_controls
        installed = module.graph_controls({
            "filters": [{"type": "peaking", "frequency_hz": 800.0, "q": 1.0, "gain_db": -6.0}],
            "input_gain_linear": 0.9,
        }, bass_enhancer=False)
        applied = []
        try:
            module.service_active = lambda: True
            module.tuning_node_id = lambda: 42
            module.compare_state = lambda: {"active": "current", "bypass": False}
            module.live_controls = lambda node_id: dict(installed, **{"limiter:grgv_l": 1.0})
            module.apply_controls_live = lambda controls: applied.append(dict(controls)) or True
            module.transparent_controls = lambda bass_enhancer=None: original_transparent(
                bass_enhancer=False
            )
            with module.correction_silenced() as silenced:
                self.assertTrue(silenced)
                self.assertEqual(applied[-1]["p1_l:Gain"], 0.0)
                self.assertEqual(applied[-1]["limiter:g_in"], 1.0)
        finally:
            for name, value in saved.items():
                setattr(module, name, value)
        self.assertEqual(len(applied), 2)
        # Restored exactly, and never the read-only meter that was in the readback.
        self.assertEqual(applied[-1], installed)
        self.assertNotIn("limiter:grgv_l", applied[-1])

    def test_the_check_mutes_what_invents_sound_and_restores_it(self):
        module = speaker_calibrate
        saved = {name: getattr(module, name) for name in (
            "service_active", "tuning_node_id", "live_controls", "apply_controls_live",
        )}
        running = {
            "hp1_l:Freq": 195.8, "p1_l:Gain": -7.0, "limiter:g_in": 1.66,
            "bass:bypass": 0.0, "bass:amt": 1.45, "bass:ceil": 195.8,
            "loudcomp:enabled": 0.0,
        }
        applied = []
        try:
            module.service_active = lambda: True
            module.tuning_node_id = lambda: 7
            module.live_controls = lambda node_id: dict(running)
            module.apply_controls_live = lambda controls: applied.append(dict(controls)) or True
            with module.added_sound_silenced() as muted:
                self.assertTrue(muted)
        finally:
            for name, value in saved.items():
                setattr(module, name, value)
        self.assertEqual(len(applied), 2)
        self.assertEqual(applied[0]["bass:bypass"], 1.0)
        self.assertEqual(applied[0]["bass:amt"], 0.0)
        # Only the add-on is touched: the filters under test stay as they are.
        self.assertNotIn("p1_l:Gain", applied[0])
        self.assertNotIn("limiter:g_in", applied[0])
        self.assertEqual(applied[1], {
            "bass:bypass": 0.0, "bass:amt": 1.45, "bass:ceil": 195.8,
        })

    def test_nothing_is_muted_when_nothing_is_inventing_sound(self):
        module = speaker_calibrate
        saved = {name: getattr(module, name) for name in (
            "service_active", "tuning_node_id", "live_controls", "apply_controls_live",
        )}
        touched = []
        try:
            module.service_active = lambda: True
            module.tuning_node_id = lambda: 7
            module.live_controls = lambda node_id: {
                "bass:bypass": 1.0, "bass:amt": 0.0, "loudcomp:enabled": 0.0,
            }
            module.apply_controls_live = lambda controls: touched.append(controls) or True
            with module.added_sound_silenced() as muted:
                self.assertFalse(muted)
        finally:
            for name, value in saved.items():
                setattr(module, name, value)
        self.assertEqual(touched, [])

    def test_nothing_is_touched_when_the_tuning_is_not_running(self):
        module = speaker_calibrate
        saved = {"service_active": module.service_active, "apply_controls_live": module.apply_controls_live}
        touched = []
        try:
            module.service_active = lambda: False
            module.apply_controls_live = lambda controls: touched.append(controls) or True
            with module.correction_silenced() as silenced:
                self.assertFalse(silenced)
        finally:
            for name, value in saved.items():
                setattr(module, name, value)
        self.assertEqual(touched, [])


class RefinementTests(unittest.TestCase):
    def setUp(self):
        self.frequencies = np.geomspace(80.0, 16_000.0, 240)
        self.error = 6.0 * np.exp(-0.5 * (np.log2(self.frequencies / 1500.0) / 0.4) ** 2)

    def check(self, difference, *, uncertainty=None, usable=None):
        size = self.frequencies.size
        return {
            "frequency_hz": self.frequencies.tolist(),
            "verified_db": difference.tolist(),
            "predicted_db": np.zeros(size).tolist(),
            "uncertainty_db": (np.full(size, CHECK_REPEATABILITY_DB)
                               if uncertainty is None else uncertainty).tolist(),
            "usable": (np.ones(size, dtype=bool) if usable is None else usable).tolist(),
        }

    def test_the_residual_follows_what_the_check_saw(self):
        residual = refinement_residual(self.check(self.error), self.frequencies)
        peak = float(residual[np.argmin(np.abs(self.frequencies - 1500.0))])
        # Smoothing a narrow feature shortens it; erring low is the safe way
        # to be wrong, since it only slows the iteration down.
        self.assertAlmostEqual(peak, 6.0, delta=1.0)
        self.assertLess(peak, 6.0)
        # Far from the error nothing moves.
        self.assertLess(abs(residual[np.argmin(np.abs(self.frequencies - 200.0))]), 0.3)

    def test_a_difference_the_check_cannot_resolve_is_mostly_ignored(self):
        noise = np.full(self.frequencies.size, 0.8)
        residual = refinement_residual(self.check(noise), self.frequencies)
        self.assertLess(float(np.max(np.abs(residual))), 0.45)

    def test_one_round_can_only_move_so_far(self):
        huge = np.full(self.frequencies.size, 40.0)
        residual = refinement_residual(self.check(huge), self.frequencies)
        self.assertLessEqual(float(np.max(np.abs(residual))), REFINEMENT_LIMIT_DB + 1e-6)

    def test_bands_the_check_could_not_judge_are_left_alone(self):
        usable = self.frequencies >= 500.0
        residual = refinement_residual(
            self.check(np.full(self.frequencies.size, 5.0), usable=usable), self.frequencies
        )
        self.assertAlmostEqual(float(residual[self.frequencies < 400.0].max()), 0.0, places=6)
        self.assertGreater(float(residual[self.frequencies > 2000.0].mean()), 3.0)

    def test_applying_a_residual_moves_every_curve_together(self):
        size = self.frequencies.size
        measurement = {
            "frequency_hz": self.frequencies.tolist(),
            "level_dbfs": np.full(size, -30.0).tolist(),
            "channels": [
                {"output_channel": "left", "response_db": np.full(size, -30.0).tolist(),
                 "uncertainty_db": np.full(size, 0.4).tolist()},
                {"output_channel": "right", "response_db": np.full(size, -29.0).tolist(),
                 "uncertainty_db": np.full(size, 0.4).tolist()},
            ],
            "validation_curves": [
                {"repeat": 0, "output_channel": "left", "response_db": np.full(size, -30.5).tolist()},
            ],
        }
        residual = np.full(size, 2.0)
        refined = apply_refinement(measurement, residual)
        self.assertAlmostEqual(refined["level_dbfs"][10], -28.0)
        self.assertAlmostEqual(refined["channels"][0]["response_db"][10], -28.0)
        self.assertAlmostEqual(refined["channels"][1]["response_db"][10], -27.0)
        self.assertAlmostEqual(refined["validation_curves"][0]["response_db"][10], -28.5)
        # The check's own spread joins the uncertainty the optimizer weighs by.
        self.assertGreater(refined["channels"][0]["uncertainty_db"][10], 0.4)
        # The original is untouched.
        self.assertAlmostEqual(measurement["level_dbfs"][10], -30.0)

    def test_refining_moves_the_fit_toward_what_the_check_measured(self):
        """A resonance the first measurement understated gets cut deeper."""
        frequencies = self.frequencies
        truth = np.full(frequencies.size, -30.0) + 12.0 * np.exp(
            -0.5 * (np.log2(frequencies / 1500.0) / 0.4) ** 2
        )
        understated = truth - self.error   # what the first measurement saw
        measurement = {
            "frequency_hz": frequencies.tolist(),
            "level_dbfs": understated.tolist(),
            "rate_hz": 48_000,
            "microphone_calibration": None,
            "channels": [
                {"output_channel": side, "response_db": understated.tolist(),
                 "uncertainty_db": np.full(frequencies.size, 0.2).tolist()}
                for side in ("left", "right")
            ],
        }
        first = optimize_peq(measurement, "neutral", internal_mic=True)
        refined = apply_refinement(measurement, refinement_residual(self.check(self.error), frequencies))
        second = optimize_peq(refined, "neutral", internal_mic=True)

        def cut_at(result, hz):
            return float(np.interp(
                np.log(hz), np.log(frequencies), np.asarray(result["correction_response_db"])
            ))
        self.assertLess(cut_at(second, 1500.0), cut_at(first, 1500.0) - 2.0)


class BassEnhancerTests(unittest.TestCase):
    fit = {
        "filters": [{"type": "peaking", "frequency_hz": 900.0, "q": 1.2, "gain_db": -5.0}],
        "highpass": {"frequency_hz": 185.0, "q": 0.707, "stages": 1},
        "input_gain_linear": 0.9,
    }

    def bundle(self, folder, *, ports=None, uri=None):
        """A pretend LV2 bundle, complete or deliberately broken."""
        module = speaker_calibrate
        ports = module.BASS_ENHANCER_PORTS if ports is None else ports
        directory = Path(folder) / "bankstown.lv2"
        directory.mkdir(parents=True)
        body = f"<{uri or module.BASS_ENHANCER_URI}> a lv2:Plugin ;\n"
        body += "".join(f'    lv2:port [ lv2:symbol "{port}" ] ;\n' for port in ports)
        (directory / "bankstown.ttl").write_text(body)
        return directory

    def with_paths(self, folder):
        module = speaker_calibrate
        original = module.BASS_ENHANCER_SEARCH_PATHS
        module.BASS_ENHANCER_SEARCH_PATHS = (str(folder),)
        return original

    def test_a_complete_bundle_is_detected(self):
        module = speaker_calibrate
        with tempfile.TemporaryDirectory() as folder:
            directory = self.bundle(folder)
            original = self.with_paths(folder)
            try:
                status = module.bass_enhancer_status()
            finally:
                module.BASS_ENHANCER_SEARCH_PATHS = original
        self.assertTrue(status["usable"])
        self.assertTrue(status["installed"])
        self.assertEqual(status["path"], str(directory))
        self.assertEqual(status["missing_ports"], [])

    def test_a_build_missing_ports_is_refused(self):
        module = speaker_calibrate
        with tempfile.TemporaryDirectory() as folder:
            self.bundle(folder, ports=("in_l", "in_r", "out_l", "out_r", "bypass"))
            original = self.with_paths(folder)
            try:
                status = module.bass_enhancer_status()
            finally:
                module.BASS_ENHANCER_SEARCH_PATHS = original
        self.assertTrue(status["installed"])
        self.assertFalse(status["usable"])
        self.assertIn("amt", status["missing_ports"])

    def test_nothing_installed_reports_nothing(self):
        module = speaker_calibrate
        with tempfile.TemporaryDirectory() as folder:
            original = self.with_paths(folder)
            try:
                status = module.bass_enhancer_status()
            finally:
                module.BASS_ENHANCER_SEARCH_PATHS = original
        self.assertFalse(status["installed"])
        self.assertFalse(status["usable"])

    def test_the_graph_never_mentions_an_absent_add_on(self):
        graph = speaker_calibrate.filter_config(
            "alsa_output.x", self.fit, bass_enhancer=False, deep_bass=False
        )
        self.assertNotIn("bankstown", graph)
        self.assertNotIn("bass:", graph)
        # Without the add-on the compensator is what the sound enters through.
        self.assertIn('inputs  = [ "loudcomp:in_l" "loudcomp:in_r" ]'.replace("  ", " "),
                      graph.replace("  ", " "))
        self.assertIn('{ output = "loudcomp:out_l" input = "hp1_l:In" }', graph)
        controls = speaker_calibrate.graph_controls(self.fit, bass_enhancer=False)
        self.assertFalse([name for name in controls if name.startswith("bass:")])

    def test_the_add_on_is_wired_in_front_of_the_high_pass(self):
        graph = speaker_calibrate.filter_config(
            "alsa_output.x", self.fit, bass_enhancer=True, deep_bass=True
        )
        self.assertIn(speaker_calibrate.BASS_ENHANCER_URI, graph)
        self.assertIn('{ output = "bass:out_l" input = "loudcomp:in_l" }', graph)
        self.assertIn('{ output = "bass:out_r" input = "loudcomp:in_r" }', graph)
        self.assertIn('"bass:in_l"', graph)
        self.assertIn('"bass:in_r"', graph)
        # The links are what order a filter chain, not the order the nodes
        # happen to be declared in: sound enters the add-on, so it sees the
        # bass before anything shapes or removes it.
        self.assertIn('"bass:in_l"', graph)
        self.assertIn('{ output = "loudcomp:out_l" input = "hp1_l:In" }', graph)
        self.assertNotIn('input = "bass:in_l" }', graph)

    def test_its_band_follows_the_measured_knee(self):
        controls = speaker_calibrate.graph_controls(
            self.fit, bass_enhancer=True, deep_bass=True
        )
        self.assertEqual(controls["bass:ceil"], 185.0)
        self.assertEqual(controls["bass:final_hp"], 185.0)
        self.assertEqual(controls["bass:floor"], speaker_calibrate.BASS_ENHANCER_FLOOR_HZ)
        self.assertEqual(controls["bass:bypass"], 0.0)
        self.assertEqual(controls["bass:amt"], speaker_calibrate.BASS_ENHANCER_AMOUNT)

    def test_switching_it_off_only_changes_controls(self):
        on = speaker_calibrate.graph_controls(self.fit, bass_enhancer=True, deep_bass=True)
        off = speaker_calibrate.graph_controls(self.fit, bass_enhancer=True, deep_bass=False)
        self.assertEqual(set(on), set(off))
        self.assertEqual(off["bass:bypass"], 1.0)
        self.assertEqual(off["bass:amt"], 0.0)
        # Everything that is not the add-on is untouched, so it applies live.
        self.assertEqual(
            {k: v for k, v in on.items() if not k.startswith("bass:")},
            {k: v for k, v in off.items() if not k.startswith("bass:")},
        )

    def test_a_corner_above_the_plugin_limit_is_clamped(self):
        fit = dict(self.fit, highpass={"frequency_hz": 400.0, "q": 0.707, "stages": 1})
        controls = speaker_calibrate.graph_controls(fit, bass_enhancer=True, deep_bass=True)
        self.assertEqual(controls["bass:ceil"], speaker_calibrate.BASS_ENHANCER_MAX_HZ)
        self.assertEqual(controls["bass:final_hp"], speaker_calibrate.BASS_ENHANCER_MAX_HZ)

    def install_command_with(self, stdout, returncode=0):
        module = speaker_calibrate
        original = module.run
        class Result:
            pass
        result = Result()
        result.returncode = returncode
        result.stdout = stdout
        try:
            module.run = lambda *args, **kwargs: result
            return module.bass_enhancer_install_command()
        finally:
            module.run = original

    def test_a_repository_copy_is_preferred_over_a_source_build(self):
        command, repository = self.install_command_with(
            "Repository      : omarchy\nName            : bankstown\n"
        )
        self.assertEqual(command, "omarchy pkg add bankstown")
        self.assertEqual(repository, "omarchy")

    def test_any_repository_counts_not_just_omarchy(self):
        command, repository = self.install_command_with(
            "Repository      : extra\nName            : bankstown\n"
        )
        self.assertEqual(command, "omarchy pkg add bankstown")
        self.assertEqual(repository, "extra")

    def test_it_falls_back_to_the_aur_when_no_repository_has_it(self):
        command, repository = self.install_command_with("", returncode=1)
        self.assertEqual(command, "omarchy pkg aur add bankstown")
        self.assertIsNone(repository)

    def test_bypassing_the_calibration_matches_the_running_shape(self):
        with_addon = speaker_calibrate.transparent_controls(bass_enhancer=True)
        without = speaker_calibrate.transparent_controls(bass_enhancer=False)
        self.assertEqual(with_addon["bass:bypass"], 1.0)
        self.assertEqual(
            set(with_addon) - set(without),
            {name for name in with_addon if name.startswith("bass:")},
        )


class ChannelTrimTests(unittest.TestCase):
    def setUp(self):
        self.frequencies = np.geomspace(80.0, 16_000.0, 240)

    def measurement(self, difference_db, *, spread=0.2):
        size = self.frequencies.size
        left = np.full(size, -30.0) + difference_db
        right = np.full(size, -30.0)
        return {
            "frequency_hz": self.frequencies.tolist(),
            "level_dbfs": ((left + right) / 2).tolist(),
            "channels": [
                {"output_channel": "left", "response_db": left.tolist(),
                 "uncertainty_db": np.full(size, spread).tolist()},
                {"output_channel": "right", "response_db": right.tolist(),
                 "uncertainty_db": np.full(size, spread).tolist()},
            ],
        }

    def test_a_built_in_array_is_refused_outright(self):
        trim = estimate_channel_trim(
            self.measurement(2.5), internal_mic=True, mode="auto"
        )
        self.assertFalse(trim["applied"])
        self.assertIn("closer to one speaker", trim["reason"])
        # It still reports what it saw, so the difference is visible.
        self.assertAlmostEqual(trim["difference_db"], 2.5, delta=0.1)

    def test_switched_off_it_measures_but_does_not_act(self):
        trim = estimate_channel_trim(
            self.measurement(2.5), internal_mic=False, mode="off"
        )
        self.assertFalse(trim["applied"])
        self.assertEqual(trim["reason"], "switched off")
        self.assertAlmostEqual(trim["difference_db"], 2.5, delta=0.1)
        self.assertEqual(trim["left_db"], 0.0)
        self.assertEqual(trim["right_db"], 0.0)

    def test_a_clear_difference_turns_the_louder_side_down(self):
        trim = estimate_channel_trim(
            self.measurement(2.0), internal_mic=False, mode="auto"
        )
        self.assertTrue(trim["applied"])
        self.assertAlmostEqual(trim["left_db"], -2.0, delta=0.1)
        self.assertEqual(trim["right_db"], 0.0)
        self.assertIn("left measured", trim["reason"])

    def test_the_quieter_side_is_never_boosted(self):
        trim = estimate_channel_trim(
            self.measurement(-2.0), internal_mic=False, mode="auto"
        )
        self.assertTrue(trim["applied"])
        self.assertEqual(trim["left_db"], 0.0)
        self.assertAlmostEqual(trim["right_db"], -2.0, delta=0.1)
        self.assertLessEqual(max(trim["left_db"], trim["right_db"]), 0.0)

    def test_a_difference_inside_the_noise_is_left_alone(self):
        trim = estimate_channel_trim(
            self.measurement(0.9, spread=1.0), internal_mic=False, mode="auto"
        )
        self.assertFalse(trim["applied"])
        self.assertIn("does not stand clear", trim["reason"])

    def test_a_wiring_fault_is_capped_and_reported(self):
        trim = estimate_channel_trim(
            self.measurement(9.0), internal_mic=False, mode="auto"
        )
        self.assertTrue(trim["applied"])
        self.assertEqual(trim["left_db"], -CHANNEL_TRIM_LIMIT_DB)
        self.assertIn("capped", trim["reason"])
        self.assertAlmostEqual(trim["difference_db"], 9.0, delta=0.1)

    def test_the_trim_reaches_the_graph_as_a_gain(self):
        fit = {
            "filters": [{"type": "peaking", "frequency_hz": 900.0, "q": 1.0, "gain_db": -3.0}],
            "input_gain_linear": 0.9,
            "channel_trim": {"applied": True, "left_db": -2.0, "right_db": 0.0},
        }
        controls = speaker_calibrate.graph_controls(fit, bass_enhancer=False)
        self.assertAlmostEqual(controls["bal_l:Mult"], 10 ** (-2.0 / 20.0), places=5)
        self.assertEqual(controls["bal_r:Mult"], 1.0)
        graph = speaker_calibrate.filter_config(
            "alsa_output.x", fit, bass_enhancer=False
        )
        self.assertIn('name = bal_l label = linear control = { "Mult" = 0.7943', graph)

    def test_a_profile_without_a_trim_is_unity(self):
        controls = speaker_calibrate.graph_controls(
            {"filters": [], "input_gain_linear": 1.0}, bass_enhancer=False
        )
        self.assertEqual(controls["bal_l:Mult"], 1.0)
        self.assertEqual(controls["bal_r:Mult"], 1.0)


class StartupCostTests(unittest.TestCase):
    """The panel asks for status constantly; that path must stay cheap."""

    HELPER = str(Path(__file__).resolve().parents[1] / "speaker-calibrate.py")

    def test_importing_the_helper_does_not_drag_in_numpy(self):
        probe = (
            "import importlib.util, sys;"
            f"spec = importlib.util.spec_from_file_location('sc', {self.HELPER!r});"
            "module = importlib.util.module_from_spec(spec);"
            "spec.loader.exec_module(module);"
            "print('numpy' in sys.modules, 'calibration_dsp' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True,
            cwd=str(Path(self.HELPER).parent),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["False", "False"], result.stdout)

    def test_asking_for_the_measurement_code_loads_it(self):
        probe = (
            "import importlib.util, sys;"
            f"spec = importlib.util.spec_from_file_location('sc', {self.HELPER!r});"
            "module = importlib.util.module_from_spec(spec);"
            "spec.loader.exec_module(module);"
            "module.load_dsp();"
            "print('numpy' in sys.modules, callable(module.optimize_peq), module.SweepSpec().rate)"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True,
            cwd=str(Path(self.HELPER).parent),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["True", "True", "48000"])

    def test_the_constants_still_match_the_sweep_they_describe(self):
        from calibration_dsp import SweepSpec
        spec = SweepSpec()
        self.assertEqual(speaker_calibrate.RATE, spec.rate)
        self.assertEqual(speaker_calibrate.EXTERNAL_SPEAKER_LEVEL_DBFS, spec.level_dbfs)


class LoudnessCompensationTests(unittest.TestCase):
    """The curve must be the only thing it applies; the level is not ours."""

    def net_gain_db(self, controls, base_gain=1.0):
        """What the compensation does to the level, contour aside.

        The contour attenuates by its volume setting and the make-up on the
        limiter pays that back, so the two have to cancel.
        """
        import math
        return (controls["loudcomp:volume"]
                + 20 * math.log10(controls["limiter:g_in"] / base_gain))

    def test_the_volume_is_read_from_what_pactl_prints(self):
        # The number and its unit are separate words, and every channel is
        # listed; an earlier version matched the bare "dB" and always read 0.
        reading = ("Volume: front-left: 36044 /  55% / -15.58 dB,"
                   "   front-right: 36044 /  55% / -15.58 dB\n        balance 0.00\n")
        self.assertAlmostEqual(speaker_calibrate.parse_sink_volume_db(reading), -15.58)
        full = ("Volume: front-left: 65536 / 100% / 0.00 dB,"
                "   front-right: 65536 / 100% / 0.00 dB\n")
        self.assertAlmostEqual(speaker_calibrate.parse_sink_volume_db(full), 0.0)
        # An unbalanced pair follows the louder side, which sets the level.
        lopsided = ("Volume: front-left: 36044 /  55% / -15.58 dB,"
                    "   front-right: 65536 / 100% / -2.00 dB\n")
        self.assertAlmostEqual(speaker_calibrate.parse_sink_volume_db(lopsided), -2.0)
        self.assertEqual(speaker_calibrate.parse_sink_volume_db("no volume here"), 0.0)

    def test_it_never_changes_the_level(self):
        # Its volume control attenuates as well as choosing the contour, and
        # the attenuating belongs to the output device, so the two must cancel.
        for volume in (0.0, -6.0, -9.4, -12.0, -35.0):
            for base in (1.0, 1.663413):
                controls = speaker_calibrate.loudness_controls(volume, True, base)
                self.assertAlmostEqual(self.net_gain_db(controls, base), 0.0, places=4)

    def test_the_make_up_never_goes_in_front_of_the_compensator(self):
        # The plugin decides how much to compensate from the level reaching
        # it, so gain on its own input reads as the music being loud again and
        # cancels the contour: measured at 60 Hz against 1500 Hz, +25 dB of
        # bass at -20 dB became -1 dB once the matching input gain was set.
        # The make-up belongs downstream, on the limiter.
        for volume in (0.0, -6.0, -12.0, -40.0):
            controls = speaker_calibrate.loudness_controls(volume, True, 1.5)
            self.assertEqual(controls["loudcomp:input"], 1.0)
        quiet = speaker_calibrate.loudness_controls(-12.0, True, 1.5)
        self.assertGreater(quiet["limiter:g_in"], 1.5)

    def test_a_quieter_setting_asks_for_a_lower_contour(self):
        loud = speaker_calibrate.loudness_controls(0.0, True)
        quiet = speaker_calibrate.loudness_controls(-9.0, True)
        self.assertLess(quiet["loudcomp:volume"], loud["loudcomp:volume"])
        # Full volume is the reference, so there it asks for nothing at all.
        self.assertEqual(loud["loudcomp:volume"], 0.0)
        self.assertEqual(loud["limiter:g_in"], 1.0)

    def test_the_contour_stops_at_the_floor(self):
        controls = speaker_calibrate.loudness_controls(-90.0, True)
        self.assertEqual(controls["loudcomp:volume"], speaker_calibrate.LOUDNESS_FLOOR_DB)
        self.assertAlmostEqual(self.net_gain_db(controls), 0.0, places=4)

    def test_the_contour_is_never_asked_for_more_than_the_volume_freed(self):
        # The volume reading understates how quiet it is, so the contour is
        # taken deeper than the reading; never deeper than the volume has
        # actually come down, though.  That is what keeps the lift inside the
        # headroom the attenuation freed, and full volume untouched.
        for volume in (0.0, -1.0, -3.0, -12.0, -30.0, -60.0):
            contour = speaker_calibrate.loudness_level_db(volume)
            extra = volume - contour
            self.assertLessEqual(
                extra,
                min(speaker_calibrate.LOUDNESS_EXTRA_DEPTH_DB, max(0.0, -volume)) + 1e-9,
            )
            # Past the floor the contour is shallower than the volume rather
            # than deeper, which asks for less than the volume freed, not more.
            self.assertGreaterEqual(contour, speaker_calibrate.LOUDNESS_FLOOR_DB)
            self.assertLessEqual(contour, 0.0)

    def test_it_follows_the_device_the_volume_keys_move(self):
        # The volume keys resolve through this filter to the device behind it,
        # so the filter's own volume never moves.  Following it left the
        # contour flat at every setting, which is why nothing was audible.
        profile = {"speaker": {"name": "alsa_output.pci-0000_00_1f.3.analog-stereo"}}
        self.assertEqual(
            speaker_calibrate.listening_sink(profile),
            "alsa_output.pci-0000_00_1f.3.analog-stereo",
        )
        self.assertNotEqual(
            speaker_calibrate.listening_sink(profile), speaker_calibrate.VIRTUAL_SINK
        )

    def test_without_a_profile_it_falls_back_to_the_filter(self):
        # Nothing else is known then, and a wrong contour is worse than none.
        self.assertEqual(
            speaker_calibrate.listening_sink({}), speaker_calibrate.VIRTUAL_SINK
        )

    def test_full_volume_is_left_alone(self):
        # There is no headroom for a boost at full scale, and nothing to
        # compensate either: the reference is what full volume is.
        controls = speaker_calibrate.loudness_controls(0.0, True, 1.663413)
        self.assertEqual(controls["loudcomp:volume"], 0.0)
        self.assertAlmostEqual(controls["limiter:g_in"], 1.663413, places=5)

    def test_switched_off_it_is_inert(self):
        controls = speaker_calibrate.loudness_controls(-30.0, False, 1.663413)
        self.assertEqual(controls["loudcomp:enabled"], 0.0)
        self.assertEqual(controls["loudcomp:input"], 1.0)
        self.assertEqual(controls["loudcomp:volume"], 0.0)
        # And the calibrated gain is handed back untouched.
        self.assertAlmostEqual(controls["limiter:g_in"], 1.663413, places=5)

    def test_it_uses_the_current_equal_loudness_standard(self):
        controls = speaker_calibrate.loudness_controls(-10.0, True)
        self.assertEqual(controls["loudcomp:std"], 4.0)   # ISO 226:2023
        self.assertEqual(controls["loudcomp:mode"], 1.0)  # IIR, so no added latency

    def test_the_graph_always_carries_it_so_it_can_be_switched_live(self):
        fit = {"filters": [], "input_gain_linear": 1.0}
        off = speaker_calibrate.graph_controls(fit, bass_enhancer=False)
        on = speaker_calibrate.graph_controls(
            fit, bass_enhancer=False, loudness_compensation=True, sink_volume_db=-12.0
        )
        self.assertEqual(set(off), set(on))
        self.assertEqual(off["loudcomp:enabled"], 0.0)
        self.assertEqual(on["loudcomp:enabled"], 1.0)
        # The limiter's gain carries the make-up, so it moves with it.
        self.assertGreater(on["limiter:g_in"], off["limiter:g_in"])
        # Nothing else moves, so switching it is a control change, not a rebuild.
        skip = ("loudcomp:", "limiter:g_in")
        self.assertEqual(
            {k: v for k, v in off.items() if not k.startswith(skip)},
            {k: v for k, v in on.items() if not k.startswith(skip)},
        )

    def test_bypassing_the_calibration_also_flattens_it(self):
        controls = speaker_calibrate.transparent_controls(bass_enhancer=False)
        self.assertEqual(controls["loudcomp:enabled"], 0.0)
        self.assertEqual(controls["loudcomp:input"], 1.0)


class MeasurementSupportTests(unittest.TestCase):
    """Omarchy ships neither numpy nor scipy, so their absence is a state."""

    def test_it_reports_what_is_missing_and_how_to_get_it(self):
        support = speaker_calibrate.measurement_support()
        self.assertEqual(set(support["packages"]), {"python-numpy", "python-scipy"})
        self.assertTrue(support["command"].startswith("omarchy pkg add"))
        # Both packages are named in the command, whichever is missing.
        for package in support["packages"]:
            self.assertIn(package, support["command"])

    def test_available_matches_what_can_actually_be_imported(self):
        support = speaker_calibrate.measurement_support()
        try:
            import numpy  # noqa: F401
            import scipy  # noqa: F401
            importable = True
        except Exception:
            importable = False
        self.assertEqual(support["available"], importable)
        self.assertEqual(support["missing"] == [], importable)

    def test_measuring_without_it_explains_itself(self):
        # A press of Calibrate on a fresh machine used to end in an ImportError
        # traceback; it has to say what is missing and how to get it.
        saved = speaker_calibrate.measurement_support
        speaker_calibrate.measurement_support = lambda: {
            "available": False, "missing": ["python-numpy"],
            "packages": ["python-numpy", "python-scipy"],
            "command": "omarchy pkg add python-numpy python-scipy",
        }
        had_np = "np" in speaker_calibrate.__dict__
        np_value = speaker_calibrate.__dict__.pop("np", None)
        try:
            with self.assertRaises(SystemExit) as caught:
                speaker_calibrate.load_dsp()
            self.assertIn("python-numpy", str(caught.exception))
            self.assertIn("omarchy pkg add", str(caught.exception))
        finally:
            speaker_calibrate.measurement_support = saved
            if had_np:
                speaker_calibrate.__dict__["np"] = np_value


class MicrophoneComparisonTests(unittest.TestCase):
    """Two microphones, put side by side by shape rather than by level."""

    def _compare(self, directory):
        """Run the comparison against this directory and nothing else.

        The profiles are pinned away as well: the backfill reads them, and a
        test that quietly depends on whatever the developer last measured is
        not a test.
        """
        saved = (speaker_calibrate.MICROPHONE_ARCHIVE,
                 speaker_calibrate.PROFILE, speaker_calibrate.PREVIOUS_PROFILE)
        speaker_calibrate.MICROPHONE_ARCHIVE = directory
        speaker_calibrate.PROFILE = directory / "no-such-profile.json"
        speaker_calibrate.PREVIOUS_PROFILE = directory / "no-such-previous.json"
        try:
            return speaker_calibrate.microphone_comparison()
        finally:
            (speaker_calibrate.MICROPHONE_ARCHIVE,
             speaker_calibrate.PROFILE,
             speaker_calibrate.PREVIOUS_PROFILE) = saved

    def _archive(self, directory, kind, response, frequencies):
        record = {
            "kind": kind, "created_at": "2026-01-01T00:00:00+00:00",
            "microphone": kind, "calibration_file": None,
            "frequency_hz": frequencies, "response_db": response,
            "uncertainty_db": None, "verdict": "pass", "speaker": "test",
        }
        (directory / f"{kind}.json").write_text(json.dumps(record))

    def test_it_reports_where_the_two_disagree(self):
        directory = Path(tempfile.mkdtemp())
        frequencies = [100.0, 150.0, 500.0, 1000.0, 3000.0, 8000.0]
        # The built-in reads 5 dB more bass; everything else agrees.
        self._archive(directory, "external", [0.0] * 6, frequencies)
        self._archive(directory, "internal", [5.0, 5.0, 0.0, 0.0, 0.0, 0.0], frequencies)
        result = self._compare(directory)
        self.assertTrue(result["available"])
        bands = {entry["band"]: entry["difference_db"] for entry in result["bands"]}
        self.assertAlmostEqual(bands["bass"], 5.0, places=2)
        self.assertAlmostEqual(bands["midrange"], 0.0, places=2)
        self.assertEqual(result["worst"]["band"], "bass")

    def test_a_device_cannot_name_itself_a_paragraph(self):
        # A USB microphone's product string is whatever its firmware says, and
        # it ends up in a row in the shell.
        self.assertEqual(len(speaker_calibrate.short_label("x" * 5000)),
                         speaker_calibrate.DEVICE_LABEL_LIMIT)
        self.assertEqual(speaker_calibrate.short_label("  Usb   Mic  "), "Usb Mic")
        self.assertIsNone(speaker_calibrate.short_label(None))
        self.assertIsNone(speaker_calibrate.short_label(""))

    def test_a_stored_curve_is_checked_before_it_is_drawn(self):
        good = {"frequency_hz": [100.0, 1000.0], "response_db": [0.0, 1.0]}
        self.assertIsNotNone(speaker_calibrate.valid_microphone_record(dict(good)))
        rejected = [
            {"frequency_hz": [100.0], "response_db": [0.0]},                 # too few
            {"frequency_hz": [100.0, 200.0], "response_db": [0.0]},          # mismatched
            {"frequency_hz": [100.0, 200.0], "response_db": [0.0, "x"]},     # not a number
            {"frequency_hz": [100.0, 200.0], "response_db": [0.0, float("inf")]},
            {"frequency_hz": [100.0, 200.0], "response_db": [0.0, float("nan")]},
            {"frequency_hz": "nope", "response_db": []},
            {},
        ]
        for record in rejected:
            self.assertIsNone(speaker_calibrate.valid_microphone_record(record), record)
        # And no more points than the analysis grid could ever hold.
        huge = speaker_calibrate.MICROPHONE_CURVE_LIMIT + 1
        self.assertIsNone(speaker_calibrate.valid_microphone_record(
            {"frequency_hz": [1.0] * huge, "response_db": [0.0] * huge}))

    def test_one_microphone_alone_is_not_a_comparison(self):
        directory = Path(tempfile.mkdtemp())
        self._archive(directory, "internal", [0.0, 0.0], [100.0, 1000.0])
        result = self._compare(directory)
        self.assertFalse(result["available"])
        self.assertIsNotNone(result["internal"])
        self.assertIsNone(result["external"])
        self.assertEqual(result["bands"], [])


class BoostBudgetTests(unittest.TestCase):
    """A boost is priced by what it asks of the speaker that was measured."""

    def test_the_cheap_band_follows_the_measured_corner(self):
        # Nothing here is decided in advance: a speaker measured down to 60 Hz
        # earns full allowance where a laptop cornering at 196 Hz does not.
        deep = boost_allowance_db(200.0, 60.0, 1.5)
        shallow = boost_allowance_db(200.0, 196.0, 1.5)
        self.assertAlmostEqual(float(deep), 1.5, places=6)
        self.assertLess(float(shallow), 0.5)

    def test_a_decibel_costs_more_the_lower_it_is_asked_for(self):
        # Excursion goes as the inverse square of frequency, so an octave down
        # is four times the ask.
        corner = 100.0
        near = excursion_weight(150.0, corner)
        octave_up = excursion_weight(300.0, corner)
        self.assertAlmostEqual(float(near) / float(octave_up), 4.0, places=6)

    def test_it_never_exceeds_what_the_measurement_justifies(self):
        # The budget can only ever tighten the trust cap, never loosen it.
        for cap in (1.5, 2.0, 3.0):
            for hz in (200.0, 500.0, 2000.0, 12000.0):
                allowed = float(boost_allowance_db(hz, 55.0, cap))
                self.assertLessEqual(allowed, cap + 1e-9)

    def test_a_filter_is_priced_where_it_could_move_to(self):
        # The centre is free inside a window, so pricing it at its starting
        # point would let it drift into a band the budget will not pay for.
        corner = 196.0
        window = _window_boost_limit(300.0, 900.0, corner, 1.5)
        self.assertLessEqual(
            window, float(boost_allowance_db(900.0, corner, 1.5))
        )
        self.assertAlmostEqual(
            window, float(boost_allowance_db(300.0, corner, 1.5)),
            places=6,
        )

    def test_the_weighting_stops_rather_than_running_away(self):
        # Far below the corner the arithmetic stops meaning anything; the
        # high-pass is already removing the band.
        weight = excursion_weight(1.0, 200.0)
        self.assertEqual(
            float(weight), EXCURSION_WEIGHT_CEILING
        )


if __name__ == "__main__":
    unittest.main()
