#!/usr/bin/env python3
"""A public profile from the registry, rendered as an Omarchy vendor tuning.

The result is a file that Omarchy sources as shell on other people's machines,
made from a stranger's upload, so most of this is about what cannot get in.
"""

import copy
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

import calibration_share as share  # noqa: E402
from test_calibration_share import VERIFIED, shared  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "speaker_calibrate_vendor", Path(__file__).resolve().parents[1] / "speaker-calibrate.py"
)
speaker_calibrate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(speaker_calibrate)

METRICS = {"bass_group_delay_swing_ms": 0.7, "limiter_headroom_db": 0.4, "peak_dbfs": -1.4,
           "dynamic_range_delta_lu": 0.0, "signal": "pink noise, 20 s, peaks at -0.1 dBFS"}


def public(verification=VERIFIED, **changes):
    return share.public_payload(shared(**changes), verification)


def render(payload, identifier="2026-09-20-calibrated-0123456789", score=90, today="2026-09-21"):
    # The four figures cost twenty seconds of simulated audio; they are tested where they are made.
    with mock.patch.object(speaker_calibrate, "vendor_metrics", return_value=dict(METRICS)):
        return speaker_calibrate.render_registry_tuning(payload, identifier=identifier, score=score, today=today)


def sourced(tuning):
    """What bash sees after sourcing the tuning, under the strictest settings."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "tuning.conf"
        path.write_text(tuning)
        canary = Path(directory) / "canary"
        proc = subprocess.run(
            ["/usr/bin/bash", "-euc",
             'cd "$2"; source "$1"; printf "%s\\n" "$description" "${match_sku[0]:-${match_dmi[0]}}" '
             '"$sink_pattern" "$derived_from" "$validated_on" "$validated_hardware"',
             "x", str(path), directory], capture_output=True, text=True, timeout=20)
        return proc, sorted(item.name for item in Path(directory).iterdir()), canary


class EligibilityTests(unittest.TestCase):
    def test_only_a_checked_calibration_that_rates_good_becomes_a_tuning(self):
        self.assertEqual(share.vendor_eligible(public(), 90), (True, ""))
        self.assertEqual(share.vendor_eligible(public(dict(VERIFIED, verdict="warning")), 61), (True, ""))
        self.assertEqual(share.vendor_eligible(public(None), 90)[1], "it has not been checked")
        self.assertEqual(share.vendor_eligible(public(dict(VERIFIED, verdict="fail")), 90)[1], "it has not been checked")
        self.assertEqual(share.vendor_eligible(public(), 59)[1], "it scores below 60")
        self.assertFalse(share.vendor_eligible(public(), float("nan"))[0])
        nameless = public()
        nameless["hardware"]["speaker"] = None
        self.assertEqual(share.vendor_eligible(nameless, 90)[1], "it does not name the speakers it was made for")
        with self.assertRaises(SystemExit):
            render(public(None))


class RenderingTests(unittest.TestCase):
    def test_a_public_profile_renders_the_same_tuning_every_time(self):
        first, second = render(public()), render(public())
        self.assertEqual(first["tuning"], second["tuning"])
        self.assertEqual(first["chain"], second["chain"])
        self.assertIn('validated_on="2026-09-21"', first["tuning"])
        self.assertIn("registry profile 2026-09-20-calibrated-0123456789, score 90", first["tuning"])
        self.assertIn('validated_by=""', first["tuning"])                       # a person who listened fills this in
        self.assertEqual(first["sections"], 3)                                  # the high-pass and two cuts
        self.assertIn("hb_", first["chain"])                                    # deep bass at the plugin's default

    def test_bash_sources_it_and_sees_the_machine(self):
        proc, files, _ = sourced(render(public())["tuning"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[2], "^alsa_output.pci-0000_00_1f.3.analog-stereo$")
        self.assertEqual(lines[4], "2026-09-21")
        self.assertEqual(files, ["tuning.conf"])

    def test_the_playing_calibration_is_never_read_when_a_profile_is_handed_in(self):
        with mock.patch.object(speaker_calibrate, "load_profile", side_effect=AssertionError("read the local profile")), \
             mock.patch.object(speaker_calibrate, "hardware_id", side_effect=AssertionError("read the local firmware")):
            render(public())


class RunnerTests(unittest.TestCase):
    def test_rendering_needs_no_limiter_on_the_machine_but_measuring_does(self):
        # The registry renders on a runner without the LV2 limiter; its first tuning failed on exactly this.
        bound = {name: speaker_calibrate.__dict__.pop(name) for name in list(speaker_calibrate.__dict__)
                 if name == "np"}
        self.addCleanup(lambda: speaker_calibrate.__dict__.update(bound))
        with mock.patch.object(speaker_calibrate, "LIMITER_PROBE", Path("/nonexistent/limiter.ttl")):
            with self.assertRaisesRegex(SystemExit, "lsp-plugins-lv2"):
                speaker_calibrate.load_dsp()
            speaker_calibrate.__dict__.pop("np", None)
            speaker_calibrate.load_dsp(playing=False)
            rendered = speaker_calibrate.render_registry_tuning(
                public(), identifier="2026-09-20-calibrated-0123456789", score=90, today="2026-09-21")
        self.assertIn("limiter_headroom_db", rendered["tuning"])
        self.assertTrue(rendered["metrics"]["signal"].startswith("pink noise"))


class HostileUploadTests(unittest.TestCase):
    ATTACKS = ('$(touch canary)', '`touch canary`', '"; touch canary; "', "'; touch canary; '", "x\ntouch canary",
               "\\", "${IFS}", "a" * 500)

    def test_firmware_strings_cannot_run_anything(self):
        for field in ("sys_vendor", "product_name", "product_sku", "product_version", "board_name", "label"):
            for attack in self.ATTACKS:
                payload = public()
                payload["hardware"][field] = attack          # past public_payload, as a tampered registry file would be
                try:
                    tuning = render(payload)["tuning"]
                except SystemExit:
                    continue                                  # refused outright is fine
                for active in ("$", "`", "\\", "'; ", '"; '):
                    self.assertNotIn(active + "touch", tuning, (field, attack))
                self.assertNotIn("\ntouch", tuning, (field, attack))      # a line break becomes a space inside the quotes
                proc, files, _ = sourced(tuning)
                self.assertEqual(proc.returncode, 0, (field, attack, proc.stderr))
                self.assertEqual(files, ["tuning.conf"], (field, attack))

    def test_the_speaker_the_date_and_the_origin_are_held_to_their_shapes(self):
        for speaker in ("alsa_output.pci-x'; touch canary; '", "alsa_output.pci-$(touch canary)", "bluez_output.AA_BB.1",
                        "alsa_output.usb-Dock.analog-stereo", "alsa_output.pci-0000_00_1f.3.hdmi-stereo", "", None):
            payload = public()
            payload["hardware"]["speaker"] = speaker
            with self.assertRaises(SystemExit, msg=speaker):
                render(payload)
        for created in ("yesterday", "", "$(touch canary)"):
            payload = public()
            payload["profile"]["created_at"] = created
            with self.assertRaises(SystemExit, msg=created):
                render(payload)
        # Only the day is ever read, so what follows it goes nowhere.
        payload = public()
        payload["profile"]["created_at"] = "2026-09-20$(touch canary)"
        self.assertNotIn("canary", render(payload)["tuning"])
        for identifier in ('x"; touch canary; "', "$(id)", "a\nb"):
            with self.assertRaises(SystemExit, msg=identifier):
                render(public(), identifier=identifier)
        for today in ("2026-09-21; id", "today", "2026-09-21T00:00:00"):
            with self.assertRaises(SystemExit, msg=today):
                render(public(), today=today)

    def test_words_and_numbers_in_the_profile_cannot_reach_the_shell(self):
        payload = public()
        payload["profile"]["voicing"] = '$(touch canary)'
        tuning = render(payload)["tuning"]
        self.assertNotIn("canary", tuning)
        self.assertIn("flat target", tuning)
        payload = copy.deepcopy(public())
        payload["profile"]["fit"]["weighted_rmse_after_db"] = '$(touch canary)'
        with self.assertRaises((SystemExit, ValueError, TypeError)):
            render(payload)


if __name__ == "__main__":
    unittest.main()
