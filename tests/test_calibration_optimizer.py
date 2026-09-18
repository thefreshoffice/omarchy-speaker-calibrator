#!/usr/bin/python3

import stat
import subprocess
import sys
import unittest
from unittest import mock
import importlib.util
import inspect
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
        graph = speaker_calibrate.filter_config("alsa_output.synthetic", fit)
        self.assertIn('"Freq" = 1000 "Q" = 1 "Gain" = -2.5', graph)
        self.assertIn('"Freq" = 2500 "Q" = 1.2 "Gain" = 0.75', graph)
        self.assertIn('"g_in" = 0.812345', graph)
        # One ASCII name everywhere: pactl's JSON output renders any non-ASCII
        # string as "(null)", which Omarchy's switcher then shows (issue #1).
        self.assertTrue(graph.isascii())
        self.assertIn('node.description = "Calibrated Speakers"', graph)
        self.assertIn('media.name = "Calibrated Speakers"', graph)
        capture = graph[graph.index("capture.props"):graph.index("playback.props")]
        self.assertIn('node.nick = "Calibrated Speakers"', capture)
        self.assertIn('node.description = "Calibrated Speakers"', capture)
        self.assertNotIn("Protected", graph)
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
        controls = speaker_calibrate.graph_controls(fit)
        slots = speaker_calibrate.PEAKING_SLOTS
        # Per channel: two high-passes, three shelves, the parametric slots,
        # and the balance trim's two controls; plus the limiter's input gain
        # and the compensator's six, which are not per channel.
        # ... plus the deep-bass path: three corners, a gain and its offset per channel.
        self.assertEqual(len(controls), 2 * (2 * 2 + 3 + 3 + 3 * slots + 3 + 2) + 1 + 6 + 10)
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
            self.assertEqual(summary["label"], "2026-09-08 20:00 · external mic · 4 filters · flat · normal bass · protected")
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
            level_match_db=-8.6
        )
        self.assertAlmostEqual(controls["limiter:g_in"], 10 ** (-8.6 / 20.0), places=5)
        # Everything else is still flat, so only the level differs.
        for name, value in controls.items():
            if name.endswith(":Gain"):
                self.assertEqual(value, 0.0, name)

    def test_a_measurement_flattens_without_the_match(self):
        # The speaker has to be measured as it is, not as the correction
        # leaves it, so the flattening used for a calibration is unity.
        controls = speaker_calibrate.transparent_controls()
        self.assertEqual(controls["limiter:g_in"], 1.0)

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
        })
        applied = []
        try:
            module.service_active = lambda: True
            module.tuning_node_id = lambda: 42
            module.compare_state = lambda: {"active": "current", "bypass": False}
            module.live_controls = lambda node_id: dict(installed, **{"limiter:grgv_l": 1.0})
            module.apply_controls_live = lambda controls: applied.append(dict(controls)) or True
            module.transparent_controls = lambda: original_transparent()
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
            "hb_out_l:Mult": 2.830895, "hb_out_r:Mult": 2.830895, "hb_lp_l:Freq": 195.8,
            "hb_out_l:Add": -1.415448, "hb_out_r:Add": -1.415448,
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
        self.assertEqual(applied[0]["hb_out_l:Mult"], 0.0)
        self.assertEqual(applied[0]["hb_out_r:Mult"], 0.0)
        # Only the deep-bass gain is touched: the filters under test stay as they are.
        self.assertNotIn("p1_l:Gain", applied[0])
        self.assertNotIn("limiter:g_in", applied[0])
        self.assertEqual(applied[1], {"hb_out_l:Mult": 2.830895, "hb_out_r:Mult": 2.830895,
                                      "hb_out_l:Add": -1.415448, "hb_out_r:Add": -1.415448})

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
                "hb_out_l:Mult": 0.0, "hb_out_r:Mult": 0.0,
                "hb_out_l:Add": 0.0, "hb_out_r:Add": 0.0, "loudcomp:enabled": 0.0,
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


