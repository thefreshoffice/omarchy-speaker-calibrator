#!/usr/bin/env python3

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

SPEC = importlib.util.spec_from_file_location(
    "speaker_calibrate_fallback", Path(__file__).resolve().parents[1] / "speaker-calibrate.py"
)
speaker_calibrate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(speaker_calibrate)
NoSignal = speaker_calibrate.NoSignal


def port(name, availability):
    return {"name": name, "type": "Mic", "availability": availability}


SINK = {"name": "alsa_output.pci-0000_04_00.6.analog-stereo", "description": "Speakers"}
# A Ryzen laptop as in pull request #14: the analog codec's capture is a
# headset jack, the real microphones are a separate digital array.
JACK = {"name": "alsa_input.pci-0000_04_00.6.HiFi__Mic2__source", "description": "Headset Microphone",
        "sample_specification": "s16le 2ch 48000Hz", "active_port": "[In] Mic2",
        "ports": [port("[In] Mic2", "not available")]}
ARRAY = {"name": "alsa_input.pci-0000_04_00.5.HiFi__Mic1__source", "description": "Digital Microphone",
         "sample_specification": "s16le 2ch 48000Hz", "active_port": "[In] Mic1",
         "ports": [port("[In] Mic1", "availability unknown")]}
# The same jack on a codec without jack detection: nothing says it is empty.
BLIND_JACK = dict(JACK, ports=[port("[In] Mic2", "availability unknown")])
WEBCAM = {"name": "alsa_input.usb-046d_HD_Pro_Webcam_C920-02.analog-stereo", "description": "HD Pro Webcam C920",
          "sample_specification": "s16le 2ch 32000Hz", "active_port": "analog-input-mic",
          "ports": [port("analog-input-mic", "availability unknown")]}
USB_MIC = {"name": "alsa_input.usb-Usb_Microphone-00.mono-fallback", "description": "Usb Microphone",
           "sample_specification": "s16le 1ch 48000Hz"}


class JackStateTests(unittest.TestCase):
    def test_only_a_positive_report_rules_a_source_out(self):
        self.assertFalse(speaker_calibrate.source_plugged_in(JACK))
        self.assertTrue(speaker_calibrate.source_plugged_in(ARRAY))
        self.assertTrue(speaker_calibrate.source_plugged_in(BLIND_JACK))
        self.assertTrue(speaker_calibrate.source_plugged_in(USB_MIC))            # no ports at all
        self.assertTrue(speaker_calibrate.source_plugged_in({"ports": "nonsense"}))

    def test_it_is_the_active_port_that_counts(self):
        # The Slimbook: one source, the internal microphone active, the empty
        # external jack beside it.  The source is alive.
        slimbook = {"active_port": "analog-input-internal-mic", "ports": [
            port("analog-input-internal-mic", "availability unknown"),
            port("analog-input-mic", "not available")]}
        self.assertTrue(speaker_calibrate.source_plugged_in(slimbook))
        self.assertFalse(speaker_calibrate.source_plugged_in(dict(slimbook, active_port="analog-input-mic")))

    def test_the_device_list_carries_it_for_microphones_only(self):
        default = subprocess.CompletedProcess([], 0, stdout="\n", stderr="")
        headphones = dict(SINK, active_port="hp", ports=[port("hp", "not available")])
        with mock.patch.object(speaker_calibrate, "pactl_json",
                               side_effect=lambda kind: [headphones] if kind == "sinks" else [JACK, ARRAY]), \
             mock.patch.object(speaker_calibrate, "run", return_value=default):
            payload = speaker_calibrate.devices_payload()
        self.assertEqual([item["available"] for item in payload["microphones"]], [False, True])
        self.assertEqual([item["available"] for item in payload["sinks"]], [True])


