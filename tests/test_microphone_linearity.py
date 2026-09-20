#!/usr/bin/env python3

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

from calibration_dsp import (  # noqa: E402
    SweepSpec, analyse_level_probe, build_measurement_signal, level_linearity,
    level_search_advice, linearity_advice, linearity_probe_level, search_measurement_level,
)

SPEC = importlib.util.spec_from_file_location(
    "speaker_calibrate_linearity", Path(__file__).resolve().parents[1] / "speaker-calibrate.py"
)
speaker_calibrate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(speaker_calibrate)

RATE = 48_000
PROBE = dict(seconds=0.4, repeats=1, pre_silence=0.1, block_gap=0.05, response_tail=0.15)


def compress(signal_in, threshold_db, ratio, rate=RATE, window=0.01):
    """A feed-forward compressor on a 10 ms envelope, like a firmware DRC."""
    frames = max(1, int(window * rate))
    padded = np.pad(signal_in ** 2, (frames // 2, frames - frames // 2 - 1), mode="edge")
    envelope = np.sqrt(np.convolve(padded, np.ones(frames) / frames, mode="valid"))
    level = 20.0 * np.log10(np.maximum(envelope, 1e-9))
    over = np.maximum(0.0, level - threshold_db)
    return signal_in * 10.0 ** (-(over * (1.0 - 1.0 / ratio)) / 20.0)


def recorded_probe(level_dbfs, *, path_gain_db=-6.0, room_dbfs=-60.0, processing=None, seed=3):
    """What a microphone records of the real level probe played at a level."""
    program, _ = build_measurement_signal(SweepSpec(level_dbfs=level_dbfs, **PROBE))
    played = np.asarray(program, dtype=float)
    played = played[:, 0] if played.ndim == 2 else played
    rng = np.random.default_rng(seed)
    lead = np.zeros(int(0.7 * RATE))
    heard = np.concatenate([lead, played * 10.0 ** (path_gain_db / 20.0), np.zeros(int(0.25 * RATE))])
    heard = heard + rng.normal(0.0, 10.0 ** (room_dbfs / 20.0), heard.size)
    if processing:
        heard = processing(heard)
    return analyse_level_probe(heard[:, np.newaxis], RATE)


class LoudnessFigureTests(unittest.TestCase):
    def test_the_figure_follows_the_played_level_one_for_one(self):
        quiet, loud = recorded_probe(-30.0), recorded_probe(-20.0)
        self.assertAlmostEqual(loud["loud_rms_dbfs"] - quiet["loud_rms_dbfs"], 10.0, delta=0.3)

    def test_room_noise_does_not_flatter_a_quiet_probe(self):
        # The quiet probe sits only some 10 dB over the room: uncorrected, the
        # room's own power would make it read about half a decibel too loud.
        quiet = recorded_probe(-32.0, room_dbfs=-52.0)
        loud = recorded_probe(-20.0, room_dbfs=-52.0)
        self.assertAlmostEqual(loud["loud_rms_dbfs"] - quiet["loud_rms_dbfs"], 12.0, delta=0.4)

    def test_a_probe_buried_in_the_room_has_no_figure_either(self):
        self.assertLess(recorded_probe(-40.0, room_dbfs=-52.0)["loud_rms_dbfs"], -119.0)

    def test_a_probe_nobody_heard_has_no_figure(self):
        self.assertLess(recorded_probe(-30.0, path_gain_db=-120.0)["loud_rms_dbfs"], -119.0)


class LinearityVerdictTests(unittest.TestCase):
    def attempts(self, levels, **options):
        return [{"level_dbfs": level, **recorded_probe(level, **options)} for level in levels]

    def test_a_plain_microphone_is_linear(self):
        result = level_linearity(self.attempts([-36.0, -24.0]))
        self.assertEqual(result["verdict"], "linear")
        self.assertAlmostEqual(result["slope"], 1.0, delta=0.05)

    def test_a_two_to_one_compressor_is_found(self):
        drc = lambda heard: compress(heard, threshold_db=-50.0, ratio=2.0)  # noqa: E731
        result = level_linearity(self.attempts([-36.0, -24.0], processing=drc))
        self.assertEqual(result["verdict"], "compressed")
        self.assertAlmostEqual(result["slope"], 0.5, delta=0.1)

    def test_the_old_peak_check_let_that_compressor_through(self):
        drc = lambda heard: compress(heard, threshold_db=-50.0, ratio=2.0)  # noqa: E731
        search = search_measurement_level(
            lambda level: recorded_probe(level, processing=drc),
            start_level_dbfs=-36.0, bounds=(-48.0, -6.0), attempts=4,
        )
        self.assertNotEqual(search["status"], "level-independent")

    def test_a_noise_gate_is_found_as_expansion(self):
        def gate(heard):
            frames = int(0.01 * RATE)
            padded = np.pad(heard ** 2, (frames // 2, frames - frames // 2 - 1), mode="edge")
            level = 10.0 * np.log10(np.maximum(
                np.convolve(padded, np.ones(frames) / frames, mode="valid"), 1e-18))
            return heard * 10.0 ** (np.minimum(0.0, level + 20.0) / 20.0)
        result = level_linearity(self.attempts([-36.0, -24.0], processing=gate))
        self.assertEqual(result["verdict"], "expanded")

    def test_probes_too_close_together_say_nothing(self):
        self.assertIsNone(level_linearity(self.attempts([-26.0, -24.0])))

    def test_a_noisy_room_with_probes_close_together_is_still_judged(self):
        # A clipped first probe and a floor close below leave only a small step.
        drc = lambda heard: compress(heard, threshold_db=-60.0, ratio=2.0)  # noqa: E731
        noisy = dict(room_dbfs=-58.0, path_gain_db=0.0)
        self.assertEqual(
            level_linearity(self.attempts([-36.0, -32.5], **noisy))["verdict"], "linear")
        self.assertEqual(
            level_linearity(self.attempts([-36.0, -32.5], processing=drc, **noisy))["verdict"],
            "compressed")

    def test_scatter_between_two_probes_is_not_a_verdict(self):
        attempts = self.attempts([-27.0, -24.0])
        attempts[1]["loud_rms_dbfs"] -= 1.0  # slope 0.67, but only 1 dB short
        self.assertEqual(level_linearity(attempts)["verdict"], "linear")

    def test_a_clipped_or_buried_probe_is_not_used(self):
        attempts = self.attempts([-36.0, -24.0])
        attempts[1]["clipped_samples"] = 3
        self.assertIsNone(level_linearity(attempts))
        attempts = self.attempts([-36.0, -24.0])
        attempts[0]["prominence_db"] = 4.0
        self.assertIsNone(level_linearity(attempts))


class ExtraProbeTests(unittest.TestCase):
    def search(self, levels, bounds=(-36.0, -6.0)):
        return {"level_bounds_dbfs": list(bounds),
                "attempts": [{"level_dbfs": level, **recorded_probe(level)} for level in levels]}

    def test_a_search_that_settled_at_once_gets_one_quieter_probe(self):
        self.assertEqual(linearity_probe_level(self.search([-24.0])), -32.0)

    def test_a_search_that_already_knows_asks_for_nothing(self):
        self.assertIsNone(linearity_probe_level(self.search([-36.0, -24.0])))

    def test_the_extra_probe_is_never_louder_and_respects_the_bounds(self):
        self.assertEqual(linearity_probe_level(self.search([-24.0], bounds=(-30.0, -6.0))), -30.0)
        self.assertIsNone(linearity_probe_level(self.search([-24.0], bounds=(-26.0, -6.0))))

    def test_nothing_clean_to_compare_with_means_no_probe(self):
        search = self.search([-24.0])
        search["attempts"][0]["clipped_samples"] = 9
        self.assertIsNone(linearity_probe_level(search))


class AdviceTests(unittest.TestCase):
    COMPRESSED = {"verdict": "compressed", "level_change_db": 12.0, "recorded_change_db": 6.1}

    def test_a_linear_path_adds_nothing(self):
        search = {"status": "converged", "selected_level_dbfs": -24.0,
                  "attempts": [{}], "linearity": {"verdict": "linear"}}
        self.assertEqual(level_search_advice(search), ([], []))
        self.assertEqual(level_search_advice(dict(search, linearity=None)), ([], []))

    def test_compression_names_the_switch_that_is_on(self):
        warnings, guidance = linearity_advice({
            "linearity": self.COMPRESSED,
            "microphone_processing": [
                {"card": "1", "name": "Microphone Capture DRC switch", "values": "on", "on": True},
                {"card": "1", "name": "Dmic0 Noise Suppression Switch", "values": "off", "on": False},
            ],
        })
        self.assertIn("6.1 dB for a 12.0 dB louder probe", warnings[0])
        self.assertIn("amixer -c 1 cset name='Microphone Capture DRC switch' off", guidance[0])
        self.assertNotIn("Noise Suppression", guidance[0])

    def test_compression_without_a_switch_still_gives_advice(self):
        warnings, guidance = linearity_advice({"linearity": self.COMPRESSED})
        self.assertEqual(len(warnings), 1)
        self.assertIn("automatic gain", guidance[0])

    def test_it_is_added_to_the_status_advice_not_put_in_its_place(self):
        warnings, _ = level_search_advice({
            "status": "best-tested", "selected_level_dbfs": -20.0, "attempts": [{}],
            "linearity": self.COMPRESSED,
        })
        self.assertEqual(len(warnings), 2)


CONTROLS = """numid=3,iface=MIXER,name='Headphone Playback Switch'
numid=21,iface=MIXER,name='Microphone Capture DRC switch'
numid=22,iface=MIXER,name='Dmic0 Capture Switch'
numid=23,iface=MIXER,name='Speaker Playback DRC switch'
numid=24,iface=MIXER,name='Dmic0 Noise Suppression Switch'
numid=25,iface=MIXER,name='Microphone Capture DRC bytes'
"""


class FakeMixer:
    """amixer and pactl as seen by the helper, with switch state that can change."""

    def __init__(self):
        self.state = {"Microphone Capture DRC switch": "on",
                      "Dmic0 Noise Suppression Switch": "off",
                      "Speaker Playback DRC switch": "on"}
        self.writes = []

    def run(self, args, *, check=True, capture=False):
        done = lambda out="", code=0: subprocess.CompletedProcess(args, code, stdout=out, stderr="")  # noqa: E731
        if args[:3] == ["amixer", "-c", "1"] and args[3] == "controls":
            return done(CONTROLS)
        name = args[4][len("name='"):-1] if len(args) > 4 else ""
        if args[3] == "cget":
            if name not in self.state:
                return done("; type=BYTES\n", 0)
            return done(f"numid=1,iface=MIXER,name='{name}'\n  ; type=BOOLEAN,access=rw------,values=1\n"
                        f"  : values={self.state[name]}\n")
        if args[3] == "cset":
            self.state[name] = args[5]
            self.writes.append((name, args[5]))
            return done()
        return done(code=1)

    @staticmethod
    def pactl(kind):
        return [{"name": "alsa_input.pci-dmic", "properties": {"alsa.card": "1"}},
                {"name": "alsa_input.usb-mic", "properties": {}}]


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.mixer = FakeMixer()
        self.directory = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(speaker_calibrate, "run", side_effect=self.mixer.run),
            mock.patch.object(speaker_calibrate, "pactl_json", side_effect=self.mixer.pactl),
            mock.patch.object(speaker_calibrate, "MIC_PROCESSING_STATE",
                              self.directory / "restore.json"),
            mock.patch.object(speaker_calibrate.time, "sleep"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_only_capture_side_processing_switches_are_listed(self):
        found = speaker_calibrate.microphone_processing_switches("alsa_input.pci-dmic")
        self.assertEqual(
            [(item["name"], item["on"]) for item in found],
            [("Microphone Capture DRC switch", True), ("Dmic0 Noise Suppression Switch", False)],
        )

    def test_a_microphone_without_a_card_has_none(self):
        self.assertEqual(speaker_calibrate.microphone_processing_switches("alsa_input.usb-mic"), [])
        self.assertEqual(speaker_calibrate.microphone_processing_switches("gone"), [])

    def test_a_missing_amixer_is_not_an_error(self):
        with mock.patch.object(speaker_calibrate, "run", side_effect=FileNotFoundError):
            self.assertEqual(
                speaker_calibrate.microphone_processing_switches("alsa_input.pci-dmic"), [])

    def test_the_bypass_turns_off_only_what_was_on_and_puts_it_back(self):
        with speaker_calibrate.microphone_processing_bypassed("alsa_input.pci-dmic") as off:
            self.assertEqual([item["name"] for item in off], ["Microphone Capture DRC switch"])
            self.assertEqual(self.mixer.state["Microphone Capture DRC switch"], "off")
            self.assertTrue(speaker_calibrate.MIC_PROCESSING_STATE.exists())
        self.assertEqual(self.mixer.state["Microphone Capture DRC switch"], "on")
        self.assertEqual(self.mixer.state["Dmic0 Noise Suppression Switch"], "off")
        self.assertEqual(self.mixer.state["Speaker Playback DRC switch"], "on")
        self.assertFalse(speaker_calibrate.MIC_PROCESSING_STATE.exists())

    def test_a_failing_measurement_still_puts_it_back(self):
        with self.assertRaises(RuntimeError):
            with speaker_calibrate.microphone_processing_bypassed("alsa_input.pci-dmic"):
                raise RuntimeError("recorder died")
        self.assertEqual(self.mixer.state["Microphone Capture DRC switch"], "on")

    def test_a_killed_run_is_repaired_by_the_next_start(self):
        self.mixer.state["Microphone Capture DRC switch"] = "off"
        speaker_calibrate.MIC_PROCESSING_STATE.write_text(json.dumps({
            "pid": 2 ** 22 + 12345,
            "switches": [{"card": "1", "name": "Microphone Capture DRC switch", "values": "on"}],
        }))
        self.assertEqual(speaker_calibrate.restore_microphone_processing(),
                         ["Microphone Capture DRC switch"])
        self.assertEqual(self.mixer.state["Microphone Capture DRC switch"], "on")

    def test_a_record_of_a_run_still_alive_is_left_to_it(self):
        speaker_calibrate.MIC_PROCESSING_STATE.write_text(json.dumps({
            "pid": speaker_calibrate.os.getpid(),
            "switches": [{"card": "1", "name": "Microphone Capture DRC switch", "values": "on"}],
        }))
        self.assertEqual(speaker_calibrate.restore_microphone_processing(), [])
        self.assertEqual(self.mixer.writes, [])

    def test_every_measurement_runs_with_the_processing_off_and_says_so(self):
        seen = {}

        def sweeps(sink_name, mic_name, *args, **kwargs):
            seen["during"] = self.mixer.state["Microphone Capture DRC switch"]
            seen["call"] = (sink_name, mic_name, args, kwargs)
            return {"quality": {"accepted": True, "warnings": [], "guidance": ["Keep still."]}}

        with mock.patch.object(speaker_calibrate, "measure_through_sweeps", side_effect=sweeps):
            measurement = speaker_calibrate.capture_measurement(
                "alsa_output.pci-spk", "alsa_input.pci-dmic", "all", None, level_sink="x")
        self.assertEqual(seen["during"], "off")
        self.assertEqual(seen["call"], ("alsa_output.pci-spk", "alsa_input.pci-dmic", ("all", None), {"level_sink": "x"}))
        self.assertEqual(self.mixer.state["Microphone Capture DRC switch"], "on")
        self.assertEqual(measurement["microphone_processing_suspended"], ["Microphone Capture DRC switch"])
        # It is said, and it is not a warning: the verdict is about the speakers.
        self.assertEqual(measurement["quality"]["warnings"], [])
        self.assertIn("'Microphone Capture DRC switch'", measurement["quality"]["guidance"][-1])
        self.assertEqual(measurement["quality"]["guidance"][0], "Keep still.")

    def test_a_measurement_that_fails_still_puts_the_processing_back(self):
        with mock.patch.object(speaker_calibrate, "measure_through_sweeps",
                               side_effect=ValueError("Background sound is too loud.")):
            with self.assertRaises(ValueError):
                speaker_calibrate.capture_measurement("alsa_output.pci-spk", "alsa_input.pci-dmic", 0)
        self.assertEqual(self.mixer.state["Microphone Capture DRC switch"], "on")
        self.assertFalse(speaker_calibrate.MIC_PROCESSING_STATE.exists())

    def test_only_the_measuring_microphone_s_card_is_touched(self):
        # Pull request #13 scanned every card; a USB interface's own
        # compressor is none of a laptop measurement's business.
        with mock.patch.object(speaker_calibrate, "measure_through_sweeps", return_value={"quality": {}}):
            measurement = speaker_calibrate.capture_measurement("alsa_output.pci-spk", "alsa_input.usb-mic", 0)
        self.assertEqual(self.mixer.writes, [])
        self.assertNotIn("microphone_processing_suspended", measurement)

    def test_nothing_on_means_nothing_written(self):
        self.mixer.state["Microphone Capture DRC switch"] = "off"
        with speaker_calibrate.microphone_processing_bypassed("alsa_input.pci-dmic") as off:
            self.assertEqual(off, [])
        self.assertEqual(self.mixer.writes, [])
        self.assertFalse(speaker_calibrate.MIC_PROCESSING_STATE.exists())


if __name__ == "__main__":
    unittest.main()
