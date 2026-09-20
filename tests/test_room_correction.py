#!/usr/bin/python3

import contextlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from scipy import signal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import calibration_dsp  # noqa: E402
from calibration_optimizer import optimize_peq, pleasant_in_room_target  # noqa: E402

_helper_spec = importlib.util.spec_from_file_location(
    "speaker_calibrate", Path(__file__).resolve().parents[1] / "speaker-calibrate.py"
)
speaker_calibrate = importlib.util.module_from_spec(_helper_spec)
_helper_spec.loader.exec_module(speaker_calibrate)

QUICK = "quick-calibration"
ROOM = "room-correction"

# Values fixed by the room correction design and the upstream safety caps.
ROOM_SWEEP_START_HZ = 20.0
QUICK_SWEEP_START_HZ = 70.0
ROOM_MAXIMUM_FILTERS = 20
QUICK_CALIBRATED_MAXIMUM_FILTERS = 10
ROOM_HIGHPASS_FLOOR_HZ = 20.0
QUICK_HIGHPASS_FLOOR_HZ = 50.0
TOTAL_CUT_CAP_DB = -15.0
BOOST_HEADROOM_CAP_DB = 3.0
MAKEUP_CAP_DB = 6.0
KNEE_SHORTFALL_DB = 15.0

BUILT_IN_SINK = {"name": "alsa_output.pci-0000_00_1f.3.analog-stereo",
                 "sample_specification": "s32le 2ch 48000Hz"}
EXTERNAL_SINK = {"name": "alsa_output.usb-Topping_DX3_Pro-00.analog-stereo",
                 "sample_specification": "s32le 2ch 48000Hz"}
BUILT_IN_MIC = {"name": "alsa_input.pci-0000_00_1f.3.analog-stereo",
                "sample_specification": "s16le 2ch 48000Hz"}
EXTERNAL_MIC = {"name": "alsa_input.usb-miniDSP_Umik-1_Gain__18dB_00002-00.analog-stereo",
                "sample_specification": "s16le 2ch 48000Hz"}
MIC_CAL_FILE = "/synthetic/umik-7001234.txt"


def narrow_mode(frequencies, center_hz, height_db, width_octaves):
    return height_db * np.exp(
        -0.5 * (np.log2(frequencies / center_hz) / width_octaves) ** 2
    )


