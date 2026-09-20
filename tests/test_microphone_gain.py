#!/usr/bin/env python3

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

SPEC = importlib.util.spec_from_file_location(
    "speaker_calibrate_gain", Path(__file__).resolve().parents[1] / "speaker-calibrate.py"
)
speaker_calibrate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(speaker_calibrate)

MIC = "alsa_input.pci-0000_00_1f.3.analog-stereo"
SINK = "alsa_output.pci-0000_00_1f.3.analog-stereo"


class FakePipeWire:
    """pactl as the helper sees it: one source whose volume can be read and set."""

    def __init__(self, values=(65536, 65536), fail=False):
        self.values = list(values)
        self.fail = fail
        self.calls = []

    def pactl_json(self, kind):
        if kind != "sources":
            return []
        return [{"name": MIC, "mute": False,
                 "volume": {f"ch{index}": {"value": value} for index, value in enumerate(self.values)}}]

    def run(self, args, *, check=True, capture=False):
        self.calls.append(args)
        if args[:2] == ["pactl", "set-source-volume"] and not self.fail:
            if args[3].endswith("dB"):
                factor = 10 ** (float(args[3][:-2]) / 60.0)      # PipeWire's cubic volume
                self.values = [max(1, round(value * factor)) for value in self.values]
            else:
                self.values = [int(value) for value in args[3:]]
        return subprocess.CompletedProcess(args, 1 if self.fail else 0, stdout="", stderr="")


class GainTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        data = Path(self.folder.name) / "data"
        data.mkdir(mode=0o700)
        self.pipewire = FakePipeWire()
        for name, value in (("DATA", data), ("MIC_GAIN_STATE", data / "microphone-volume-restore.json")):
            patch = mock.patch.object(speaker_calibrate, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        for name, value in (("pactl_json", self.pipewire.pactl_json), ("run", self.pipewire.run)):
            patch = mock.patch.object(speaker_calibrate, name, side_effect=value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(speaker_calibrate.time, "sleep")
        patch.start()
        self.addCleanup(patch.stop)


class MicrophoneGainTests(GainTestCase):
    def test_lowered_in_steps_and_put_back_exactly(self):
        self.pipewire.values = [65536, 52000]
        with speaker_calibrate.microphone_gain_managed(MIC) as gain:
            self.assertTrue(gain.lower("background-too-loud"))
            self.assertTrue(gain.lower("clipped-at-a-quiet-level"))
            self.assertEqual(gain.lowered_db, 24)
            self.assertLess(self.pipewire.values[0], 65536)
            record = json.loads(speaker_calibrate.MIC_GAIN_STATE.read_text())
            self.assertEqual(record["values"], [65536, 52000])
            self.assertEqual(record["microphone"], MIC)
        self.assertEqual(self.pipewire.values, [65536, 52000])
        self.assertFalse(speaker_calibrate.MIC_GAIN_STATE.exists())

    def test_never_more_than_three_steps(self):
        with speaker_calibrate.microphone_gain_managed(MIC) as gain:
            self.assertEqual([gain.lower("x") for _ in range(5)], [True, True, True, False, False])
            self.assertEqual(gain.lowered_db, 36)

    def test_a_failing_measurement_still_puts_it_back(self):
        with self.assertRaises(ValueError):
            with speaker_calibrate.microphone_gain_managed(MIC) as gain:
                gain.lower("background-too-loud")
                raise ValueError("the recorder died")
        self.assertEqual(self.pipewire.values, [65536, 65536])

    def test_untouched_means_nothing_written_and_nothing_set(self):
        with speaker_calibrate.microphone_gain_managed(MIC):
            pass
        self.assertEqual(self.pipewire.calls, [])
        self.assertFalse(speaker_calibrate.MIC_GAIN_STATE.exists())

    def test_a_volume_that_cannot_be_read_or_set_is_left_alone(self):
        with speaker_calibrate.microphone_gain_managed("alsa_input.usb-gone") as gain:
            self.assertFalse(gain.lower("x"))
        self.pipewire.fail = True
        with speaker_calibrate.microphone_gain_managed(MIC) as gain:
            self.assertFalse(gain.lower("x"))
            self.assertEqual(gain.lowered_db, 0)

    def test_a_killed_run_is_repaired_by_the_next_start(self):
        self.pipewire.values = [16461, 16461]
        speaker_calibrate.MIC_GAIN_STATE.write_text(json.dumps(
            {"pid": 2 ** 22 + 4321, "microphone": MIC, "values": [65536, 65536]}))
        speaker_calibrate.MIC_GAIN_STATE.chmod(0o600)
        self.assertTrue(speaker_calibrate.restore_microphone_volume())
        self.assertEqual(self.pipewire.values, [65536, 65536])
        self.assertFalse(speaker_calibrate.MIC_GAIN_STATE.exists())

    def test_a_record_of_a_run_still_alive_is_left_to_it(self):
        speaker_calibrate.MIC_GAIN_STATE.write_text(json.dumps(
            {"pid": speaker_calibrate.os.getpid(), "microphone": MIC, "values": [65536]}))
        speaker_calibrate.MIC_GAIN_STATE.chmod(0o600)
        self.assertFalse(speaker_calibrate.restore_microphone_volume())
        self.assertEqual(self.pipewire.calls, [])

    def test_a_record_someone_else_wrote_is_read_with_suspicion(self):
        for state in ({"pid": 1, "microphone": 'x" } ] context.exec', "values": [65536]},
                      {"pid": 2 ** 22 + 1, "microphone": MIC, "values": [10 ** 9]},
                      {"pid": 2 ** 22 + 1, "microphone": MIC, "values": ["-rf"]},
                      {"pid": 2 ** 22 + 1, "microphone": "-rf", "values": [65536]}, [1, 2]):
            speaker_calibrate.MIC_GAIN_STATE.write_text(json.dumps(state))
            speaker_calibrate.MIC_GAIN_STATE.chmod(0o600)
            speaker_calibrate.restore_microphone_volume()
            self.assertEqual([call for call in self.pipewire.calls if "set-source-volume" in call], [], state)


def search(status, selected=-24.0, attempts=None):
    return {"status": status, "selected_level_dbfs": selected, "level_bounds_dbfs": [-36.0, -6.0],
            "attempts": attempts or [{"level_dbfs": selected, "peak_dbfs": -9.0, "prominence_db": 30.0,
                                      "clipped_samples": 0, "background_rms_dbfs": -48.0}]}


class HotMicrophoneTests(unittest.TestCase):
    def test_what_points_at_too_much_gain(self):
        reason = speaker_calibrate.hot_microphone_reason
        for status in ("background-too-loud", "clipping-at-minimum-level", "limited-by-minimum-level"):
            self.assertEqual(reason(search(status), -12.0), status)
        clipped_then_quiet = search("converged", -32.6, [
            {"level_dbfs": -24.0, "clipped_samples": 125}, {"level_dbfs": -32.6, "clipped_samples": 0}])
        self.assertEqual(reason(clipped_then_quiet, -12.0), "clipped-at-a-quiet-level")

    def test_what_does_not(self):
        reason = speaker_calibrate.hot_microphone_reason
        for status in ("converged", "limited-by-maximum-level", "no-signal", "level-independent", "best-tested"):
            self.assertIsNone(reason(search(status, -12.0), -12.0), status)
        # One clipped probe on the way to a normal level is just the search working.
        normal = search("converged", -18.0, [{"level_dbfs": -12.0, "clipped_samples": 9},
                                             {"level_dbfs": -18.0, "clipped_samples": 0}])
        self.assertIsNone(reason(normal, -12.0))


class LevelSearchWithGainTests(GainTestCase):
    def find(self, outcomes, gain=True):
        results = iter(outcomes)
        with mock.patch.object(speaker_calibrate, "load_dsp"), \
             mock.patch.object(speaker_calibrate, "refuse_silenced_devices"), \
             mock.patch.object(speaker_calibrate, "refuse_playing_speakers"), \
             mock.patch.object(speaker_calibrate, "playing_applications", return_value=[]), \
             mock.patch.object(speaker_calibrate, "search_measurement_level",
                               side_effect=lambda *a, **k: dict(next(results)), create=True), \
             mock.patch.object(speaker_calibrate, "level_search_advice",
                               side_effect=lambda found: ([f"failed: {found['status']}"], []), create=True), \
             mock.patch.object(speaker_calibrate, "LEVEL_SEARCH_ABORT_STATUSES",
                               ("no-signal", "background-too-loud", "level-independent"), create=True), \
             mock.patch.object(speaker_calibrate, "linearity_probe_level", return_value=None, create=True), \
             mock.patch.object(speaker_calibrate, "level_linearity", return_value=None, create=True):
            with speaker_calibrate.microphone_gain_managed(MIC) as managed:
                found = speaker_calibrate.find_measurement_level(
                    SINK, MIC, "all", 2, gain=managed if gain else None)
                return found, list(self.pipewire.values)

    def test_a_hot_microphone_is_turned_down_until_the_search_settles(self):
        found, during = self.find([search("background-too-loud"), search("converged", -32.0, [
            {"level_dbfs": -24.0, "clipped_samples": 40, "prominence_db": 20.0},
            {"level_dbfs": -32.0, "clipped_samples": 0, "prominence_db": 14.0}]),
            search("limited-by-maximum-level", -6.0)])
        self.assertEqual(found["status"], "limited-by-maximum-level")
        self.assertEqual(found["microphone_gain_db"], -24)
        self.assertEqual(found["microphone_gain_reasons"], ["background-too-loud", "clipped-at-a-quiet-level"])
        self.assertLess(during[0], 65536)
        self.assertEqual(self.pipewire.values, [65536, 65536])          # and back afterwards

    def test_a_search_that_is_fine_touches_nothing(self):
        found, during = self.find([search("converged", -14.0)])
        self.assertNotIn("microphone_gain_db", found)
        self.assertEqual(self.pipewire.calls, [])

    def test_a_room_that_really_is_loud_still_fails_as_one(self):
        # Turned down, the background passes the gate on paper, but the probe
        # barely stands out: it was the room, and the first answer stands.
        quiet_on_paper = search("converged", -6.0, [{"level_dbfs": -6.0, "clipped_samples": 0,
                                                     "prominence_db": 7.0, "peak_dbfs": -20.0}])
        with self.assertRaisesRegex(ValueError, "background-too-loud"):
            self.find([search("background-too-loud"), quiet_on_paper])
        self.assertEqual(self.pipewire.values, [65536, 65536])

    def test_silence_is_not_a_reason_to_turn_the_microphone_down(self):
        with self.assertRaises(speaker_calibrate.NoSignal):
            self.find([search("no-signal")])
        self.assertEqual(self.pipewire.calls, [])

    def test_without_a_gain_handle_the_search_is_what_it_was(self):
        with self.assertRaisesRegex(ValueError, "background-too-loud"):
            self.find([search("background-too-loud")], gain=False)
        self.assertEqual(self.pipewire.calls, [])


class ResultTests(unittest.TestCase):
    def attach(self, level_search):
        measurement = {"quality": {"accepted": True, "verdict": "pass", "warnings": [], "guidance": []}}
        advice = (["The level search stopped at the loudest allowed sweep level."],
                  ["Raise the speaker hardware volume or the microphone input gain, then measure again."])
        with mock.patch.object(speaker_calibrate, "level_search_advice", return_value=advice, create=True):
            speaker_calibrate.attach_level_search(measurement, level_search)
        return measurement["quality"]

    def test_it_says_that_it_lowered_the_level_and_does_not_advise_raising_it(self):
        quality = self.attach(dict(search("limited-by-maximum-level", -6.0), microphone_gain_db=-24))
        self.assertEqual(quality["warnings"], [])
        self.assertEqual(len(quality["guidance"]), 1)
        self.assertIn("lowered by 24 dB for this measurement and put back afterwards", quality["guidance"][0])
        self.assertEqual(quality["verdict"], "pass")

    def test_a_signal_that_is_still_weak_keeps_its_advice(self):
        weak = search("limited-by-maximum-level", -6.0, [{"level_dbfs": -6.0, "peak_dbfs": -31.0}])
        quality = self.attach(dict(weak, microphone_gain_db=-12))
        self.assertEqual(len(quality["warnings"]), 1)
        self.assertEqual(len(quality["guidance"]), 2)

    def test_an_untouched_microphone_gets_no_such_line(self):
        quality = self.attach(search("limited-by-maximum-level", -6.0))
        self.assertFalse(any("lowered" in line for line in quality["guidance"]))


class PanelTests(unittest.TestCase):
    def test_a_failure_is_said_where_the_button_is(self):
        source = (Path(speaker_calibrate.__file__).parent / "Panel.qml").read_text()
        button = source.index('"Calibrate again"')
        said = source.index('text: service.error !== "" ? service.error', button)
        self.assertLess(said - button, 3000)
        self.assertEqual(source.count('visible: service.error !== ""'), 2)   # header text, and the offer

    def test_the_recorder_does_not_talk_into_the_helper_s_messages(self):
        helper = Path(speaker_calibrate.__file__).read_text()
        start = helper.index("def record_while_playing(")
        body = helper[start:helper.index("\ndef ", start)]
        for stream in ("stdout=subprocess.DEVNULL", "stderr=subprocess.DEVNULL"):
            self.assertIn(stream, body)


if __name__ == "__main__":
    unittest.main()
