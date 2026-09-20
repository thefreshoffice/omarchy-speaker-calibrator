#!/usr/bin/env python3

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

SPEC = importlib.util.spec_from_file_location(
    "speaker_calibrate_playing", Path(__file__).resolve().parents[1] / "speaker-calibrate.py"
)
speaker_calibrate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(speaker_calibrate)

SPEAKERS = "alsa_output.pci-0000_00_1f.3.analog-stereo"
SILENCE = [-180.0] * 18


class SpeakersPlayingTests(unittest.TestCase):
    def refuse(self, levels, sinks=(SPEAKERS,), streams=()):
        with mock.patch.object(speaker_calibrate, "speakers_output_levels", return_value=levels) as listened, \
             mock.patch.object(speaker_calibrate, "playing_applications", return_value=list(streams)):
            speaker_calibrate.refuse_playing_speakers(list(sinks))
        return listened

    def test_exact_silence_passes(self):
        self.refuse(SILENCE)

    def test_quiet_music_is_heard_long_before_a_microphone_would_notice(self):
        with self.assertRaisesRegex(speaker_calibrate.SpeakersPlaying, r"up to -43 dBFS at their output"):
            self.refuse([-43.0, -47.0, -52.0] * 6)

    def test_music_with_pauses_in_it_still_counts(self):
        with self.assertRaises(speaker_calibrate.SpeakersPlaying):
            self.refuse([-180.0] * 12 + [-30.0] * 6)

    def test_one_stray_block_is_not_music(self):
        # A notification's tail, or the monitor's own start, in a second of silence.
        self.refuse([-180.0] * 17 + [-40.0])

    def test_what_cannot_be_heard_does_not_block_a_measurement(self):
        self.refuse(None)
        self.refuse([])

    def test_the_message_says_open_not_playing(self):
        with self.assertRaises(speaker_calibrate.SpeakersPlaying) as caught:
            self.refuse([-30.0] * 18, streams=["cliamp", "Spotify"])
        self.assertIn("Audio streams open right now: cliamp, Spotify.", str(caught.exception))
        self.assertIn("nothing was measured", str(caught.exception))

    def test_each_sink_is_listened_to_once_and_missing_ones_are_skipped(self):
        listened = self.refuse(SILENCE, sinks=(SPEAKERS, None, SPEAKERS, ""))
        listened.assert_called_once_with(SPEAKERS)

    def test_the_level_search_listens_to_the_real_speakers_before_the_first_probe(self):
        order = []
        with mock.patch.object(speaker_calibrate, "refuse_silenced_devices",
                               side_effect=lambda *a: order.append("silenced")), \
             mock.patch.object(speaker_calibrate, "load_dsp"), \
             mock.patch.object(speaker_calibrate, "refuse_playing_speakers",
                               side_effect=speaker_calibrate.SpeakersPlaying("music")) as refused, \
             mock.patch.object(speaker_calibrate, "search_measurement_level", create=True) as searched:
            with self.assertRaises(speaker_calibrate.SpeakersPlaying):
                speaker_calibrate.find_measurement_level(
                    "omarchy_speaker_tuning", "alsa_input.usb-mic", 0, 1, level_sink=SPEAKERS)
        refused.assert_called_once_with([SPEAKERS])          # not the calibrated sink in front of them
        searched.assert_not_called()
        self.assertEqual(order, ["silenced"])

    def test_it_is_an_ordinary_measurement_failure_for_everything_above_it(self):
        self.assertTrue(issubclass(speaker_calibrate.SpeakersPlaying, ValueError))
        self.assertFalse(issubclass(speaker_calibrate.SpeakersPlaying, speaker_calibrate.NoSignal))

    def test_a_name_pipewire_would_not_give_is_never_handed_to_the_recorder(self):
        with mock.patch.object(speaker_calibrate.subprocess, "Popen") as started:
            with self.assertRaises(SystemExit):
                speaker_calibrate.speakers_output_levels('x" } ] context.exec')
        started.assert_not_called()


class PanelKeepsTheFailureTests(unittest.TestCase):
    def setUp(self):
        self.service = (Path(speaker_calibrate.__file__).parent / "Service.qml").read_text()
        start = self.service.index("  function start(operation, arguments) {")
        self.start = self.service[start:self.service.index("\n  }\n", start)]

    def test_what_the_panel_starts_by_itself_leaves_a_failure_on_the_screen(self):
        self.assertIn('readonly property var _ownPhases: ["status", "devices", "cache", "mics", "remember"]',
                      self.service)
        guarded = self.start.index("if (_ownPhases.indexOf(operation) < 0) {")
        cleared = self.start.index('error = ""')
        self.assertLess(guarded, cleared)
        self.assertEqual(self.start.count('error = ""'), 1)

    def test_something_the_user_starts_still_clears_it(self):
        for operation in ("measure", "install", "verify", "refit", "import"):
            self.assertNotIn(f'"{operation}"', self.service[self.service.index("_ownPhases:"):
                                                             self.service.index("function start(")])


if __name__ == "__main__":
    unittest.main()