class TestSinglePositionRoomCorrection(unittest.TestCase):
    """A single-position room correction run."""

    def setUp(self):
        self.frequencies = np.geomspace(20.0, 16_000.0, 240)
        self.target = pleasant_in_room_target(self.frequencies, "neutral") - 30.0

    def measurement(self, response):
        response = np.asarray(response, dtype=float)
        uncertainty = np.full(response.size, 0.2)
        return {
            "frequency_hz": self.frequencies.tolist(),
            "level_dbfs": response.tolist(),
            "rate_hz": 48_000,
            "microphone_calibration": {"path": MIC_CAL_FILE},
            "channels": [
                {
                    "output_channel": side,
                    "response_db": response.tolist(),
                    "uncertainty_db": uncertainty.tolist(),
                }
                for side in ("left", "right")
            ],
            "validation_curves": [
                {"repeat": repeat, "output_channel": "left",
                 "response_db": response.tolist()}
                for repeat in range(2)
            ],
        }

    def fit(self, response, mode, loudness="protected"):
        return optimize_peq(
            self.measurement(response), "neutral",
            internal_mic=False, loudness=loudness, mode=mode,
        )

    @contextlib.contextmanager
    def devices(self):
        with mock.patch.object(
            speaker_calibrate, "physical_sinks", lambda: [BUILT_IN_SINK, EXTERNAL_SINK]
        ), mock.patch.object(
            speaker_calibrate, "microphones", lambda: [BUILT_IN_MIC, EXTERNAL_MIC]
        ):
            yield

    def test_room_correction_is_refused_without_an_external_sink_and_mic(self):
        refused = [
            (BUILT_IN_SINK, EXTERNAL_MIC, MIC_CAL_FILE, "external"),
            (EXTERNAL_SINK, BUILT_IN_MIC, MIC_CAL_FILE, "external"),
        ]
        with self.devices():
            for sink, mic, cal_file, reason in refused:
                with mock.patch.object(speaker_calibrate, "build_profile") as build:
                    with self.assertRaises(SystemExit) as raised:
                        speaker_calibrate.calibrate_noninteractive(
                            sink["name"], mic["name"], "0", "neutral", cal_file,
                            mode=ROOM,
                        )
                    self.assertIn(reason, str(raised.exception).lower(), (sink, mic))
                    build.assert_not_called()

            with mock.patch.object(speaker_calibrate, "build_profile") as build:
                speaker_calibrate.calibrate_noninteractive(
                    EXTERNAL_SINK["name"], EXTERNAL_MIC["name"], "0", "neutral",
                    MIC_CAL_FILE, mode=ROOM,
                )
                build.assert_called_once()
                passed = list(build.call_args.args) + list(build.call_args.kwargs.values())
                self.assertIn(ROOM, passed)

            # Quick calibration of the built-in speakers is not affected.
            with mock.patch.object(speaker_calibrate, "build_profile") as build:
                speaker_calibrate.calibrate_noninteractive(
                    BUILT_IN_SINK["name"], BUILT_IN_MIC["name"], "0", "neutral", None,
                    mode=QUICK,
                )
                build.assert_called_once()

    def test_room_correction_sweeps_from_20_hz_at_the_same_level(self):
        speaker_calibrate.load_dsp()
        seen = {}

        def capture(mode, folder):
            specs, searches = [], []

            def analyze(recording, channel, schedule, spec, **_):
                specs.append(spec)
                return {"quality": {
                    "accepted": True, "verdict": "pass", "warnings": [], "guidance": [],
                    "metrics": {"clipped_samples": 0, "maximum_accepted_peak_dbfs": -20.0},
                }}

            def search(*args, **kwargs):
                searches.append((args, kwargs))
                return {"selected_level_dbfs": -33.0, "level_bounds_dbfs": [-51.0, -21.0]}

            with self.devices(), \
                    mock.patch.object(speaker_calibrate, "secure_directory"), \
                    mock.patch.object(speaker_calibrate, "record_while_playing"), \
                    mock.patch.object(speaker_calibrate, "attach_level_search"), \
                    mock.patch.object(speaker_calibrate, "find_measurement_level", search), \
                    mock.patch.object(speaker_calibrate, "analyze_recording", analyze):
                speaker_calibrate.capture_measurement(
                    EXTERNAL_SINK["name"], EXTERNAL_MIC["name"], 0, None,
                    sweeps=Path(folder) / f"{mode}-sweeps.wav",
                    recording=Path(folder) / f"{mode}-recording.wav",
                    mode=mode,
                )
            seen[mode] = (specs[-1], searches)

        with tempfile.TemporaryDirectory() as folder:
            capture(ROOM, folder)
            capture(QUICK, folder)

        room_spec, room_searches = seen[ROOM]
        quick_spec, quick_searches = seen[QUICK]
        self.assertAlmostEqual(room_spec.start_hz, ROOM_SWEEP_START_HZ, delta=1e-9)
        self.assertAlmostEqual(quick_spec.start_hz, QUICK_SWEEP_START_HZ, delta=1e-9)
        self.assertEqual(room_searches, quick_searches)
        self.assertAlmostEqual(room_spec.level_dbfs, quick_spec.level_dbfs, delta=1e-9)
        self.assertEqual(
            speaker_calibrate.default_sweep_level(EXTERNAL_SINK["name"]), -27.0
        )

    def test_room_correction_cuts_a_bass_mode_with_up_to_20_filters_and_the_same_caps(self):
        response = self.target + narrow_mode(self.frequencies, 45.0, 10.0, 0.05)
        room = self.fit(response, ROOM, loudness="matched")
        quick = self.fit(response, QUICK, loudness="matched")

        self.assertEqual(room["maximum_filter_count"], ROOM_MAXIMUM_FILTERS)
        self.assertEqual(quick["maximum_filter_count"], QUICK_CALIBRATED_MAXIMUM_FILTERS)
        self.assertLessEqual(room["filter_count"], ROOM_MAXIMUM_FILTERS)

        # The 45 Hz mode is cut by a section within a sixth of an octave of it.
        mode_cuts = [
            item for item in room["filters"]
            if item["type"] == "peaking" and item["gain_db"] <= -3.0
            and abs(np.log2(item["frequency_hz"] / 45.0)) <= 1.0 / 6.0
        ]
        self.assertTrue(mode_cuts, room["filters"])
        self.assertFalse(
            [item for item in quick["filters"] if item["frequency_hz"] < 100.0],
            quick["filters"],
        )

        # A speaker that plays to 20 Hz keeps its bass in room correction only.
        self.assertIsNone(room["highpass"]["knee_hz"])
        self.assertAlmostEqual(room["highpass_hz"], ROOM_HIGHPASS_FLOOR_HZ, delta=0.5)
        self.assertAlmostEqual(quick["highpass_hz"], QUICK_HIGHPASS_FLOOR_HZ, delta=0.5)

        for payload in (room, quick):
            self.assertAlmostEqual(payload["cut_limit_db"], TOTAL_CUT_CAP_DB, delta=1e-9)
            self.assertGreaterEqual(payload["deepest_correction_db"], TOTAL_CUT_CAP_DB - 0.05)
            self.assertAlmostEqual(
                payload["boost_budget"]["allowance_db"], BOOST_HEADROOM_CAP_DB, delta=1e-9
            )
            self.assertLessEqual(payload["actual_maximum_boost_db"], BOOST_HEADROOM_CAP_DB)
            self.assertLessEqual(payload["makeup_db"], MAKEUP_CAP_DB)

    def test_room_correction_boosts_only_above_the_bass_knee_and_cuts_below_it(self):
        roll_off = np.where(
            self.frequencies < 50.0, 24.0 * np.log2(50.0 / self.frequencies), 0.0
        )
        response = (
            self.target - roll_off + narrow_mode(self.frequencies, 26.0, 34.0, 0.06)
        )
        room = self.fit(response, ROOM)
        quick = self.fit(response, QUICK)
        knee = room["highpass"]["knee_hz"]

        # 24 dB/octave below 50 Hz falls 15 dB short at 50 / 2**(15/24) Hz.
        expected_knee = 50.0 / 2.0 ** (KNEE_SHORTFALL_DB / 24.0)
        self.assertIsNotNone(knee)
        self.assertAlmostEqual(knee, expected_knee, delta=8.0)
        self.assertGreaterEqual(room["highpass_hz"], ROOM_HIGHPASS_FLOOR_HZ)
        self.assertLess(room["highpass_hz"], QUICK_HIGHPASS_FLOOR_HZ)
        self.assertAlmostEqual(quick["highpass_hz"], QUICK_HIGHPASS_FLOOR_HZ, delta=0.5)

        for item in room["filters"]:
            if item["gain_db"] > 0.0:
                self.assertGreaterEqual(item["frequency_hz"], knee, item)
        below_knee_cuts = [
            item for item in room["filters"]
            if item["type"] == "peaking" and item["gain_db"] <= -2.0
            and item["frequency_hz"] < knee
            and abs(np.log2(item["frequency_hz"] / 26.0)) <= 1.0 / 6.0
        ]
        self.assertTrue(below_knee_cuts, room["filters"])

    def test_a_failed_room_correction_capture_is_kept_and_not_installed(self):
        failed = self.measurement(self.target)
        failed["quality"] = {
            "accepted": False, "verdict": "fail",
            "warnings": ["Room noise is too close to the sweep."], "guidance": [],
            "metrics": {"clipped_samples": 0},
        }
        with tempfile.TemporaryDirectory() as folder:
            proposal = Path(folder) / "proposed-profile.json"
            with self.devices(), \
                    mock.patch.object(speaker_calibrate, "PROPOSAL", proposal), \
                    mock.patch.object(speaker_calibrate, "PROFILE", Path(folder) / "active.json"), \
                    mock.patch.object(speaker_calibrate, "archive_measurement") as archive, \
                    mock.patch.object(speaker_calibrate, "correction_silenced",
                                      lambda: contextlib.nullcontext(False)), \
                    mock.patch.object(speaker_calibrate, "capture_measurement",
                                      lambda *args, **kwargs: failed), \
                    mock.patch.object(speaker_calibrate, "install_profile") as install:
                profile = speaker_calibrate.calibrate_noninteractive(
                    EXTERNAL_SINK["name"], EXTERNAL_MIC["name"], "0", "neutral",
                    MIC_CAL_FILE, mode=ROOM,
                )
                result = speaker_calibrate.install_if_accepted(profile)
                kept = proposal.read_text()
            self.assertEqual(result["mode"], ROOM)
            self.assertFalse(result["installed"])
            self.assertIsNone(result["fit"])
            install.assert_not_called()
            archive.assert_called_once()
            self.assertIn('"room-correction"', kept)
            self.assertIn("Room noise is too close to the sweep.", kept)