class FallbackTests(unittest.TestCase):
    def calibrate(self, microphones, requested, outcomes, channel="0"):
        """Run calibrate_noninteractive with build_profile answering per microphone name."""
        tried = []

        def build(sink, mic, channel, voicing, mic_cal_file, *rest):
            tried.append((mic["name"], channel, mic_cal_file))
            outcome = outcomes[mic["name"]]
            if isinstance(outcome, Exception):
                raise outcome
            return {"quality": {"accepted": True, "warnings": [], "guidance": ["Keep still."]},
                    "microphone": {"name": mic["name"]}}

        with mock.patch.object(speaker_calibrate, "physical_sinks", return_value=[SINK]), \
             mock.patch.object(speaker_calibrate, "microphones", return_value=microphones), \
             mock.patch.object(speaker_calibrate, "build_profile", side_effect=build):
            try:
                return speaker_calibrate.calibrate_noninteractive(
                    SINK["name"], requested["name"], channel, "neutral", "/cal/file.txt"), tried
            except SystemExit as stop:
                return stop, tried

    def test_an_empty_jack_is_not_even_probed_when_the_array_can_answer(self):
        profile, tried = self.calibrate([JACK, ARRAY, WEBCAM], JACK, {ARRAY["name"]: "ok"})
        self.assertEqual([name for name, _, _ in tried], [ARRAY["name"]])
        self.assertEqual(profile["microphone_fallback"],
                         {"requested": JACK["name"], "used": ARRAY["name"], "reason": "not-plugged-in"})
        self.assertEqual(profile["quality"]["warnings"], [])
        self.assertIn("Headset Microphone has nothing plugged in", profile["quality"]["guidance"][-1])
        self.assertIn("Digital Microphone", profile["quality"]["guidance"][-1])
        self.assertEqual(profile["quality"]["guidance"][0], "Keep still.")

    def test_without_jack_detection_the_probe_decides(self):
        profile, tried = self.calibrate([BLIND_JACK, ARRAY], BLIND_JACK,
                                        {BLIND_JACK["name"]: NoSignal("heard nothing"), ARRAY["name"]: "ok"})
        self.assertEqual([name for name, _, _ in tried], [BLIND_JACK["name"], ARRAY["name"]])
        self.assertEqual(profile["microphone_fallback"]["reason"], "no-signal")
        self.assertIn("did not pick up the level probe", profile["quality"]["guidance"][-1])

    def test_the_correction_file_stays_with_the_microphone_it_describes(self):
        _, tried = self.calibrate([BLIND_JACK, ARRAY], BLIND_JACK,
                                  {BLIND_JACK["name"]: NoSignal("x"), ARRAY["name"]: "ok"})
        self.assertEqual([cal for _, _, cal in tried], ["/cal/file.txt", None])

    def test_a_webcam_or_a_headset_is_never_the_substitute(self):
        stop, tried = self.calibrate([BLIND_JACK, WEBCAM, USB_MIC], BLIND_JACK,
                                     {BLIND_JACK["name"]: NoSignal("The microphone did not pick up the level probe.")})
        self.assertIsInstance(stop, SystemExit)
        self.assertEqual([name for name, _, _ in tried], [BLIND_JACK["name"]])
        self.assertIn("did not pick up the level probe", str(stop))

    def test_an_external_choice_is_never_replaced(self):
        stop, tried = self.calibrate([ARRAY, USB_MIC], USB_MIC, {USB_MIC["name"]: NoSignal("silent")})
        self.assertIsInstance(stop, SystemExit)
        self.assertEqual([name for name, _, _ in tried], [USB_MIC["name"]])

    def test_the_working_choice_is_simply_used(self):
        profile, tried = self.calibrate([JACK, ARRAY], ARRAY, {ARRAY["name"]: "ok"}, channel="all")
        self.assertEqual(tried, [(ARRAY["name"], "all", "/cal/file.txt")])
        self.assertNotIn("microphone_fallback", profile)
        self.assertEqual(profile["quality"]["guidance"], ["Keep still."])

    def test_any_other_failure_stops_at_once(self):
        stop, tried = self.calibrate([BLIND_JACK, ARRAY], BLIND_JACK,
                                     {BLIND_JACK["name"]: ValueError("Background sound is too loud.")})
        self.assertEqual(str(stop), "Background sound is too loud.")
        self.assertEqual(len(tried), 1)

    def test_when_nothing_hears_it_every_microphone_tried_is_named(self):
        second = dict(ARRAY, name="alsa_input.pci-0000_04_00.5.HiFi__Mic3__source", description="Second Array")
        stop, tried = self.calibrate([BLIND_JACK, ARRAY, second], BLIND_JACK,
                                     {item["name"]: NoSignal("x") for item in (BLIND_JACK, ARRAY, second)})
        self.assertEqual(len(tried), 3)
        for name in ("Headset Microphone", "Digital Microphone", "Second Array"):
            self.assertIn(name, str(stop))

    def test_an_empty_jack_with_nothing_else_is_still_probed(self):
        # Jack detection can be wrong, and the user picked it.
        profile, tried = self.calibrate([JACK, WEBCAM], JACK, {JACK["name"]: "ok"})
        self.assertEqual([name for name, _, _ in tried], [JACK["name"]])
        self.assertNotIn("microphone_fallback", profile)


class LevelSearchErrorTests(unittest.TestCase):
    def search(self, status):
        found = {"status": status, "selected_level_dbfs": -6.0, "attempts": [{}], "level_bounds_dbfs": [-36, -6]}
        with mock.patch.object(speaker_calibrate, "load_dsp"), \
             mock.patch.object(speaker_calibrate, "search_measurement_level", return_value=found, create=True), \
             mock.patch.object(speaker_calibrate, "level_search_advice",
                               return_value=(["The microphone heard nothing."], []), create=True), \
             mock.patch.object(speaker_calibrate, "LEVEL_SEARCH_ABORT_STATUSES",
                               ("no-signal", "background-too-loud", "level-independent"), create=True), \
             mock.patch.object(speaker_calibrate, "playing_applications", return_value=[]):
            speaker_calibrate.find_measurement_level(SINK["name"], ARRAY["name"], 0, 2)

    def test_only_silence_is_a_no_signal(self):
        with self.assertRaises(NoSignal):
            self.search("no-signal")
        for status in ("background-too-loud", "level-independent"):
            with self.assertRaises(ValueError) as caught:
                self.search(status)
            self.assertNotIsInstance(caught.exception, NoSignal)


class PanelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.source = (Path(speaker_calibrate.__file__).parent / "Panel.qml").read_text()

    def function(self, name):
        start = self.source.index(f"function {name}(")
        return self.source[start:self.source.index("\n  }\n", start)]

    def test_an_empty_jack_is_never_the_automatic_choice(self):
        first = self.function("firstInternal")
        preferred = first.index("list[index].internal === true && list[index].available !== false")
        any_internal = first.index("list[any].internal === true")
        self.assertLess(preferred, any_internal)
        self.assertIn("list[index].available !== false", self.function("defaultInternal"))

    def test_the_row_says_so(self):
        self.assertIn('modelData.available === false ? "  ·  nothing plugged in" : ""', self.source)


if __name__ == "__main__":
    unittest.main()