class HarmonicBassTests(unittest.TestCase):
    """Deep bass built into the graph, in PipeWire's own nodes."""
    fit = {
        "filters": [{"type": "peaking", "frequency_hz": 900.0, "q": 1.2, "gain_db": -5.0}],
        "highpass": {"frequency_hz": 185.0, "q": 0.707, "stages": 1},
        "input_gain_linear": 0.9,
    }

    def test_the_path_sits_ahead_of_the_compensator_in_every_graph(self):
        graph = speaker_calibrate.filter_config("alsa_output.x", self.fit, deep_bass=True)
        for needle in ('name = hb_in_l', 'name = hb_mix_r', 'label = exp', 'label = log',
                       '{ output = "hb_mix_l:Out" input = "loudcomp:in_l" }',
                       '{ output = "hb_cl_r:Out" input = "hb_mix_r:In 1" }',
                       'inputs = [ "hb_in_l:In" "hb_in_r:In" ]'):
            self.assertIn(needle, graph, needle)
        self.assertNotIn("type = lv2 name = bass", graph)
        self.assertNotIn("chadmed", graph)
        for line in graph.splitlines():
            if "label = linear" in line:
                self.assertNotIn('"Mult" = -', line, line)

    def test_its_band_follows_the_measured_knee(self):
        controls = speaker_calibrate.graph_controls(self.fit, deep_bass=True)
        self.assertEqual(controls["hb_lp_l:Freq"], 185.0)
        self.assertEqual(controls["hb_fh_r:Freq"], 185.0)
        self.assertEqual(controls["hb_fl_l:Freq"], 555.0)
        self.assertAlmostEqual(controls["hb_out_l:Mult"],
                               2.0 * speaker_calibrate.HARMONIC_SCALE * speaker_calibrate.HARMONIC_AMOUNT, places=6)
        settings = speaker_calibrate.harmonic_settings(400.0)
        self.assertEqual(settings["ceil_hz"], speaker_calibrate.HARMONIC_MAX_HZ)

    def test_switching_it_off_changes_one_control_per_channel_and_nothing_else(self):
        on = speaker_calibrate.graph_controls(self.fit, deep_bass=True)
        off = speaker_calibrate.graph_controls(self.fit, deep_bass=False)
        self.assertEqual(set(on), set(off))
        self.assertEqual(off["hb_out_l:Mult"], 0.0)
        self.assertEqual(off["hb_out_r:Mult"], 0.0)
        self.assertEqual({k: v for k, v in on.items() if not k.startswith("hb_out_")},
                         {k: v for k, v in off.items() if not k.startswith("hb_out_")})
        self.assertEqual(speaker_calibrate.transparent_controls()["hb_out_r:Mult"], 0.0)

    def test_the_status_says_it_is_built_in(self):
        status = speaker_calibrate.harmonic_bass_status()
        self.assertTrue(status["usable"]); self.assertTrue(status["builtin"]); self.assertIsNone(status["package"])


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
        controls = speaker_calibrate.graph_controls(fit)
        self.assertAlmostEqual(controls["bal_l:Mult"], 10 ** (-2.0 / 20.0), places=5)
        self.assertEqual(controls["bal_r:Mult"], 1.0)
        graph = speaker_calibrate.filter_config(
            "alsa_output.x", fit
        )
        self.assertIn('name = bal_l label = linear control = { "Mult" = 0.7943', graph)

    def test_a_profile_without_a_trim_is_unity(self):
        controls = speaker_calibrate.graph_controls(
            {"filters": [], "input_gain_linear": 1.0}
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

    def test_the_make_up_pays_back_most_of_the_attenuation_not_all(self):
        # The contour attenuates the midband and lifts the bass.  Paying the
        # midband back in full made music two to three decibels louder with
        # the compensation on, because the lift is real energy, and drove bass
        # transients into the limiter.  The make-up is a fixed share of the
        # attenuation, so the loudness stays about where it was and the
        # limiter keeps the headroom the lift needs.
        share = speaker_calibrate.LOUDNESS_MAKEUP_SHARE
        self.assertLess(share, 1.0)
        self.assertGreater(share, 0.5)
        for volume in (0.0, -6.0, -9.4, -12.0, -35.0):
            for base in (1.0, 1.663413):
                controls = speaker_calibrate.loudness_controls(volume, True, base)
                contour = controls["loudcomp:volume"]
                expected_net = contour * (1.0 - share)
                self.assertAlmostEqual(self.net_gain_db(controls, base), expected_net, places=3)
                # And never louder than the attenuation would justify.
                self.assertLessEqual(self.net_gain_db(controls, base), 1e-6)

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
        floor = speaker_calibrate.LOUDNESS_FLOOR_DB
        self.assertEqual(controls["loudcomp:volume"], floor)
        # The make-up stops growing where the contour does, at its share.
        self.assertAlmostEqual(
            self.net_gain_db(controls),
            floor * (1.0 - speaker_calibrate.LOUDNESS_MAKEUP_SHARE), places=3)

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
        off = speaker_calibrate.graph_controls(fit)
        on = speaker_calibrate.graph_controls(
            fit, loudness_compensation=True, sink_volume_db=-12.0
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
        controls = speaker_calibrate.transparent_controls()
        self.assertEqual(controls["loudcomp:enabled"], 0.0)
        self.assertEqual(controls["loudcomp:input"], 1.0)


class MicrophoneCandidateTests(unittest.TestCase):
    """Only a real capture device can describe a loudspeaker."""

    def test_it_accepts_the_machine_s_own_inputs(self):
        for name in ("alsa_input.pci-0000_00_1f.3.analog-stereo",
                     "alsa_input.usb-Usb_Microphone_Usb_Microphone-00.mono-fallback"):
            self.assertTrue(speaker_calibrate.is_measurement_microphone(name), name)

    def test_it_refuses_a_bluetooth_headset(self):
        # HFP and HSP are mono and narrowband with gain control and noise
        # suppression inside the headset: a measurement through one describes
        # the headset, and the fit would correct the speakers to it.
        for name in ("bluez_input.80:C3:BA:81:E7:90",
                     "bluez_input.AA:BB:CC:DD:EE:FF.1"):
            self.assertFalse(speaker_calibrate.is_measurement_microphone(name), name)

    def test_it_refuses_anything_that_is_not_a_capture_device(self):
        for name in ("omarchy_speaker_tuning.monitor", "some.remote.source", "", None):
            self.assertFalse(speaker_calibrate.is_measurement_microphone(name), name)

    def test_the_speaker_side_already_had_this_rule(self):
        # The asymmetry was the bug: outputs were filtered, inputs were not.
        self.assertFalse(speaker_calibrate.is_physical_sink("bluez_output.80_C3_BA_81_E7_90.1"))
        self.assertTrue(speaker_calibrate.is_physical_sink("alsa_output.pci-0000_00_1f.3.analog-stereo"))


class MeasurementSupportTests(unittest.TestCase):
    """Omarchy ships neither numpy nor scipy, so their absence is a state."""

    def test_it_reports_what_is_missing_and_how_to_get_it(self):
        # Three things can be absent: the two Python packages measuring needs,
        # and the LV2 limiter the filter chain ends in.  Omarchy carries that
        # last one but installs it only where a shipped tuning applies.
        support = speaker_calibrate.measurement_support()
        self.assertEqual(
            set(support["packages"]),
            {"python-numpy", "python-scipy", "lsp-plugins-lv2"},
        )
        self.assertTrue(support["command"].startswith("omarchy pkg add"))
        # The command installs what is actually missing, not everything.
        for package in support["missing"]:
            self.assertIn(package, support["command"])

    def test_the_limiter_package_is_checked_by_its_own_file(self):
        # It is not a Python module, so importing proves nothing about it.
        self.assertTrue(str(speaker_calibrate.LIMITER_PROBE).endswith(".ttl"))
        support = speaker_calibrate.measurement_support()
        present = speaker_calibrate.LIMITER_PROBE.exists()
        self.assertEqual("lsp-plugins-lv2" not in support["missing"], present)

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

    def test_a_device_cannot_name_itself_markup(self):
        # A device name reaches a button label and the panel heading, and both
        # are drawn by shell components that decide for themselves whether a
        # string is markup.  Rich text fetches what an image tag points at.
        hostile = 'Mic <img src="http://10.0.0.1/leak.png">'
        cleaned = speaker_calibrate.short_label(hostile)
        for character in "<>&":
            self.assertNotIn(character, cleaned)
        self.assertIn("Mic", cleaned)

    def test_it_drops_characters_that_do_not_print(self):
        noisy = "name" + chr(0x200B) + "with" + chr(7) + "control"
        self.assertEqual(speaker_calibrate.short_label(noisy), "namewithcontrol")

    def test_a_missing_description_is_absent_not_the_word_null(self):
        # PipeWire reports "(null)" for a node with no description.
        for placeholder in ("(null)", "NULL", " none ", "unknown", "", "   "):
            self.assertIsNone(speaker_calibrate.short_label(placeholder), placeholder)

    def test_a_verdict_is_one_of_the_words_this_produces(self):
        # It goes into a section heading, which the shell draws.
        for good in ("pass", "warning", "fail", "inconclusive"):
            self.assertEqual(speaker_calibrate.safe_verdict(good), good)
        for bad in ("<img src=x>", "PASS", "", None, 3):
            self.assertIsNone(speaker_calibrate.safe_verdict(bad), bad)

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


class CheckAndProfileLabelTests(unittest.TestCase):
    helper = Path(__file__).resolve().parents[1] / "speaker-calibrate.py"

    def test_the_check_cannot_be_asked_to_use_another_microphone(self):
        # The check measures with the microphone the calibration was made
        # with, on its channels.  Neither the function nor the command line
        # takes anything that could point it elsewhere.
        self.assertEqual(
            list(inspect.signature(speaker_calibrate.verify_calibration).parameters), []
        )
        result = subprocess.run(
            [sys.executable, "-s", str(self.helper), "verify-json", "--channel", "0"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments: --channel", result.stderr)

    def test_a_profile_label_names_the_microphone_that_made_it(self):
        with tempfile.TemporaryDirectory() as folder:
            for internal, word in ((True, "internal mic"), (False, "external mic")):
                path = Path(folder) / f"{word[:8]}.json"
                path.write_text(json.dumps({
                    "created_at": "2026-09-09T20:46:00+00:00",
                    "microphone": {"name": "alsa_input.x", "internal": internal},
                    "fit": {"filter_count": 5},
                    "voicing": "neutral", "bass": "full", "loudness": "matched",
                }))
                summary = speaker_calibrate.profile_summary(path)
                self.assertEqual(summary["microphone_kind"], word.split()[0])
                self.assertTrue(
                    summary["label"].startswith(f"2026-09-09 20:46 · {word} · 5 filters"),
                    summary["label"],
                )


class SharedCalibrationTests(unittest.TestCase):
    """Exporting a calibration and loading one that somebody shared."""

    OURS = {"sys_vendor": "SLIMBOOK", "product_name": "Executive", "product_version": "",
            "product_sku": "EXE14", "board_name": "EXE14", "label": "SLIMBOOK Executive"}

    def profile(self, **changes):
        base = {
            "schema_version": 5, "plugin_version": "1.1.0", "created_at": "2026-09-16T10:00:00+00:00",
            "speaker": {"name": "alsa_output.pci-test.analog-stereo", "description": "Built-in Audio"},
            "microphone": {"name": "alsa_input.usb-test", "description": "Usb Microphone", "channel": 0,
                           "internal": False, "calibration_file": "/home/someone/mic.txt"},
            "voicing": "neutral", "loudness": "matched", "bass": "full", "deep_bass": "on",
            "loudness_compensation": "on",
            "quality": {"accepted": True, "verdict": "pass", "warnings": [], "guidance": []},
            "fit": {"filter_count": 2, "input_gain_linear": 0.8, "makeup_db": 2.0,
                    "filters": [{"type": "peaking", "frequency_hz": 600.0, "q": 2.6, "gain_db": -8.5},
                                {"type": "peaking", "frequency_hz": 2500.0, "q": 1.0, "gain_db": -6.0}],
                    "highpass": {"frequency_hz": 190.0, "q": 0.707, "stages": 1},
                    "bass_shelf": {"frequency_hz": 200.0, "q": 0.707, "gain_db": 3.0}},
        }
        base.update(changes)
        return base

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.downloads = root / "Downloads"; self.downloads.mkdir()
        self.data = root / "data"; self.data.mkdir(mode=0o700)
        self.patches = [
            mock.patch.object(speaker_calibrate, "DATA", self.data),
            mock.patch.object(speaker_calibrate, "PROFILE", self.data / "active-profile.json"),
            mock.patch.object(speaker_calibrate, "PROPOSAL", self.data / "proposed-profile.json"),
            mock.patch.object(speaker_calibrate, "share_directory", lambda: self.downloads),
            mock.patch.object(speaker_calibrate, "hardware_id", lambda: dict(self.OURS)),
            mock.patch.object(speaker_calibrate, "pactl_json",
                              lambda kind: [{"name": "alsa_output.pci-test.analog-stereo", "description": "Built-in Audio"}]),
            mock.patch.object(speaker_calibrate, "plugin_version", lambda: "1.1.0"),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(self.folder.cleanup)
        for patch in self.patches:
            self.addCleanup(patch.stop)

    def install_active(self, profile=None):
        (self.data / "active-profile.json").write_text(json.dumps(profile or self.profile()))

    def test_export_names_the_machine_and_carries_no_paths(self):
        self.install_active()
        result = speaker_calibrate.export_profile()
        path = self.downloads / result["file"]
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "slimbook-executive-external-mic-2026-09-16.speaker-calibration.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        # The Downloads folder itself is left as it was.
        self.assertEqual(stat.S_IMODE(self.downloads.stat().st_mode), 0o755)
        payload = json.loads(path.read_text())
        self.assertEqual(payload["format"], speaker_calibrate.SHARE_FORMAT)
        self.assertEqual(payload["name"], "SLIMBOOK Executive · external mic · 2026-09-16")
        self.assertEqual(payload["hardware"]["product_sku"], "EXE14")
        self.assertEqual(payload["hardware"]["speaker"], "alsa_output.pci-test.analog-stereo")
        self.assertIs(payload["profile"]["microphone"]["calibration_file"], True)
        self.assertNotIn("/home/", path.read_text())

    def test_a_shared_file_is_listed_loaded_and_matched(self):
        self.install_active()
        exported = speaker_calibrate.export_profile()
        listed = speaker_calibrate.shared_profiles()
        self.assertEqual([entry["file"] for entry in listed], [exported["file"]])
        self.assertTrue(listed[0]["valid"])
        self.assertEqual(listed[0]["matches"], {"machine": True, "speakers": True})
        result = speaker_calibrate.import_profile(name=exported["file"])
        self.assertIsNone(result["warning"])
        proposal = json.loads((self.data / "proposed-profile.json").read_text())
        self.assertEqual(proposal["imported"]["file"], exported["file"])
        self.assertEqual(proposal["imported"]["matches"], {"machine": True, "speakers": True})
        self.assertEqual(proposal["speaker"]["name"], "alsa_output.pci-test.analog-stereo")
        self.assertTrue(proposal["quality"]["accepted"])

    def test_other_hardware_is_warned_about_and_pointed_at_these_speakers(self):
        self.install_active()
        exported = speaker_calibrate.export_profile()
        theirs = {**self.OURS, "sys_vendor": "Dell Inc.", "product_name": "XPS 14", "product_sku": "0DB9",
                  "label": "Dell Inc. XPS 14"}
        with mock.patch.object(speaker_calibrate, "hardware_id", lambda: theirs), \
             mock.patch.object(speaker_calibrate, "pactl_json",
                               lambda kind: [{"name": "alsa_output.pci-xps.analog-stereo", "description": "XPS speakers"}]):
            (self.data / "active-profile.json").unlink()
            listed = speaker_calibrate.shared_profiles()
            self.assertEqual(listed[0]["matches"], {"machine": False, "speakers": False})
            result = speaker_calibrate.import_profile(name=exported["file"])
        self.assertIn("made on SLIMBOOK Executive; this is Dell Inc. XPS 14", result["warning"])
        self.assertEqual(result["proposal"]["speaker"]["name"], "alsa_output.pci-xps.analog-stereo")
        self.assertFalse(result["proposal"]["imported"]["matches"]["machine"])
        self.assertEqual(result["proposal"]["deep_bass"], "on")
        self.assertEqual(result["proposal"]["loudness_compensation"], "on")

    def test_hardware_matching_prefers_the_sku(self):
        matches = speaker_calibrate.hardware_matches
        self.assertTrue(matches({"product_sku": "A", "product_name": "X"}, {"product_sku": "A", "product_name": "Y"}))
        self.assertFalse(matches({"product_sku": "A", "product_name": "X"}, {"product_sku": "B", "product_name": "X"}))
        self.assertTrue(matches({"sys_vendor": "Slimbook", "product_name": "Executive"},
                                {"sys_vendor": "SLIMBOOK", "product_name": "executive", "product_sku": "Z"}))
        self.assertFalse(matches({"sys_vendor": "Slimbook"}, {"sys_vendor": "Slimbook"}))

    def write_shared(self, name, payload):
        (self.downloads / name).write_text(json.dumps(payload))

    def shared(self, **fit_changes):
        profile = self.profile()
        profile["fit"].update(fit_changes)
        return {"format": speaker_calibrate.SHARE_FORMAT, "name": "x", "hardware": dict(self.OURS), "profile": profile}

    def test_values_outside_the_protective_envelope_are_refused(self):
        valid = speaker_calibrate.valid_shared_payload
        valid(self.shared())
        for changes, reason in (
            ({"filters": [{"type": "peaking", "frequency_hz": 600.0, "q": 1.0, "gain_db": 30.0}]}, "gain"),
            ({"filters": [{"type": "peaking", "frequency_hz": 50000.0, "q": 1.0, "gain_db": -3.0}]}, "frequency"),
            ({"filters": [{"type": "allpass", "frequency_hz": 600.0, "q": 1.0, "gain_db": -3.0}]}, "unknown type"),
            ({"filters": [{"type": "peaking", "frequency_hz": 600.0, "q": 1.0, "gain_db": -3.0}] * 13}, "filters are expected"),
            ({"highpass": {"frequency_hz": 5000.0}}, "high-pass"),
            ({"channel_trim": {"left_db": 12.0, "right_db": 0.0}}, "trim"),
            ({"input_gain_linear": 9.0}, "input gain"),
            ({"input_gain_linear": float("nan")}, "not finite"),
        ):
            with self.assertRaisesRegex(ValueError, reason, msg=str(changes)):
                valid(self.shared(**changes))
        rejected = self.shared(); rejected["profile"]["quality"]["accepted"] = False
        with self.assertRaisesRegex(ValueError, "quality"):
            valid(rejected)
        with self.assertRaisesRegex(ValueError, "not a shared"):
            valid({"format": "something-else/1"})

    def test_bad_names_symlinks_and_oversized_files_are_refused(self):
        with self.assertRaises(SystemExit):
            speaker_calibrate.import_profile(name="../etc/passwd.speaker-calibration.json")
        with self.assertRaises(SystemExit):
            speaker_calibrate.import_profile(name="nope")
        victim = Path(self.folder.name) / "victim.json"; victim.write_text(json.dumps(self.shared()))
        (self.downloads / "link.speaker-calibration.json").symlink_to(victim)
        with self.assertRaises(SystemExit):
            speaker_calibrate.import_profile(name="link.speaker-calibration.json")
        self.assertEqual(speaker_calibrate.shared_profiles(), [])
        self.write_shared("big.speaker-calibration.json", self.shared())
        with mock.patch.object(speaker_calibrate, "SHARE_LIMIT_BYTES", 100):
            with self.assertRaises(SystemExit):
                speaker_calibrate.import_profile(name="big.speaker-calibration.json")
            listed = speaker_calibrate.shared_profiles()
        self.assertEqual([entry["valid"] for entry in listed], [False])


class LevelVariantTests(CalibrationOptimizerTests):
    """The bass and loudness switches without a refit."""

    def fitted(self, bass, loudness):
        response = -6.0 * np.exp(-((np.log(self.frequencies / 700.0)) ** 2) / 0.4) \
            - 12.0 * (self.frequencies < 150.0) * (150.0 - self.frequencies) / 150.0
        return optimize_peq(self.measurement(response), "neutral", internal_mic=False,
                            bass=bass, loudness=loudness)

    def test_every_combination_is_stored_and_the_chosen_one_is_the_fit(self):
        fit = self.fitted("full", "matched")
        self.assertEqual(set(fit["variants"]), {f"{b}/{l}" for b in ("normal", "full")
                                                for l in ("protected", "balanced", "matched")})
        chosen = fit["variants"]["full/matched"]
        for field in ("bass_shelf", "headroom_db", "boost_budget", "loudness_loss_db", "makeup_db",
                      "net_input_gain_db", "input_gain_linear", "predicted_response_db",
                      "correction_response_db", "weighted_rmse_after_db"):
            self.assertEqual(fit[field], chosen[field], field)
        self.assertIsNone(fit["variants"]["normal/matched"]["bass_shelf"])
        self.assertEqual(fit["variants"]["normal/protected"]["makeup_db"], 0.0)

    def test_a_stored_variant_is_what_a_refit_with_that_setting_gives(self):
        first = self.fitted("full", "matched")
        refit = self.fitted("normal", "protected")
        self.assertEqual(first["filters"], refit["filters"])
        variant = first["variants"]["normal/protected"]
        for field in ("bass_shelf", "headroom_db", "makeup_db", "net_input_gain_db",
                      "input_gain_linear", "correction_response_db", "predicted_response_db",
                      "weighted_rmse_after_db", "boost_budget"):
            self.assertEqual(refit[field], variant[field], field)

    def test_an_older_profile_gets_the_same_answer_on_the_fly(self):
        fit = self.fitted("full", "matched")
        measurement = self.measurement(np.zeros(self.frequencies.size))
        measurement["frequency_hz"] = self.frequencies.tolist()
        profile = {"fit": {k: v for k, v in fit.items() if k != "variants"}, "measurement": measurement,
                   "bass": "full", "loudness": "matched", "safety": {}}
        computed = speaker_calibrate.level_variant_for(profile, "normal", "protected")
        stored = fit["variants"]["normal/protected"]
        for field in ("headroom_db", "makeup_db", "net_input_gain_db", "input_gain_linear"):
            self.assertAlmostEqual(computed[field], stored[field], places=2, msg=field)
        self.assertIsNone(computed["bass_shelf"])
        self.assertLess(np.max(np.abs(np.asarray(computed["correction_response_db"])
                                      - np.asarray(stored["correction_response_db"]))), 0.01)
        speaker_calibrate.apply_level_variant(profile, "normal", "protected")
        self.assertEqual(profile["bass"], "normal")
        self.assertEqual(profile["loudness"], "protected")
        self.assertEqual(profile["fit"]["input_gain_linear"], computed["input_gain_linear"])
        self.assertEqual(profile["safety"]["input_trim_db"], -computed["headroom_db"])
        self.assertEqual(profile["safety"]["makeup_gain_db"], 0.0)


class VendorTuningTests(SharedCalibrationTests):
    """A calibration rendered as an Omarchy vendor tuning."""

    def test_coefficients_agree_with_the_magnitude_responses(self):
        from calibration_optimizer import (chain_response, group_delay_swing_ms, _peaking_response_db,
                                           _lowshelf_response_db, _highshelf_response_db, _highpass_response_db)
        f = np.geomspace(20.0, 20000.0, 300)
        for kind, freq, q, gain, reference in (
            ("peaking", 600.0, 2.6, -8.5, _peaking_response_db(f, 600.0, 2.6, -8.5, 48000)),
            ("lowshelf", 200.0, 0.707, 3.0, _lowshelf_response_db(f, 200.0, 0.707, 3.0, 48000)),
            ("highshelf", 6000.0, 1.5, -1.3, _highshelf_response_db(f, 6000.0, 1.5, -1.3, 48000)),
            ("highpass", 190.0, 0.707, 0.0, _highpass_response_db(f, 190.0, 0.707, 48000)),
        ):
            magnitude = 20.0 * np.log10(np.abs(chain_response([(kind, freq, q, gain)], f, 48000)))
            self.assertLess(float(np.max(np.abs(magnitude - reference))), 1e-6, kind)
        self.assertEqual(group_delay_swing_ms([], 48000), 0.0)
        swing = group_delay_swing_ms([("highpass", 60.9, 1.0, 0.0)] * 2 + [("peaking", 83.4, 1.8, -8.0)], 48000)
        self.assertGreater(swing, 1.0)
        self.assertLess(swing, 30.0)

    def test_sections_follow_omarchy_s_order_and_drop_idle_ones(self):
        fit = self.profile()["fit"]
        fit["filters"].append({"type": "peaking", "frequency_hz": 300.0, "q": 1.0, "gain_db": 0.0})
        fit["filters"].append({"type": "highshelf", "frequency_hz": 8000.0, "q": 0.7, "gain_db": -2.0})
        fit["highpass"]["stages"] = 2
        sections = speaker_calibrate.vendor_sections(fit)
        self.assertEqual([kind for kind, *_ in sections],
                         ["highpass", "highpass", "lowshelf", "peaking", "peaking", "highshelf"])
        self.assertEqual([round(freq) for _, freq, *_ in sections], [190, 190, 200, 600, 2500, 8000])

    def test_the_rendered_tuning_has_what_omarchy_checks_for(self):
        self.install_active(self.profile(deep_bass="off"))
        fake_metrics = {"bass_group_delay_swing_ms": 2.5, "limiter_headroom_db": 1.2, "peak_dbfs": -2.2,
                        "dynamic_range_delta_lu": 0.3, "signal": "pink noise"}
        with mock.patch.object(speaker_calibrate, "vendor_metrics", lambda *a, **k: fake_metrics):
            result = speaker_calibrate.vendor_tuning()
        folder = Path(result["directory"])
        self.assertEqual(folder, self.downloads / "omarchy-tuning-slimbook-executive")
        self.assertEqual(sorted(result["files"]), ["README.txt", "filter-chain.conf", "tuning.conf"])
        chain = (folder / "filter-chain.conf").read_text()
        for needle in ('name = s0_l', 'name = s0_r', 'label = bq_highpass', 'label = bq_lowshelf', 'label = bq_peaking',
                       'name   = limiter', '"alr"   = 0', '"boost" = 0', '"g_in"  = 0.8000', '"th"    = 0.891',
                       'node.name   = "omarchy_speaker_tuning"', 'target.object = "@SPEAKER_SINK@"',
                       'node.dont-move = true', 'node.dont-fallback = true', 'inputs  = [ "s0_l:In" "s0_r:In" ]',
                       'outputs = [ "limiter:out_l" "limiter:out_r" ]', '{ output = "s3_l:Out" input = "limiter:in_l" }'):
            self.assertIn(needle, chain, needle)
        self.assertNotIn("bankstown", chain)
        self.assertNotIn("loud_comp", chain)
        tuning = (folder / "tuning.conf").read_text()
        for needle in ('match_sku=("EXE14")', "sink_pattern='^alsa_output.pci-test.analog-stereo$'",
                       'description="SLIMBOOK Executive speakers"', 'bass_group_delay_swing_ms="2.5"',
                       'limiter_headroom_db="1.2"', 'dynamic_range_delta_lu="0.3"', 'validated_by=""',
                       'derived_from="Omarchy Speaker Calibrator'):
            self.assertIn(needle, tuning, needle)

    def test_deep_bass_becomes_bankstown_s_recipe_in_built_in_nodes(self):
        self.install_active(self.profile(deep_bass="on"))
        fake_metrics = {"bass_group_delay_swing_ms": 2.5, "limiter_headroom_db": 1.2, "peak_dbfs": -2.2,
                        "dynamic_range_delta_lu": 0.3, "signal": "pink noise"}
        with mock.patch.object(speaker_calibrate, "vendor_metrics", lambda *a, **k: fake_metrics):
            result = speaker_calibrate.vendor_tuning()
        chain = (Path(result["directory"]) / "filter-chain.conf").read_text()
        for needle in ('name = hb_in_l  label = copy', 'name = hb_cl_l  label = clamp        control = { "Min" = -10 "Max" = 10 }',
                       'label = bq_lowpass   control = { "Freq" = 190 "Q" = 0.707 }',   # ceil at the knee
                       'label = bq_lowpass   control = { "Freq" = 570 "Q" = 0.707 }',   # three times the knee
                       '"Mult" = 3.5 "Add" = 0', 'label = exp          control = { "Base" = 0.367879441 }',
                       'label = log          control = { "Base" = 2.718281828 "M1" = 1 "M2" = 1 }',
                       'inputs  = [ "hb_in_l:In" "hb_in_r:In" ]',
                       '{ output = "hb_cl_l:Out" input = "hb_mix_l:In 1" }', '{ output = "hb_fl_r:Out" input = "hb_mix_r:In 2" }',
                       '{ output = "hb_mix_l:Out" input = "s0_l:In" }', "own recipe, in built-in nodes"):
            self.assertIn(needle, chain, needle)
        scale = speaker_calibrate.HARMONIC_SCALE * speaker_calibrate.HARMONIC_AMOUNT
        self.assertIn(f'"Mult" = {2 * scale:.6f} "Add" = {-scale:.6f}', chain)
        # No negative multiplier anywhere: PipeWire's linear node drops the sign.
        for line in chain.splitlines():
            if "label = linear" in line:
                self.assertNotIn('"Mult" = -', line, line)
        # The node chain's arithmetic is a tanh with no constant left over: the
        # last stage computes 2 s - 1 == tanh(u), so silence in is silence out
        # and a graph that starts from zero state has nothing to step through.
        u = np.linspace(-17.0, 17.0, 2001)
        s = np.exp(-np.log(1.0 + np.exp(-2.0 * u)))
        self.assertLess(float(np.max(np.abs(2.0 * s - 1.0 - np.tanh(u)))), 1e-6)
        controls = speaker_calibrate.harmonic_controls(190.0, True)
        self.assertAlmostEqual(controls["hb_out_l:Add"], -0.5 * controls["hb_out_l:Mult"], places=5)
        off = speaker_calibrate.harmonic_controls(190.0, False)
        self.assertEqual((off["hb_out_l:Mult"], off["hb_out_l:Add"]), (0.0, 0.0))
        tuning = (Path(result["directory"]) / "tuning.conf").read_text()
        self.assertIn("Deep bass is included", tuning)

    def test_metrics_come_from_a_simulated_pass_through_the_chain(self):
        sections = speaker_calibrate.vendor_sections(self.profile()["fit"])
        with mock.patch.object(speaker_calibrate, "VENDOR_SIMULATION_SECONDS", 2.0):
            metrics = speaker_calibrate.vendor_metrics(sections, 0.8, 48000)
            harmonics = speaker_calibrate.vendor_harmonics(self.profile(deep_bass="on"))
            with_bass = speaker_calibrate.vendor_metrics(sections, 0.8, 48000, harmonics=harmonics)
        self.assertEqual(harmonics["ceil_hz"], 190.0)
        self.assertGreater(with_bass["peak_dbfs"], metrics["peak_dbfs"] - 0.01)
        self.assertGreater(metrics["bass_group_delay_swing_ms"], 0.0)
        self.assertLess(metrics["peak_dbfs"], 0.0)
        self.assertAlmostEqual(metrics["limiter_headroom_db"], -1.0 - metrics["peak_dbfs"], places=1)
        self.assertIn("pink noise", metrics["signal"])
        if metrics["dynamic_range_delta_lu"] is not None:
            self.assertLess(abs(metrics["dynamic_range_delta_lu"]), 10.0)


class InstallDefaultsTests(unittest.TestCase):
    def test_a_new_calibration_follows_the_volume_by_default(self):
        self.assertEqual(speaker_calibrate.LOUDNESS_COMPENSATION_DEFAULT, "on")

    def test_installing_starts_or_stops_the_tracker_to_match_the_profile(self):
        calls = []
        patches = [
            mock.patch.object(speaker_calibrate, "install_profile", lambda profile, graph: "live"),
            mock.patch.object(speaker_calibrate, "filter_config", lambda *a, **k: "graph"),
            mock.patch.object(speaker_calibrate, "sink_volume_db", lambda sink: 0.0),
            mock.patch.object(speaker_calibrate, "listening_sink", lambda profile: "sink"),
            mock.patch.object(speaker_calibrate, "start_loudness_tracker", lambda: calls.append("start")),
            mock.patch.object(speaker_calibrate, "stop_loudness_tracker", lambda: calls.append("stop")),
        ]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)
        base = {"quality": {"accepted": True}, "fit": {"filters": []}, "speaker": {"name": "s"}}
        speaker_calibrate.install_now({**base, "loudness_compensation": "on"})
        speaker_calibrate.install_now({**base, "loudness_compensation": "off"})
        self.assertEqual(calls, ["start", "stop"])


class PreviewDecisionTests(unittest.TestCase):
    """A new calibration plays and waits; apply or keep the previous."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); root = Path(self.folder.name)
        self.data = root / "data"; self.data.mkdir(0o700)
        self.paths = {name: self.data / f"{name}.json" for name in ("active-profile", "previous-profile", "held-profile", "held-previous", "compare-state")}
        self.activated = []
        def fake_install_now(profile):
            # What the real one does to the files: the current moves to previous, the new becomes current.
            current = speaker_calibrate.load_profile(speaker_calibrate.PROFILE)
            if current is not None:
                speaker_calibrate.write_atomic(speaker_calibrate.PREVIOUS_PROFILE, json.dumps(current))
            speaker_calibrate.write_atomic(speaker_calibrate.PROFILE, json.dumps(profile))
            self.activated.append(("install", profile["created_at"])); return dict(profile, installed=True)
        patches = [
            mock.patch.object(speaker_calibrate, "DATA", self.data),
            mock.patch.object(speaker_calibrate, "PROFILE", self.paths["active-profile"]),
            mock.patch.object(speaker_calibrate, "PREVIOUS_PROFILE", self.paths["previous-profile"]),
            mock.patch.object(speaker_calibrate, "HELD_PROFILE", self.paths["held-profile"]),
            mock.patch.object(speaker_calibrate, "HELD_PREVIOUS", self.paths["held-previous"]),
            mock.patch.object(speaker_calibrate, "COMPARE_STATE", self.paths["compare-state"]),
            mock.patch.object(speaker_calibrate, "install_now", fake_install_now),
            mock.patch.object(speaker_calibrate, "reinstall_profile", lambda profile: self.activated.append(("reinstall", profile["created_at"])) or "live"),
            mock.patch.object(speaker_calibrate, "compare_toggle", lambda: self.activated.append(("compare", None))),
        ]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)
        self.addCleanup(self.folder.cleanup)

    def profile(self, stamp):
        return {"created_at": stamp, "fit": {"filters": []}, "speaker": {"name": "s"}, "quality": {"accepted": True}}

    def test_the_first_calibration_installs_and_nothing_waits(self):
        result = speaker_calibrate.preview_install(self.profile("first"))
        self.assertNotIn("previewing", result)
        self.assertFalse(speaker_calibrate.previewing())

    def test_keeping_the_previous_puts_both_slots_back(self):
        self.paths["active-profile"].write_text(json.dumps(self.profile("old"))); self.paths["previous-profile"].write_text(json.dumps(self.profile("older")))
        result = speaker_calibrate.preview_install(self.profile("new"))
        self.assertTrue(result["previewing"]); self.assertTrue(speaker_calibrate.previewing())
        self.assertEqual(speaker_calibrate.load_profile(speaker_calibrate.PROFILE)["created_at"], "new")
        self.assertEqual(speaker_calibrate.load_profile(speaker_calibrate.PREVIOUS_PROFILE)["created_at"], "old")
        # A second measurement while waiting keeps the original held copies.
        speaker_calibrate.preview_install(self.profile("newer"))
        self.assertEqual(speaker_calibrate.load_profile(speaker_calibrate.HELD_PROFILE)["created_at"], "old")
        decided = speaker_calibrate.preview_discard()
        self.assertEqual(decided["profile"]["created_at"], "old")
        self.assertEqual(speaker_calibrate.load_profile(speaker_calibrate.PROFILE)["created_at"], "old")
        self.assertEqual(speaker_calibrate.load_profile(speaker_calibrate.PREVIOUS_PROFILE)["created_at"], "older")
        self.assertFalse(speaker_calibrate.previewing())
        self.assertEqual(self.activated[-1], ("reinstall", "old"))

    def test_applying_keeps_the_new_one_and_the_old_one_under_switch_profile(self):
        self.paths["active-profile"].write_text(json.dumps(self.profile("old")))
        speaker_calibrate.preview_install(self.profile("new"))
        speaker_calibrate.write_compare_state({"active": "previous", "bypass": False})
        decided = speaker_calibrate.preview_apply()
        self.assertEqual(decided["profile"]["created_at"], "new")
        self.assertEqual(speaker_calibrate.load_profile(speaker_calibrate.PREVIOUS_PROFILE)["created_at"], "old")
        self.assertFalse(speaker_calibrate.previewing())
        self.assertIn(("compare", None), self.activated)
        with self.assertRaises(SystemExit):
            speaker_calibrate.preview_apply()


class VendorTrialTests(unittest.TestCase):
    """Telling whose graph plays, and staging Omarchy's tree for a trial."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); root = Path(self.folder.name)
        self.data = root / "data"; self.data.mkdir(0o700)
        self.share = root / "omarchy"
        (self.share / "bin").mkdir(parents=True); (self.share / "bin" / "omarchy-hw-match").write_text("#!/bin/sh\n")
        (self.share / "default" / "audio" / "tunings" / "dell-xps-2026").mkdir(parents=True)
        (self.share / "default" / "audio" / "tunings" / "dell-xps-2026" / "tuning.conf").write_text("match_sku=(x)\n")
        (self.share / "default" / "audio" / "filter-chain-host.conf").write_text("host\n")
        (self.share / "default" / "systemd" / "user").mkdir(parents=True)
        (self.share / "default" / "systemd" / "user" / "omarchy-speaker-tuning.service").write_text("[Unit]\n")
        self.patches = [
            mock.patch.object(speaker_calibrate, "DATA", self.data),
            mock.patch.object(speaker_calibrate, "FRAGMENT", self.data / "90-tuning.conf"),
            mock.patch.object(speaker_calibrate, "VENDOR_TRIAL", self.data / "vendor-trial.json"),
            mock.patch.object(speaker_calibrate, "VENDOR_OVERLAY", self.data / "omarchy-path"),
            mock.patch.object(speaker_calibrate, "OMARCHY_SHARE", self.share),
        ]
        for patch in self.patches:
            patch.start(); self.addCleanup(patch.stop)
        self.addCleanup(self.folder.cleanup)

    def test_whose_graph_is_playing(self):
        fragment = self.data / "90-tuning.conf"
        self.assertEqual(speaker_calibrate.graph_kind(), "none")
        fragment.write_text("# Generated by Omarchy Speaker Calibrator. Boosts...\ncontext.modules = []\n")
        self.assertEqual(speaker_calibrate.graph_kind(), "calibrator")
        fragment.write_text("# SLIMBOOK Executive speaker tuning.\n#\n# Fitted by the Omarchy Speaker Calibrator 1.1.0 from...\n")
        self.assertEqual(speaker_calibrate.graph_kind(), "vendor-trial")
        fragment.write_text("# Dell XPS 14 / XPS 16 (2026) speaker tuning.\ncontext.modules = []\n")
        self.assertEqual(speaker_calibrate.graph_kind(), "other")
        (self.data / "vendor-trial.json").write_text('{"slug": "x"}')
        self.assertFalse(speaker_calibrate.vendor_trial_active())
        fragment.write_text("# x speaker tuning.\n# Fitted by the Omarchy Speaker Calibrator\n")
        self.assertTrue(speaker_calibrate.vendor_trial_active())

    def test_a_trial_beside_the_calibration_gets_its_own_names_and_the_real_target(self):
        chain = ('capture.props = { node.name = "omarchy_speaker_tuning" }\n'
                 'playback.props = { node.name = "omarchy_speaker_tuning_output" target.object = "@SPEAKER_SINK@" }\n'
                 'node.description = "Laptop Speakers"\n')
        graph = speaker_calibrate.trial_graph(chain, "alsa_output.pci-test.analog-stereo")
        self.assertIn('node.name = "omarchy_speaker_trial" ', graph)
        self.assertIn('node.name = "omarchy_speaker_trial_output"', graph)
        self.assertIn('target.object = "alsa_output.pci-test.analog-stereo"', graph)
        self.assertNotIn("omarchy_speaker_tuning", graph)
        self.assertNotIn("@SPEAKER_SINK@", graph)
        self.assertIn("omarchy-speaker-trial.conf", speaker_calibrate.TRIAL_UNIT_TEXT)
        self.assertNotIn("omarchy-speaker-tuning.conf", speaker_calibrate.TRIAL_UNIT_TEXT)

    def test_the_overlay_links_omarchy_and_holds_only_the_rendered_tuning(self):
        rendered = {"slug": "slimbook-executive", "tuning": 'description="x"\n', "chain": "context.modules = []\n"}
        overlay = speaker_calibrate.build_vendor_overlay(rendered)
        self.assertTrue((overlay / "bin").is_symlink())
        self.assertEqual((overlay / "bin").resolve(), (self.share / "bin").resolve())
        self.assertTrue((overlay / "default" / "systemd").is_symlink())
        self.assertTrue((overlay / "default" / "audio" / "filter-chain-host.conf").is_symlink())
        tunings = overlay / "default" / "audio" / "tunings"
        self.assertFalse(tunings.is_symlink())
        self.assertEqual(sorted(p.name for p in tunings.iterdir()), ["slimbook-executive"])
        conf = tunings / "slimbook-executive" / "tuning.conf"
        self.assertFalse(conf.is_symlink())
        self.assertEqual(conf.read_text(), 'description="x"\n')
        # Rebuilt from scratch on every trial.
        speaker_calibrate.build_vendor_overlay({**rendered, "slug": "other-machine"})
        self.assertEqual(sorted(p.name for p in tunings.iterdir()), ["other-machine"])



class FakeRecorder:
    """Stands in for the pw-record Popen object."""

    def __init__(self, *, hangs=False):
        self.signals = []
        self.killed = False
        self.hangs = hangs
        self.waits = 0

    def poll(self):
        return None if not self.signals and not self.killed else 0

    def send_signal(self, signum):
        self.signals.append(signum)

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waits += 1
        if self.hangs and not self.killed:
            raise subprocess.TimeoutExpired("pw-record", timeout)
        return 0


class PluginDirectoryHygieneTests(unittest.TestCase):
    """The helper must never write into the plugin directory (issue #2)."""

    def test_importing_the_helper_switches_bytecode_writing_off(self):
        self.assertTrue(sys.dont_write_bytecode)
        source = Path(speaker_calibrate.__file__).read_text()
        self.assertLess(source.index("sys.dont_write_bytecode = True"),
                        source.index("from calibration_io import"))

    def test_tracker_does_the_same_before_loading_the_helper(self):
        source = (Path(speaker_calibrate.__file__).parent / "loudness-tracker.py").read_text()
        self.assertLess(source.index("sys.dont_write_bytecode = True"), source.index("HELPER ="))


class RecorderCleanupTests(unittest.TestCase):
    """A cancelled or failed measurement leaves no pw-record behind (issue #1)."""

    def test_playback_failure_still_stops_the_recorder(self):
        recorder = FakeRecorder()
        with mock.patch.object(speaker_calibrate.subprocess, "Popen", return_value=recorder) as popen, \
                mock.patch.object(speaker_calibrate, "run", side_effect=subprocess.CalledProcessError(1, "pw-play")), \
                mock.patch.object(speaker_calibrate.time, "sleep"):
            with self.assertRaises(subprocess.CalledProcessError):
                speaker_calibrate.record_while_playing(
                    "alsa_output.synthetic", "mic", 1, Path("/tmp/p.wav"), Path("/tmp/r.wav"), 0, 0)
        self.assertEqual(recorder.signals, [speaker_calibrate.signal.SIGINT])
        self.assertEqual(recorder.waits, 1)
        # The recorder is told to follow the helper if the helper is killed outright.
        self.assertIs(popen.call_args.kwargs["preexec_fn"], speaker_calibrate.die_with_parent)

    def test_a_recorder_that_ignores_sigint_is_killed(self):
        recorder = FakeRecorder(hangs=True)
        speaker_calibrate.stop_recorder(recorder)
        self.assertEqual(recorder.signals, [speaker_calibrate.signal.SIGINT])
        self.assertTrue(recorder.killed)

    def test_an_already_finished_recorder_is_not_signalled(self):
        recorder = FakeRecorder()
        recorder.killed = True  # poll() reports it gone
        speaker_calibrate.stop_recorder(recorder)
        self.assertEqual(recorder.signals, [])

    def test_sigterm_becomes_a_normal_exit(self):
        with self.assertRaises(SystemExit) as caught:
            speaker_calibrate.exit_on_terminate(speaker_calibrate.signal.SIGTERM, None)
        self.assertEqual(caught.exception.code, 143)



def load_tracker():
    path = Path(speaker_calibrate.__file__).parent / "loudness-tracker.py"
    spec = importlib.util.spec_from_file_location("loudness_tracker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LoudnessRampTests(unittest.TestCase):
    """A volume change reaches the graph as a short ramp, not one jump."""

    def test_a_small_change_is_one_write_of_the_whole_target(self):
        start = speaker_calibrate.loudness_controls(-20.0, True, 1.0)
        target = speaker_calibrate.loudness_controls(-20.3, True, 1.0)
        self.assertEqual(speaker_calibrate.loudness_ramp(start, target), [target])

    def test_a_volume_step_is_walked_in_half_decibel_writes(self):
        # Levels above the compensator's floor, where a step still moves it;
        # in this range the contour level moves twice as far as the volume.
        start = speaker_calibrate.loudness_controls(-8.0, True, 1.0)
        target = speaker_calibrate.loudness_controls(-7.25, True, 1.0)
        ramp = speaker_calibrate.loudness_ramp(start, target)
        self.assertEqual(len(ramp), 3)
        self.assertEqual(ramp[-1], target)
        for write in ramp[:-1]:
            self.assertEqual(set(write), {"loudcomp:volume", "limiter:g_in"})
        volumes = [start["loudcomp:volume"]] + [w["loudcomp:volume"] for w in ramp]
        gains = [start["limiter:g_in"]] + [w["limiter:g_in"] for w in ramp]
        for before, after in zip(volumes, volumes[1:]):
            self.assertAlmostEqual(after - before, 0.5, places=2)
        # The make-up walks in decibels too, so the two never drift apart.
        for before, after in zip(gains, gains[1:]):
            self.assertAlmostEqual(20 * np.log10(after / before), 20 * np.log10(target["limiter:g_in"] / start["limiter:g_in"]) / 3, places=3)

    def test_a_large_jump_takes_bigger_steps_instead_of_lagging(self):
        start = speaker_calibrate.loudness_controls(-15.0, True, 1.0)
        target = speaker_calibrate.loudness_controls(0.0, True, 1.0)
        ramp = speaker_calibrate.loudness_ramp(start, target)
        self.assertEqual(len(ramp), speaker_calibrate.LOUDNESS_RAMP_MAX_WRITES)
        self.assertEqual(ramp[-1], target)


class FakeTrackerHelper:
    """The slice of the helper the tracker touches, with writes recorded."""

    def __init__(self):
        self.writes = []
        self.loudness_controls = speaker_calibrate.loudness_controls
        self.loudness_ramp = speaker_calibrate.loudness_ramp

    def tuning_node_id(self):
        return 7

    def write_controls(self, node, controls):
        self.writes.append((node, dict(controls)))
        return True


class TrackerRampTests(unittest.TestCase):
    def setUp(self):
        self.module = load_tracker()
        self.helper = FakeTrackerHelper()
        self.tracker = self.module.Tracker(self.helper)
        self.tracker.node = 7
        self.tracker.input_gain = 1.0

    def test_the_first_level_is_applied_in_one_write(self):
        with mock.patch.object(self.module.time, "sleep"):
            self.assertTrue(self.tracker.apply(-20.0))
        self.assertEqual(len(self.helper.writes), 1)
        self.assertEqual(self.tracker.applied_db, -20.0)

    def test_a_later_level_is_ramped_from_the_applied_one(self):
        self.tracker.applied_db = -8.0
        with mock.patch.object(self.module.time, "sleep") as sleep:
            self.assertTrue(self.tracker.apply(-6.5))
        expected = speaker_calibrate.loudness_ramp(
            speaker_calibrate.loudness_controls(-8.0, True, 1.0),
            speaker_calibrate.loudness_controls(-6.5, True, 1.0))
        self.assertGreater(len(expected), 1)
        self.assertEqual([w for _, w in self.helper.writes], expected)
        self.assertEqual(sleep.call_count, len(expected) - 1)
        self.assertEqual(self.tracker.applied_db, -6.5)

    def test_switching_off_is_immediate(self):
        self.tracker.applied_db = -20.0
        with mock.patch.object(self.module.time, "sleep"):
            self.assertTrue(self.tracker.apply(0.0, enabled=False))
        self.assertEqual(len(self.helper.writes), 1)
        self.assertIsNone(self.tracker.applied_db)

    def test_a_burst_of_volume_events_is_applied_once(self):
        import os
        read_end, write_end = os.pipe()
        os.write(write_end, b"Event 'change' on sink #59\n" * 5)
        os.close(write_end)

        class FakeSubscription:
            stdout = os.fdopen(read_end, "rb", buffering=0)
            def terminate(self): pass
            def wait(self, timeout=None): return 0
            def kill(self): pass

        followed = []
        self.tracker.follow_volume = lambda: followed.append(True) or True
        with mock.patch.object(self.module.subprocess, "Popen", return_value=FakeSubscription()):
            self.assertFalse(self.tracker.watch())  # the subscription ended
        self.assertEqual(followed, [True])



class MicrophoneArchiveIdentityTests(unittest.TestCase):
    """One record is kept per kind; the panel must know which device made it."""

    def test_the_summary_carries_the_device_name_when_the_record_has_it(self):
        directory = Path(tempfile.mkdtemp())
        (directory / "external.json").write_text(json.dumps({
            "kind": "external", "created_at": "2026-09-16T20:55:00+00:00",
            "microphone": "Usb Microphone Mono",
            "microphone_name": "alsa_input.usb-Usb_Microphone-00.mono-fallback",
            "calibration_file": None, "verdict": "warning",
        }))
        (directory / "internal.json").write_text(json.dumps({
            "kind": "internal", "created_at": "2026-09-09T20:26:00+00:00",
            "microphone": "Built-in Audio Analog Stereo", "calibration_file": None, "verdict": "pass",
        }))
        saved = speaker_calibrate.MICROPHONE_ARCHIVE
        speaker_calibrate.MICROPHONE_ARCHIVE = directory
        try:
            found = speaker_calibrate.archived_microphones()
        finally:
            speaker_calibrate.MICROPHONE_ARCHIVE = saved
        self.assertEqual(found["external"]["name"], "alsa_input.usb-Usb_Microphone-00.mono-fallback")
        self.assertEqual(found["external"]["microphone"], "Usb Microphone Mono")
        # A record from before the name was kept: the panel falls back to the label.
        self.assertEqual(found["internal"]["name"], "")
        self.assertEqual(found["internal"]["warnings"], [])

    def test_the_summary_carries_the_reasons_for_a_warning(self):
        directory = Path(tempfile.mkdtemp())
        (directory / "external.json").write_text(json.dumps({
            "kind": "external", "created_at": "2026-09-16T20:45:00+00:00", "microphone": "m",
            "microphone_name": "n", "calibration_file": None, "verdict": "warning",
            "warnings": ["The accepted test signal peaked at only -21.5 dBFS.", "<b>x</b>", 7],
        }))
        saved = speaker_calibrate.MICROPHONE_ARCHIVE
        speaker_calibrate.MICROPHONE_ARCHIVE = directory
        try:
            found = speaker_calibrate.archived_microphones()
        finally:
            speaker_calibrate.MICROPHONE_ARCHIVE = saved
        reasons = found["external"]["warnings"]
        self.assertEqual(reasons[0], "The accepted test signal peaked at only -21.5 dBFS.")
        self.assertEqual(len(reasons), 2)
        self.assertNotIn("<", reasons[1])

    def test_the_panel_matches_a_row_to_its_own_record_only(self):
        source = (Path(speaker_calibrate.__file__).parent / "Panel.qml").read_text()
        note = source[source.index("function microphoneRecord"):source.index("function calibrationMicrophone")]
        self.assertIn("record.name === entry.name", note)
        self.assertIn("record.microphone === entry.description", note)
        self.assertIn("active.name === entry.name", note)
        self.assertNotIn("active.internal === entry.internal", note)
        self.assertIn("record.warnings", note)


if __name__ == "__main__":
    unittest.main()