DEFAULT_POSITIONS = 5
MIDBAND_HZ = (250.0, 2000.0)


def power_average_db(curves):
    """Per-frequency power average, written out independently of the code."""
    powers = np.mean([10.0 ** (np.asarray(curve) / 10.0) for curve in curves], axis=0)
    return 10.0 * np.log10(powers)


class RoomRunFixture:
    """Devices, a scratch data directory, and captures for a room correction run."""

    def setUp(self):
        self.frequencies = np.geomspace(20.0, 16_000.0, 240)
        self.target = pleasant_in_room_target(self.frequencies, "neutral") - 30.0
        self.midband = (
            (self.frequencies >= MIDBAND_HZ[0]) & (self.frequencies <= MIDBAND_HZ[1])
        )

    def capture(self, response, position=None, *, warning=None):
        measurement = TestSinglePositionRoomCorrection.measurement(self, response)
        measurement["quality"] = {
            "accepted": warning is None,
            "verdict": "pass" if warning is None else "fail",
            "warnings": [] if warning is None else [warning],
            "guidance": [],
            "metrics": {"clipped_samples": 0},
        }
        if position is not None:
            measurement["position"] = position
        return measurement

    @contextlib.contextmanager
    def run_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            with TestSinglePositionRoomCorrection.devices(self), \
                    mock.patch.object(speaker_calibrate, "ROOM_RUN", folder / "room-run"), \
                    mock.patch.object(speaker_calibrate, "PROPOSAL", folder / "proposed.json"), \
                    mock.patch.object(speaker_calibrate, "PROFILE", folder / "active.json"), \
                    mock.patch.object(speaker_calibrate, "secure_directory"), \
                    mock.patch.object(speaker_calibrate, "archive_measurement"), \
                    mock.patch.object(speaker_calibrate, "correction_silenced",
                                      lambda: contextlib.nullcontext(False)):
                yield folder / "room-run"

    def start(self, **kwargs):
        return speaker_calibrate.start_room_run(
            EXTERNAL_SINK["name"], EXTERNAL_MIC["name"], "0", "neutral", MIC_CAL_FILE,
            **kwargs,
        )

    def run_positions(self, responses):
        """Measure each response as one position of the started run, then fit it."""
        captures = [self.capture(response) for response in responses]
        with mock.patch.object(
            speaker_calibrate, "capture_measurement", side_effect=captures
        ) as capture:
            for position in range(1, len(responses) + 1):
                speaker_calibrate.measure_room_position(position)
        for call in capture.call_args_list:
            self.assertEqual(call.kwargs.get("mode"), ROOM, call)
        return speaker_calibrate.fit_room_run()

    def deepest_cut_near(self, filters, center_hz, octaves):
        gains = [
            item["gain_db"] for item in filters
            if item["type"] == "peaking"
            and abs(np.log2(item["frequency_hz"] / center_hz)) <= octaves
        ]
        return min(gains, default=0.0)


