#!/usr/bin/env python3

import importlib.util
import io
import json
import subprocess
import tempfile
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
             mock.patch.object(speaker_calibrate, "refuse_silenced_devices"), \
             mock.patch.object(speaker_calibrate, "playing_applications", return_value=[]):
            speaker_calibrate.find_measurement_level(SINK["name"], ARRAY["name"], 0, 2)

    def test_only_silence_is_a_no_signal(self):
        with self.assertRaises(NoSignal):
            self.search("no-signal")
        for status in ("background-too-loud", "level-independent"):
            with self.assertRaises(ValueError) as caught:
                self.search(status)
            self.assertNotIsInstance(caught.exception, NoSignal)


def volume(value):
    return {"front-left": {"value": value, "value_percent": f"{round(value / 655.36)}%", "db": "x"},
            "front-right": {"value": value, "value_percent": f"{round(value / 655.36)}%", "db": "x"}}


class SilencedDeviceTests(unittest.TestCase):
    def test_mute_and_zero_volume_are_reasons_and_nothing_else_is(self):
        reason = speaker_calibrate.silenced_reason
        self.assertEqual(reason({"mute": True, "volume": volume(65536)}), "is muted")
        self.assertEqual(reason({"mute": False, "volume": volume(0)}), "is turned down to zero")
        self.assertIsNone(reason({"mute": False, "volume": volume(1)}))
        self.assertIsNone(reason({"mute": False, "volume": {"mono": {"value": 0}, "aux": {"value": 30000}}}))
        for odd in ({}, {"volume": "loud"}, {"volume": {}}, {"volume": {"mono": {"value": None}}},
                    {"volume": {"mono": {"value": True}}}, None, "x"):
            self.assertIsNone(reason(odd), odd)

    def check(self, sinks, sources, sink_names, mic):
        with mock.patch.object(speaker_calibrate, "pactl_json",
                               side_effect=lambda kind: sinks if kind == "sinks" else sources):
            speaker_calibrate.refuse_silenced_devices(sink_names, mic)

    def test_a_microphone_at_zero_stops_the_measurement_before_a_sound(self):
        headset = dict(USB_MIC, description="BTD 700 Mono", mute=False, volume=volume(0))
        with self.assertRaisesRegex(speaker_calibrate.DeviceSilenced, "BTD 700 Mono is turned down to zero"):
            self.check([SINK], [headset], [SINK["name"], None], headset["name"])

    def test_muted_speakers_too_including_the_real_ones_behind_the_calibrated_sink(self):
        muted = dict(SINK, mute=True)
        with self.assertRaisesRegex(speaker_calibrate.DeviceSilenced, "Speakers is muted"):
            self.check([{"name": "omarchy_speaker_tuning"}, muted], [ARRAY],
                       ["omarchy_speaker_tuning", SINK["name"]], ARRAY["name"])

    def test_healthy_or_unknown_devices_pass(self):
        self.check([dict(SINK, mute=False, volume=volume(40000))], [ARRAY], [SINK["name"], None], ARRAY["name"])
        self.check([], [], ["gone"], "gone too")
        with mock.patch.object(speaker_calibrate, "pactl_json", side_effect=OSError):
            speaker_calibrate.refuse_silenced_devices([SINK["name"]], ARRAY["name"])

    def test_the_level_search_asks_first(self):
        with mock.patch.object(speaker_calibrate, "refuse_silenced_devices",
                               side_effect=speaker_calibrate.DeviceSilenced("muted")) as asked, \
             mock.patch.object(speaker_calibrate, "load_dsp") as loaded:
            with self.assertRaises(speaker_calibrate.DeviceSilenced):
                speaker_calibrate.find_measurement_level("sink", "mic", 0, 1, level_sink="real")
        asked.assert_called_once_with(["sink", "real"], "mic")
        loaded.assert_not_called()

    def test_the_device_list_says_it_and_the_fallback_skips_it(self):
        default = subprocess.CompletedProcess([], 0, stdout="\n", stderr="")
        quiet = dict(ARRAY, mute=True)
        with mock.patch.object(speaker_calibrate, "pactl_json",
                               side_effect=lambda kind: [SINK] if kind == "sinks" else [JACK, quiet]), \
             mock.patch.object(speaker_calibrate, "run", return_value=default), \
             mock.patch.object(speaker_calibrate, "load_selection", return_value={}):
            listed = speaker_calibrate.devices_payload()["microphones"]
        self.assertEqual([item["silenced"] for item in listed], [None, "is muted"])


