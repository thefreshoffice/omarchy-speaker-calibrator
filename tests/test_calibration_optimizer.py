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
    def test_swap_files_exchanges_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "a.conf"
            second = Path(folder) / "b.conf"
            first.write_text("current graph")
            second.write_text("previous graph")
            speaker_calibrate.swap_files(first, second)
            self.assertEqual(first.read_text(), "previous graph")
            self.assertEqual(second.read_text(), "current graph")

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
            self.assertEqual(summary["label"], "2026-09-08 20:00 · 4 filters · flat")
            self.assertEqual(summary["plugin_version"], "0.8.0")
            self.assertIsNone(speaker_calibrate.profile_summary(Path(folder) / "missing.json"))


if __name__ == "__main__":
    unittest.main()