class TestSpatialAverage(RoomRunFixture, unittest.TestCase):
    """A room correction run fitted to the spatial average."""

    def test_a_run_uses_5_positions_by_default_and_refuses_0_and_10(self):
        with self.run_folder() as folder:
            argv = ["speaker-calibrate.py", "room-start-json",
                    "--sink", EXTERNAL_SINK["name"], "--mic", EXTERNAL_MIC["name"],
                    "--mic-cal-file", MIC_CAL_FILE]
            output = io.StringIO()
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
                speaker_calibrate.main()
            started = json.loads(output.getvalue())
            self.assertEqual(started["positions"], DEFAULT_POSITIONS)
            self.assertEqual(started["mode"], ROOM)
            saved = json.loads((folder / "run.json").read_text())
            self.assertEqual(saved["positions"], DEFAULT_POSITIONS)
            self.assertEqual(self.start(positions=9)["positions"], 9)

            for count in (0, 10):
                with self.assertRaises(SystemExit) as raised:
                    self.start(positions=count)
                self.assertIn("1 to 9", str(raised.exception), count)
            self.assertEqual(json.loads((folder / "run.json").read_text())["positions"], 9)

            self.start(positions=3)
            with mock.patch.object(speaker_calibrate, "capture_measurement") as capture:
                for outside in (0, 4):
                    with self.assertRaises(SystemExit):
                        speaker_calibrate.measure_room_position(outside)
                capture.assert_not_called()

    def test_the_spatial_average_is_the_per_frequency_power_average(self):
        bass_peak = narrow_mode(self.frequencies, 60.0, 9.0, 0.2)
        bass_dip = -narrow_mode(self.frequencies, 90.0, 12.0, 0.2)
        treble_dip = -narrow_mode(self.frequencies, 6000.0, 8.0, 0.2)
        seat = self.target + bass_peak
        second = self.target + bass_dip
        # A different sweep level shifts a whole position; only its shape counts.
        third = self.target + treble_dip + 6.0
        averaged = calibration_dsp.spatial_average([
            self.capture(seat, 1), self.capture(second, 2), self.capture(third, 3),
        ])

        expected = power_average_db([seat, second, third - 6.0])
        level = np.asarray(averaged["level_dbfs"])
        np.testing.assert_allclose(level, expected, atol=0.05)
        for channel in averaged["channels"]:
            np.testing.assert_allclose(channel["response_db"], expected, atol=0.05)
        for single in (seat, second, third - 6.0):
            self.assertGreater(np.max(np.abs(level - single)), 1.0)
        self.assertEqual(averaged["positions"], [1, 2, 3])

    def test_a_mode_at_one_position_of_five_is_cut_less_than_a_mode_at_all_five(self):
        mode = narrow_mode(self.frequencies, 60.0, 10.0, 0.05)
        with self.run_folder():
            self.start()
            everywhere = self.run_positions([self.target + mode] * DEFAULT_POSITIONS)
            self.start()
            seat_only = self.run_positions(
                [self.target + mode] + [self.target] * (DEFAULT_POSITIONS - 1)
            )
        everywhere_cut = self.deepest_cut_near(everywhere["fit"]["filters"], 60.0, 1 / 6)
        seat_only_cut = self.deepest_cut_near(seat_only["fit"]["filters"], 60.0, 1 / 6)
        self.assertEqual(everywhere["mode"], ROOM)
        self.assertEqual(everywhere["measurement"]["positions"], [1, 2, 3, 4, 5])
        self.assertLessEqual(everywhere_cut, -3.0)
        self.assertGreater(seat_only_cut, everywhere_cut + 2.0)

    def test_a_filter_that_worsens_the_holdout_position_is_dropped(self):
        peak = narrow_mode(self.frequencies, 1000.0, 6.0, 0.15)

        def fit(with_peak):
            averaged = calibration_dsp.spatial_average([
                self.capture(self.target + (peak if has else 0.0), position)
                for position, has in enumerate(with_peak, start=1)
            ])
            return optimize_peq(averaged, "neutral", internal_mic=False, mode=ROOM)

        held_everywhere = fit([True] * 5)
        missing_at_holdout = fit([True, True, True, True, False])
        self.assertLessEqual(
            self.deepest_cut_near(held_everywhere["filters"], 1000.0, 1 / 3), -3.0
        )
        self.assertGreater(
            self.deepest_cut_near(missing_at_holdout["filters"], 1000.0, 1 / 3), -1.0
        )

    def test_every_capture_of_the_run_is_saved_with_its_position(self):
        offsets = [0.0, -1.0, -2.0, -3.0, -4.0]
        with self.run_folder() as folder:
            self.start()
            (folder / "position-5.json").write_text('{"position": 5, "stale": true}')
            self.start()
            self.assertFalse((folder / "position-5.json").exists())
            self.run_positions([self.target + offset for offset in offsets])
            for position, offset in enumerate(offsets, start=1):
                saved = json.loads((folder / f"position-{position}.json").read_text())
                self.assertEqual(saved["position"], position)
                np.testing.assert_allclose(
                    saved["measurement"]["level_dbfs"], self.target + offset, atol=1e-6
                )