class OfferTests(FallbackTests):
    def test_an_external_pick_that_hears_nothing_offers_the_built_in_one(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            stop, tried = self.calibrate([JACK, ARRAY, USB_MIC], USB_MIC, {USB_MIC["name"]: NoSignal("silent")})
        self.assertEqual(stop.code, speaker_calibrate.OFFER_EXIT_CODE)
        self.assertEqual([name for name, _, _ in tried], [USB_MIC["name"]])      # nothing else was measured
        reply = json.loads(out.getvalue())
        self.assertEqual(reply["offer"], {"microphone": ARRAY["name"], "description": "Digital Microphone",
                                          "channel": "all"})
        self.assertIn("Usb Microphone did not pick up the level probe", reply["error"])
        self.assertIn("Digital Microphone", reply["error"])

    def test_no_built_in_microphone_worth_offering_means_the_plain_failure(self):
        for microphones in ([JACK, USB_MIC], [dict(ARRAY, mute=True), USB_MIC], [USB_MIC, WEBCAM]):
            stop, _ = self.calibrate(microphones, USB_MIC, {USB_MIC["name"]: NoSignal("The microphone heard nothing.")})
            self.assertEqual(str(stop), "The microphone heard nothing.")

    def test_a_muted_built_in_microphone_is_not_a_substitute_either(self):
        stop, tried = self.calibrate([BLIND_JACK, dict(ARRAY, mute=True)], BLIND_JACK,
                                     {BLIND_JACK["name"]: NoSignal("heard nothing")})
        self.assertEqual([name for name, _, _ in tried], [BLIND_JACK["name"]])
        self.assertEqual(str(stop), "heard nothing")


class RememberedSelectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        data = Path(self.folder.name) / "data"
        data.mkdir(mode=0o700)
        for name, value in (("DATA", data), ("SELECTION", data / "selection.json")):
            patch = mock.patch.object(speaker_calibrate, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_pick_survives_and_a_later_one_replaces_only_what_it_names(self):
        self.assertEqual(speaker_calibrate.load_selection(), {})
        speaker_calibrate.remember_selection(SINK["name"], USB_MIC["name"], "2")
        self.assertEqual(speaker_calibrate.load_selection(),
                         {"sink": SINK["name"], "mic": USB_MIC["name"], "channel": 2})
        speaker_calibrate.remember_selection(mic=ARRAY["name"], channel=0)
        self.assertEqual(speaker_calibrate.load_selection(),
                         {"sink": SINK["name"], "mic": ARRAY["name"], "channel": 0})
        self.assertEqual(speaker_calibrate.SELECTION.stat().st_mode & 0o777, 0o600)

    def test_a_name_pipewire_would_not_give_is_refused_and_nothing_is_written(self):
        for name in ('x" } context.exec = [', "-rf", "a b", ""):
            with self.assertRaises(SystemExit, msg=name):
                speaker_calibrate.remember_selection(mic=name)
        with self.assertRaises(SystemExit):
            speaker_calibrate.remember_selection(mic=ARRAY["name"], channel="all")
        self.assertFalse(speaker_calibrate.SELECTION.exists())

    def test_a_file_someone_else_wrote_is_read_with_suspicion(self):
        for text in ('{"mic": "x\" } ] context.exec", "sink": 7, "channel": 900}', "[1, 2]", "not json",
                     "[" * 100000, '{"channel": true}'):
            speaker_calibrate.SELECTION.write_text(text)
            speaker_calibrate.SELECTION.chmod(0o600)
            self.assertEqual(speaker_calibrate.load_selection(), {}, text[:30])
        speaker_calibrate.SELECTION.unlink()
        target = Path(self.folder.name) / "elsewhere.json"
        target.write_text(json.dumps({"mic": ARRAY["name"]}))
        speaker_calibrate.SELECTION.symlink_to(target)
        self.assertEqual(speaker_calibrate.load_selection(), {})

    def test_the_device_list_carries_it(self):
        speaker_calibrate.remember_selection(SINK["name"], ARRAY["name"], 1)
        default = subprocess.CompletedProcess([], 0, stdout="\n", stderr="")
        with mock.patch.object(speaker_calibrate, "pactl_json", return_value=[]), \
             mock.patch.object(speaker_calibrate, "run", return_value=default):
            self.assertEqual(speaker_calibrate.devices_payload()["chosen"],
                             {"sink": SINK["name"], "mic": ARRAY["name"], "channel": 1})


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

    def test_a_pick_from_before_a_restart_counts_as_a_pick(self):
        select = self.function("selectDevices")
        adopted = select.index('if (root.chosenMic === "" && kept.mic)')
        ranked = select.index("deviceIndex(service.microphones, root.chosenMic)")
        self.assertLess(adopted, ranked)
        self.assertEqual(self.source.count("service.rememberSelection("), 3)   # speaker, microphone, channel

    def test_the_offer_is_one_press_and_never_changes_the_pick(self):
        start = self.source.index("readonly property int offeredIndex")
        button = self.source[start:self.source.index("\n          }\n", start)]
        self.assertIn("root.deviceIndex(service.microphones, service.offer.microphone)", button)
        self.assertIn("service.microphones[offeredIndex].description", button)   # the panel's own label
        self.assertNotIn("service.offer.description", button)
        self.assertIn("service.measure(", button)
        self.assertNotIn("chosenMic", button)
        self.assertNotIn("rememberSelection", button)

    def test_a_failure_with_an_offer_is_read_as_one_document(self):
        service = (Path(speaker_calibrate.__file__).parent / "Service.qml").read_text()
        self.assertIn('typeof failure.error === "string"', service)
        self.assertIn('typeof offered.microphone === "string"', service)
        self.assertIn('if (operation !== "remember") offer = null', service)

    def test_the_row_says_so(self):
        self.assertIn('modelData.available === false ? "  ·  nothing plugged in" : ""', self.source)


if __name__ == "__main__":
    unittest.main()