NOISE_WARNING = "Room noise is too close to the sweep."
PASS, FAIL, UNMEASURED = "pass", "fail", "unmeasured"


class TestPartialRunInstall(RoomRunFixture, unittest.TestCase):
    """Which partial room correction runs may install."""

    def measure(self, position, capture):
        with mock.patch.object(
            speaker_calibrate, "capture_measurement", return_value=capture
        ):
            return speaker_calibrate.measure_room_position(position)

    def response(self, position):
        """A response per position whose shape differs above the midband."""
        return self.target + narrow_mode(self.frequencies, 3000.0 * position, 3.0, 0.1)

    def run_pattern(self, pattern):
        """Start a run of len(pattern) positions, measure them, fit and try to install."""
        self.start(positions=len(pattern))
        for position, outcome in enumerate(pattern, start=1):
            if outcome == UNMEASURED:
                continue
            warning = NOISE_WARNING if outcome == FAIL else None
            self.measure(position, self.capture(self.response(position), warning=warning))
        return self.install(speaker_calibrate.fit_room_run())

    def install(self, profile):
        with mock.patch.object(speaker_calibrate, "install_profile",
                               return_value="restart") as install, \
                mock.patch.object(speaker_calibrate, "sink_volume_db", return_value=0.0), \
                mock.patch.object(speaker_calibrate, "bass_enhancer_status",
                                  return_value={"usable": False}):
            result = speaker_calibrate.install_if_accepted(profile)
        self.assertEqual(install.called, result["installed"], result["quality"])
        return result

    def test_a_failed_position_offers_a_retake_that_replaces_it(self):
        failed_shape = self.target + narrow_mode(self.frequencies, 120.0, 12.0, 0.1)
        with self.run_folder() as folder:
            self.start(positions=3)
            self.measure(1, self.capture(self.response(1)))
            failed = self.measure(2, self.capture(failed_shape, warning=NOISE_WARNING))
            retaken = self.measure(2, self.capture(self.response(2)))
            self.measure(3, self.capture(self.response(3)))
            self.assertTrue(failed["retake"])
            self.assertEqual(failed["quality"]["verdict"], "fail")
            self.assertFalse(retaken["retake"])
            saved = json.loads((folder / "position-2.json").read_text())
            np.testing.assert_allclose(
                saved["measurement"]["level_dbfs"], self.response(2), atol=1e-6
            )
            result = self.install(speaker_calibrate.fit_room_run())

        self.assertTrue(result["installed"])
        self.assertEqual(result["measurement"]["positions"], [1, 2, 3])
        np.testing.assert_allclose(
            result["measurement"]["level_dbfs"],
            power_average_db([self.response(position) for position in (1, 2, 3)]),
            atol=0.05,
        )

    def test_a_skipped_failed_position_is_left_out_and_still_saved(self):
        with self.run_folder() as folder:
            result = self.run_pattern([PASS, PASS, FAIL, PASS, PASS])
            kept = json.loads((folder / "position-3.json").read_text())
        self.assertTrue(result["installed"])
        self.assertEqual(result["measurement"]["positions"], [1, 2, 4, 5])
        self.assertEqual(result["measurement"]["dropped_positions"], [3])
        np.testing.assert_allclose(
            result["measurement"]["level_dbfs"],
            power_average_db([self.response(position) for position in (1, 2, 4, 5)]),
            atol=0.05,
        )
        self.assertEqual(kept["position"], 3)
        self.assertFalse(kept["measurement"]["quality"]["accepted"])
        np.testing.assert_allclose(
            kept["measurement"]["level_dbfs"], self.response(3), atol=1e-6
        )

    def test_a_run_whose_seat_position_failed_installs_nothing(self):
        with self.run_folder():
            result = self.run_pattern([FAIL, PASS, PASS, PASS, PASS])
        self.assertFalse(result["installed"])
        self.assertIsNone(result["fit"])
        self.assertEqual(result["quality"]["verdict"], "fail")
        self.assertTrue(any("seat" in warning.lower()
                            for warning in result["quality"]["warnings"]))

    def test_a_run_installs_only_with_the_seat_and_half_the_positions_passing(self):
        cases = [
            ([PASS, FAIL, PASS, FAIL, PASS], True),
            ([PASS, PASS, PASS, UNMEASURED, UNMEASURED], True),
            ([PASS, PASS, FAIL, FAIL, FAIL], False),
            ([PASS, FAIL, FAIL, FAIL, PASS], False),
            ([PASS, PASS, UNMEASURED, UNMEASURED, UNMEASURED], False),
            ([PASS, FAIL, FAIL, PASS], True),
            ([PASS, FAIL, FAIL, FAIL], False),
            ([PASS], True),
        ]
        with self.run_folder():
            for pattern, installs in cases:
                result = self.run_pattern(pattern)
                self.assertEqual(result["installed"], installs, pattern)
                self.assertEqual(result["mode"], ROOM)
                if not installs:
                    self.assertIsNone(result["fit"], pattern)

    def test_a_run_that_installs_nothing_keeps_every_capture(self):
        pattern = [PASS, FAIL, FAIL, FAIL, UNMEASURED]
        with self.run_folder() as folder:
            result = self.run_pattern(pattern)
            proposal = json.loads(speaker_calibrate.PROPOSAL.read_text())
            kept = {
                position: json.loads((folder / f"position-{position}.json").read_text())
                for position in range(1, 5)
            }
            self.assertFalse((folder / "position-5.json").exists())
        self.assertFalse(result["installed"])
        self.assertEqual(proposal["mode"], ROOM)
        self.assertFalse(proposal["quality"]["accepted"])
        self.assertEqual(proposal["measurement"]["dropped_positions"], [2, 3, 4])
        for position, record in kept.items():
            self.assertEqual(record["position"], position)
            self.assertEqual(
                record["measurement"]["quality"]["accepted"], pattern[position - 1] == PASS
            )
            np.testing.assert_allclose(
                record["measurement"]["level_dbfs"], self.response(position), atol=1e-6
            )


RATE = 48_000
ONE_SAMPLE_MS = 1000.0 / RATE
MAX_TRIM_DB = 0.0


class TestArrivalAlignment(unittest.TestCase):
    """Arrival alignment from the seat position."""

    spec = calibration_dsp.SweepSpec(start_hz=20.0, seconds=1.5)

    def analysed(self, *, left_ms, right_ms, left_db=0.0, right_db=0.0, position=None):
        """A capture analysed by the real DSP, from speakers with known arrivals."""
        program, schedule = calibration_dsp.build_measurement_signal(self.spec)
        recorded = np.zeros(program.shape[0] + RATE // 2)
        rng = np.random.default_rng(position or 0)
        for side, arrival_ms, gain_db in ((0, left_ms, left_db), (1, right_ms, right_db)):
            impulse = np.zeros(int(0.1 * RATE))
            direct = int(round(arrival_ms * RATE / 1000.0))
            impulse[direct] = 0.4 * 10.0 ** (gain_db / 20.0)
            impulse[direct + 170] = 0.08 * 10.0 ** (gain_db / 20.0)
            played = signal.fftconvolve(program[:, side], impulse)
            recorded[:played.size] += played
        lead = np.zeros(int(0.9 * RATE))
        capture = np.concatenate((lead, recorded)) + rng.normal(0.0, 2e-5, lead.size + recorded.size)
        measurement = calibration_dsp.analyse_capture(
            capture, schedule, self.spec, record_lead_seconds=1.0, internal_mic=False,
        )
        measurement["microphone_calibration"] = {"path": MIC_CAL_FILE}
        if position is not None:
            measurement["position"] = position
        return measurement

    def fit(self, measurement, mode=ROOM):
        return optimize_peq(measurement, "neutral", internal_mic=False, mode=mode)

    def test_the_nearer_speaker_is_delayed_to_the_later_arrival(self):
        seat = self.analysed(left_ms=20.0, right_ms=22.0)
        delay = self.fit(seat)["channel_delay_ms"]
        self.assertAlmostEqual(delay["left"], 2.0, delta=ONE_SAMPLE_MS)
        self.assertAlmostEqual(delay["right"], 0.0, delta=ONE_SAMPLE_MS)

        mirrored = self.fit(self.analysed(left_ms=26.5, right_ms=21.0))["channel_delay_ms"]
        self.assertAlmostEqual(mirrored["left"], 0.0, delta=ONE_SAMPLE_MS)
        self.assertAlmostEqual(mirrored["right"], 5.5, delta=ONE_SAMPLE_MS)

    def test_only_the_seat_position_sets_the_alignment(self):
        seat = self.analysed(left_ms=20.0, right_ms=22.0, right_db=2.0, position=1)
        others = [
            self.analysed(left_ms=25.0, right_ms=22.0, left_db=3.0, position=2),
            self.analysed(left_ms=18.0, right_ms=18.0, left_db=1.0, position=3),
        ]
        run = self.fit(calibration_dsp.spatial_average([seat] + others))
        self.assertAlmostEqual(run["channel_delay_ms"]["left"], 2.0, delta=ONE_SAMPLE_MS)
        self.assertAlmostEqual(run["channel_delay_ms"]["right"], 0.0, delta=ONE_SAMPLE_MS)
        self.assertTrue(run["channel_trim"]["applied"], run["channel_trim"])
        self.assertAlmostEqual(run["channel_trim"]["right_db"], -2.0, delta=0.15)
        self.assertAlmostEqual(run["channel_trim"]["left_db"], 0.0, delta=1e-9)

    def test_only_the_louder_channel_is_trimmed(self):
        for left_db, right_db in ((0.0, 2.0), (1.5, 0.0)):
            trim = self.fit(self.analysed(
                left_ms=20.0, right_ms=20.0, left_db=left_db, right_db=right_db,
            ))["channel_trim"]
            self.assertTrue(trim["applied"], trim)
            self.assertAlmostEqual(trim["left_db"], min(right_db - left_db, 0.0), delta=0.15)
            self.assertAlmostEqual(trim["right_db"], min(left_db - right_db, 0.0), delta=0.15)
            self.assertAlmostEqual(max(trim["left_db"], trim["right_db"]), MAX_TRIM_DB, delta=1e-9)

    def test_the_installed_chain_carries_each_channel_delay(self):
        seat = self.analysed(left_ms=20.0, right_ms=23.0)
        graphs = {}
        for mode in (ROOM, QUICK):
            profile = {
                "mode": mode,
                "speaker": {"name": EXTERNAL_SINK["name"]},
                "quality": {"accepted": True},
                "fit": self.fit(seat, mode),
            }
            with mock.patch.object(speaker_calibrate, "install_profile",
                                   return_value="restart") as install, \
                    mock.patch.object(speaker_calibrate, "sink_volume_db", return_value=0.0), \
                    mock.patch.object(speaker_calibrate, "bass_enhancer_status",
                                      return_value={"usable": False}):
                speaker_calibrate.install_if_accepted(profile)
            graphs[mode] = (install.call_args.args[1], profile["fit"])

        for mode, (left_s, right_s) in ((ROOM, (0.003, 0.0)), (QUICK, (0.0, 0.0))):
            graph, fit = graphs[mode]
            delays = {
                side: float(value) for side, value in re.findall(
                    r'name = dly_([lr]) label = delay [^}]*control = \{ "Delay \(s\)" = ([0-9.]+) \}',
                    graph,
                )
            }
            self.assertAlmostEqual(delays["l"], left_s, delta=ONE_SAMPLE_MS / 1000.0)
            self.assertAlmostEqual(delays["r"], right_s, delta=ONE_SAMPLE_MS / 1000.0)
            for side in "lr":
                self.assertIn(f'{{ output = "bal_{side}:Out" input = "dly_{side}:In" }}', graph)
                self.assertIn(f'{{ output = "dly_{side}:Out" input = "limiter:in_{side}" }}', graph)
            controls = speaker_calibrate.graph_controls(fit, bass_enhancer=False)
            self.assertAlmostEqual(controls["dly_l:Delay (s)"], left_s, delta=ONE_SAMPLE_MS / 1000.0)
            self.assertAlmostEqual(controls["dly_r:Delay (s)"], right_s, delta=ONE_SAMPLE_MS / 1000.0)


HDMI_SINK = {"name": "alsa_output.pci-0000_00_1f.3.hdmi-stereo",
             "sample_specification": "s32le 2ch 48000Hz"}
SPDIF_SINK = {"name": "alsa_output.pci-0000_00_1f.3.iec958-stereo",
              "sample_specification": "s32le 2ch 48000Hz"}
UNNAMED_MIC = {
    "name": "alsa_input.usb-R__DE_Microphones_R__DE_NT-USB_Mini_00000001-00.mono-fallback",
    "description": "(null)",
    "sample_specification": "s16le 1ch 48000Hz",
    "properties": {
        "device.description": "(null)",
        "device.product.name": "(null)",
        "device.vendor.name": "RODE Microphones",
        "device.profile.description": "Mono",
    },
}
MISSING_CAL_WARNING = "calibration file"


class TestEverydayDevices(RoomRunFixture, unittest.TestCase):
    """Room correction with monitor outputs and uncalibrated mics."""

    measure = TestPartialRunInstall.measure
    response = TestPartialRunInstall.response
    run_pattern = TestPartialRunInstall.run_pattern
    install = TestPartialRunInstall.install

    def test_monitor_and_spdif_outputs_are_external_speakers(self):
        with mock.patch.object(speaker_calibrate, "physical_sinks",
                               lambda: [BUILT_IN_SINK, HDMI_SINK, SPDIF_SINK]), \
                mock.patch.object(speaker_calibrate, "microphones", lambda: [EXTERNAL_MIC]):
            listed = {item["name"]: item["internal"]
                      for item in speaker_calibrate.devices_payload()["sinks"]}
            self.assertEqual(listed, {BUILT_IN_SINK["name"]: True,
                                      HDMI_SINK["name"]: False,
                                      SPDIF_SINK["name"]: False})
            for sink in (HDMI_SINK, SPDIF_SINK):
                self.assertAlmostEqual(
                    speaker_calibrate.default_sweep_level(sink["name"]), -27.0, delta=1e-9
                )
                with mock.patch.object(speaker_calibrate, "build_profile") as build:
                    speaker_calibrate.calibrate_noninteractive(
                        sink["name"], EXTERNAL_MIC["name"], "0", "neutral", None, mode=ROOM,
                    )
                    build.assert_called_once()
        self.assertAlmostEqual(
            speaker_calibrate.default_sweep_level(BUILT_IN_SINK["name"]), -12.0, delta=1e-9
        )

    def uncalibrated(self, response, warning=None):
        capture = self.capture(response, warning=warning)
        capture["microphone_calibration"] = None
        return capture

    def test_a_run_without_a_calibration_file_installs_with_a_warning(self):
        with self.run_folder():
            argv = ["speaker-calibrate.py", "room-start-json",
                    "--sink", EXTERNAL_SINK["name"], "--mic", EXTERNAL_MIC["name"],
                    "--positions", "2"]
            output = io.StringIO()
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
                speaker_calibrate.main()
            self.assertIsNone(json.loads(output.getvalue())["mic_cal_file"])
            for position in (1, 2):
                self.measure(position, self.uncalibrated(self.response(position)))
            result = self.install(speaker_calibrate.fit_room_run())
        warnings = result["quality"]["warnings"]
        self.assertTrue(result["installed"])
        self.assertEqual(result["quality"]["verdict"], "warning")
        self.assertIsNone(result["microphone"]["calibration_file"])
        self.assertTrue(any(MISSING_CAL_WARNING in item.lower() for item in warnings), warnings)

    def test_a_run_with_a_calibration_file_has_no_such_warning(self):
        with self.run_folder():
            result = self.run_pattern([PASS, PASS])
        warnings = result["quality"]["warnings"]
        self.assertTrue(result["installed"])
        self.assertFalse(any(MISSING_CAL_WARNING in item.lower() for item in warnings), warnings)

    def test_a_device_named_null_is_listed_by_vendor_and_profile(self):
        with mock.patch.object(speaker_calibrate, "physical_sinks", lambda: []), \
                mock.patch.object(speaker_calibrate, "microphones", lambda: [UNNAMED_MIC]):
            listed = speaker_calibrate.devices_payload()["microphones"][0]["description"]
        self.assertNotIn("null", listed.lower())
        self.assertIn("RODE Microphones", listed)
        self.assertIn("Mono", listed)
        self.assertEqual(speaker_calibrate.label(UNNAMED_MIC), listed)


if __name__ == "__main__":
    unittest.main()
