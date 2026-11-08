#!/usr/bin/python3
"""The calibration list: every calibration that played, one click away, and a switch that keeps the sound going."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox  # noqa: E402,F401  (keeps every test off this machine's calibrator)

SPEC = importlib.util.spec_from_file_location(
    "speaker_calibrate_library", Path(__file__).resolve().parents[1] / "speaker-calibrate.py"
)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)
# The real one, before any test fakes it.
STATUS_PAYLOAD = helper.status_payload

SPEAKER = "alsa_output.pci-test.analog-stereo"


def profile(created, *, speaker=SPEAKER, imported=None, gain=-6.0, **changes):
    value = {
        "schema_version": 5, "plugin_version": "1.2.0", "created_at": created,
        "speaker": {"name": speaker, "description": "Built-in Audio"},
        "microphone": {"name": "alsa_input.test", "internal": True, "channel": "all"},
        "voicing": "neutral", "loudness": "protected", "bass": "normal",
        "deep_bass": "off", "loudness_compensation": "off",
        "quality": {"accepted": True, "verdict": "pass", "warnings": [], "guidance": []},
        "fit": {"filter_count": 1, "input_gain_linear": 0.8, "makeup_db": 0.0,
                "filters": [{"type": "peaking", "frequency_hz": 600.0, "q": 2.0, "gain_db": gain}],
                "highpass": {"frequency_hz": 190.0, "q": 0.707, "stages": 1}},
    }
    if imported is not None:
        value["imported"] = imported
    value.update(changes)
    return value


class LibraryTestCase(unittest.TestCase):
    # Most tests are about the list, so the switch itself is faked.
    fake_activate = True

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.data = root / "data"
        self.data.mkdir(mode=0o700)
        self.config = root / "config"
        paths = {
            "DATA": self.data, "PROFILE": self.data / "active-profile.json",
            "PROPOSAL": self.data / "proposed-profile.json",
            "PREVIOUS_PROFILE": self.data / "previous-profile.json",
            "HELD_PROFILE": self.data / "held-profile.json", "HELD_PREVIOUS": self.data / "held-previous.json",
            "COMPARE_STATE": self.data / "compare-state.json", "STATE_LOCK": self.data / "lock",
            "LIBRARY": self.data / "library", "LIBRARY_INDEX": self.data / "library" / "index.json",
            "FRAGMENT": self.config / "pipewire/omarchy-speaker-tuning.conf.d/90-tuning.conf",
        }
        for name, value in paths.items():
            self.patch(name, value)
        self.activated = []
        if self.fake_activate:
            self.patch("activate_profile", lambda item: self.activated.append(item["created_at"]) or "live")
        self.patch("status_payload", lambda: {"library": helper.library_status(helper.load_profile(helper.PROFILE))})
        for name in ("start_loudness_tracker", "stop_loudness_tracker", "archive_measurement"):
            self.patch(name, lambda *args, **options: None)

    def patch(self, name, value):
        patcher = mock.patch.object(helper, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, item):
        """What install_profile does to the library and to PROFILE."""
        helper.library_add(item)
        helper.PROFILE.write_text(json.dumps(item))
        return item["library_id"]

    def rows(self):
        return helper.library_status(helper.load_profile(helper.PROFILE))


class AddingAndPlayingTests(LibraryTestCase):
    def test_a_new_calibration_plays_and_the_one_before_stays_one_click_away(self):
        first = self.install(profile("2026-11-01T10:00:00+00:00"))
        second = self.install(profile("2026-11-02T10:00:00+00:00", gain=-3.0))
        self.assertEqual([(row["id"], row["playing"]) for row in self.rows()], [(second, True), (first, False)])
        result = helper.library_play(first)
        self.assertEqual(self.activated, ["2026-11-01T10:00:00+00:00"])
        self.assertEqual(result["method"], "live")
        self.assertIn("switched live", result["message"])
        self.assertEqual(helper.load_profile(helper.PROFILE)["library_id"], first)
        self.assertEqual([row["id"] for row in self.rows() if row["playing"]], [first])
        self.assertEqual(self.rows()[0]["id"], first)

    def test_what_the_switches_did_is_kept_with_the_calibration(self):
        first = self.install(profile("2026-11-01T10:00:00+00:00"))
        playing = json.loads(helper.PROFILE.read_text())
        playing["bass"] = "full"                      # what relevel writes to PROFILE
        helper.PROFILE.write_text(json.dumps(playing))
        self.install(profile("2026-11-02T10:00:00+00:00"))
        stored = json.loads((helper.LIBRARY / f"{first}.json").read_text())
        self.assertEqual(stored["bass"], "full")

    def test_the_machine_s_switches_stay_as_they_are(self):
        first = self.install(profile("2026-11-01T10:00:00+00:00", deep_bass="off"))
        self.install(profile("2026-11-02T10:00:00+00:00", deep_bass="on", loudness_compensation="on"))
        helper.library_play(first)
        playing = helper.load_profile(helper.PROFILE)
        self.assertEqual((playing["deep_bass"], playing["loudness_compensation"]), ("on", "on"))

    def test_a_switch_that_fails_still_leaves_the_list_and_profile_agreeing(self):
        first = self.install(profile("2026-11-01T10:00:00+00:00"))
        self.install(profile("2026-11-02T10:00:00+00:00"))
        def sink_never_appears(item):
            raise SystemExit("The tuning sink did not appear; inspect the user service status.")
        self.patch("activate_profile", sink_never_appears)
        with self.assertRaisesRegex(SystemExit, "did not appear"):
            helper.library_play(first)
        # activate_profile had already written the graph for this one, so it is what the next start plays.
        self.assertEqual(helper.load_profile(helper.PROFILE)["library_id"], first)
        self.assertEqual([row["id"] for row in self.rows() if row["playing"]], [first])
        self.assertEqual(self.rows()[0]["id"], first)

    def test_playing_what_already_plays_changes_nothing(self):
        first = self.install(profile("2026-11-01T10:00:00+00:00"))
        result = helper.library_play(first)
        self.assertEqual(self.activated, [])
        self.assertIn("already playing", result["message"])


class KeepingTests(LibraryTestCase):
    def test_the_last_ten_of_one_s_own_are_kept_and_pinned_and_shared_on_top(self):
        pinned = self.install(profile("2026-10-01T10:00:00+00:00"))
        helper.library_pin(pinned, True)
        shared = self.install(profile("2026-09-20", imported={"registry": "2026-09-20-calibrated-0123456789", "name": "x"}))
        own = [self.install(profile(f"2026-11-{day:02d}T10:00:00+00:00")) for day in range(1, 13)]
        kept = {row["id"] for row in self.rows()}
        self.assertEqual(kept, set(own[-10:]) | {pinned, shared})
        self.assertEqual(sorted(path.stem for path in helper.LIBRARY.glob("*.json") if path.stem != "index"),
                         sorted(kept))

    def test_the_playing_one_is_never_dropped(self):
        entries = [{"id": f"{index:012x}", "source": "measured", "pinned": False,
                    "played_at": f"2026-11-{index + 1:02d}"} for index in range(12)]
        oldest = entries[0]["id"]
        kept = [entry["id"] for entry in helper._prune(entries, oldest)]
        self.assertIn(oldest, kept)
        self.assertEqual(len(kept), 11)              # the ten most recent, and the one that plays

    def test_unpinned_and_deleted(self):
        first = self.install(profile("2026-11-01T10:00:00+00:00"))
        second = self.install(profile("2026-11-02T10:00:00+00:00"))
        helper.library_pin(first, True)
        self.assertTrue(next(row for row in self.rows() if row["id"] == first)["pinned"])
        helper.library_pin(first, False)
        self.assertFalse(next(row for row in self.rows() if row["id"] == first)["pinned"])
        with self.assertRaisesRegex(SystemExit, "is playing"):
            helper.library_delete(second)
        helper.library_delete(first)
        self.assertEqual([row["id"] for row in self.rows()], [second])
        self.assertFalse((helper.LIBRARY / f"{first}.json").exists())


class RefusalTests(LibraryTestCase):
    def test_only_an_id_in_the_list_ever_becomes_a_path(self):
        self.install(profile("2026-11-01T10:00:00+00:00"))
        for identifier in ("../x", "ABCDEF123456", "0123456789abc", "", None, "0123456789ab"):
            with self.assertRaises(SystemExit, msg=repr(identifier)):
                helper.library_play(identifier)
            with self.assertRaises(SystemExit, msg=repr(identifier)):
                helper.library_delete(identifier)

    def test_an_entry_replaced_by_a_link_is_not_read(self):
        first = self.install(profile("2026-11-01T10:00:00+00:00"))
        self.install(profile("2026-11-02T10:00:00+00:00"))
        target = self.data / "elsewhere.json"
        target.write_text(json.dumps(profile("2026-11-01T10:00:00+00:00", speaker="alsa_output.evil")))
        entry = helper.LIBRARY / f"{first}.json"
        entry.unlink()
        entry.symlink_to(target)
        with self.assertRaisesRegex(SystemExit, "cannot be read"):
            helper.library_play(first)
        self.assertEqual(self.activated, [])

    def test_a_damaged_index_is_rebuilt_from_the_entries(self):
        first = self.install(profile("2026-11-01T10:00:00+00:00"))
        second = self.install(profile("2026-11-02T10:00:00+00:00"))
        helper.LIBRARY_INDEX.write_text("{ not json")
        self.assertEqual({entry["id"] for entry in helper.library_entries()}, {first, second})

    def test_another_change_running_is_said_and_not_waited_for(self):
        import fcntl
        other = os.open(helper.STATE_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, other)
        fcntl.flock(other, fcntl.LOCK_EX)          # as another process holding it
        with self.assertRaisesRegex(SystemExit, "Another change"):
            with helper.state_lock():
                pass


class TextStatusTests(unittest.TestCase):
    def test_the_terminal_status_lists_the_calibrations(self):
        rows = [{"id": "0123456789ab", "playing": True, "pinned": False, "name": None, "label": "2026-11-08 08:03 · internal mic"},
                {"id": "ba9876543210", "playing": False, "pinned": True, "name": "Shared one", "label": "x"}]
        payload = {"service": "active", "defaultSink": "omarchy_speaker_tuning", "verification": None,
                   "bypass": False, "library": rows, "profile": None}
        printed = io.StringIO()
        with mock.patch.object(helper, "status_payload", lambda: payload), contextlib.redirect_stdout(printed):
            helper.status()
        text = printed.getvalue()
        self.assertIn("* 0123456789ab  2026-11-08 08:03 · internal mic", text)
        self.assertIn("  ba9876543210  Shared one  · pinned", text)


class SharedFileTests(LibraryTestCase):
    """Files in Downloads: what goes out, what comes in, and which the list already holds."""

    OURS = {"sys_vendor": "SLIMBOOK", "product_name": "Executive", "product_version": "",
            "product_sku": "EXE14", "board_name": "EXE14", "label": "SLIMBOOK Executive"}

    def setUp(self):
        super().setUp()
        self.downloads = self.data.parent / "Downloads"
        self.downloads.mkdir()
        self.patch("share_directory", lambda: self.downloads)
        self.patch("hardware_id", lambda: dict(self.OURS))
        self.patch("physical_sinks", lambda: [{"name": SPEAKER, "description": "Built-in Audio"}])
        self.patch("plugin_version", lambda: "1.2.0")
        self.patch("install_now", lambda item: {**item, "library_id": self.install(item), "activation": "live"})

    def exported(self):
        """The one file an export left in Downloads, as JSON."""
        (path,) = self.downloads.glob("*" + helper.SHARE_SUFFIX)
        return path, json.loads(path.read_text())

    def test_an_export_carries_no_place_in_the_list(self):
        self.install(profile("2026-11-01T10:00:00+00:00"))
        helper.export_profile()
        _, payload = self.exported()
        for key in helper.LIBRARY_KEYS:
            self.assertNotIn(key, payload["profile"])

    def test_your_own_export_is_known_to_the_list(self):
        self.install(profile("2026-11-01T10:00:00+00:00"))
        helper.export_profile()
        (row,) = helper.shared_profiles()
        self.assertTrue(row["valid"])
        self.assertTrue(row["in_library"])

    def test_somebody_else_s_file_is_not(self):
        self.install(profile("2026-11-01T10:00:00+00:00"))
        helper.export_profile()
        path, payload = self.exported()
        payload["profile"]["fit"]["filters"][0]["gain_db"] = -2.0   # measured on another machine
        path.write_text(json.dumps(payload))
        (row,) = helper.shared_profiles()
        self.assertFalse(row["in_library"])

    def test_a_loaded_file_is_known_and_leaves_the_measured_one_alone(self):
        measured = self.install(profile("2026-11-01T10:00:00+00:00"))
        before = (helper.LIBRARY / f"{measured}.json").read_text()
        helper.export_profile()
        path, _ = self.exported()
        helper.import_profile(path.name)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["source"] for row in rows], ["imported", "measured"])
        self.assertEqual((helper.LIBRARY / f"{measured}.json").read_text(), before)
        (row,) = helper.shared_profiles()
        self.assertTrue(row["in_library"])

    def test_an_id_carried_in_an_older_file_is_not_trusted(self):
        measured = self.install(profile("2026-11-01T10:00:00+00:00"))
        helper.export_profile()
        path, payload = self.exported()
        payload["profile"]["library_id"] = measured                 # exported before this was fixed
        payload["profile"]["library_played_at"] = "2026-11-01T10:00:00+00:00"
        path.write_text(json.dumps(payload))
        helper.import_profile(path.name)
        ids = [row["id"] for row in self.rows()]
        self.assertEqual(len(ids), 2)
        self.assertIn(measured, ids)
        self.assertEqual(json.loads((helper.LIBRARY / f"{measured}.json").read_text())["created_at"],
                         "2026-11-01T10:00:00+00:00")

    def test_downloads_without_shared_files_reads_nothing_from_the_list(self):
        self.patch("_library_known", lambda: self.fail("the list was read for an empty Downloads"))
        self.assertEqual(helper.shared_profiles(), [])


class IdentityTests(LibraryTestCase):
    def test_two_shared_files_from_the_same_day_are_two_entries(self):
        one = profile("2026-09-16", imported={"file": "a.speaker-calibration.json", "exported_at": "2026-09-16"})
        two = profile("2026-09-16", gain=-2.0, imported={"file": "b.speaker-calibration.json", "exported_at": "2026-09-16"})
        self.assertNotEqual(helper.profile_identity(one), helper.profile_identity(two))

    def test_the_id_is_fixed_once_given(self):
        item = profile("2026-11-01T10:00:00+00:00")
        identifier = self.install(item)
        changed = json.loads(helper.PROFILE.read_text())
        changed["fit"]["filters"][0]["gain_db"] = -1.0
        self.assertEqual(helper.profile_identity(changed), identifier)

    def test_the_list_carries_short_clean_labels_only(self):
        item = profile("2026-09-16", imported={"file": "x.speaker-calibration.json", "name": "<img src=http://x/>" + "a" * 500})
        self.install(item)
        row = self.rows()[0]
        self.assertNotIn("<", row["name"])
        self.assertLessEqual(len(row["name"]), helper.DEVICE_LABEL_LIMIT)
        self.assertNotIn("fit", row)
        self.assertLess(len(json.dumps(self.rows())), 2000)


class MigrationTests(LibraryTestCase):
    def test_the_two_slots_and_a_held_decision_become_the_list(self):
        current, previous = profile("2026-10-01T10:00:00+00:00"), profile("2026-09-01T10:00:00+00:00", gain=-2.0)
        held, proposal = profile("2026-08-01T10:00:00+00:00", gain=-4.0), profile("2026-11-01T10:00:00+00:00", gain=-5.0)
        for path, item in ((helper.PROFILE, current), (helper.PREVIOUS_PROFILE, previous),
                           (helper.HELD_PROFILE, held), (helper.HELD_PREVIOUS, previous), (helper.PROPOSAL, proposal)):
            path.write_text(json.dumps(item))
        # The previous slot is what played.
        helper.COMPARE_STATE.write_text(json.dumps({"active": "previous", "bypass": True}))
        entries = helper.library_entries()
        created = sorted(json.loads((helper.LIBRARY / f"{entry['id']}.json").read_text())["created_at"]
                         for entry in entries)
        self.assertEqual(created, sorted(item["created_at"] for item in (current, previous, held, proposal)))
        self.assertEqual(helper.load_profile(helper.PROFILE)["created_at"], previous["created_at"])
        self.assertEqual(self.activated, [])                 # already audible: written down, not played again
        for path in (helper.PREVIOUS_PROFILE, helper.HELD_PROFILE, helper.HELD_PREVIOUS):
            self.assertFalse(path.exists())
        self.assertTrue(helper.PROPOSAL.exists())             # the last measurement stays the last measurement
        self.assertEqual(json.loads(helper.COMPARE_STATE.read_text()), {"bypass": True})
        self.assertEqual([row["playing"] for row in self.rows()][0], True)

    def test_the_first_status_after_an_update_marks_what_plays(self):
        helper.PROFILE.write_text(json.dumps(profile("2026-10-01T10:00:00+00:00")))
        helper.PREVIOUS_PROFILE.write_text(json.dumps(profile("2026-09-01T10:00:00+00:00", gain=-2.0)))
        with mock.patch.object(helper, "status_payload", STATUS_PAYLOAD), \
             mock.patch.object(helper, "run", lambda args, **options: subprocess.CompletedProcess(args, 0, "", "")), \
             mock.patch.object(helper, "pactl_json", lambda kind: []):
            for name in ("graph_kind", "vendor_trial_active", "vendor_export_status", "load_verification",
                         "loudness_running", "archived_microphones", "hardware_id", "shared_profiles",
                         "status_registry", "asahi_array_hidden", "measurement_support", "sink_description"):
                self.patch(name, lambda *args, **options: None)
            self.patch("unusable_microphones", lambda: [])
            payload = helper.status_payload()
        self.assertEqual([row["playing"] for row in payload["library"]], [True, False])

    def test_the_list_is_not_filled_while_another_change_runs(self):
        old = profile("2026-10-01T10:00:00+00:00")
        helper.PROFILE.write_text(json.dumps(old))
        helper.PREVIOUS_PROFILE.write_text(json.dumps(profile("2026-09-01T10:00:00+00:00", gain=-2.0)))
        # Another process holds the lock: an install, say, half way through.
        other = os.open(helper.STATE_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, other)
        import fcntl
        fcntl.flock(other, fcntl.LOCK_EX)
        with self.assertRaisesRegex(SystemExit, "Another change"):
            helper.library_entries()
        self.assertEqual(self.rows(), [])                # a status simply gets no list this time
        self.assertTrue(helper.PREVIOUS_PROFILE.exists())
        self.assertFalse(helper.LIBRARY_INDEX.exists())
        self.assertEqual(json.loads(helper.PROFILE.read_text()), old)
        fcntl.flock(other, fcntl.LOCK_UN)
        self.assertEqual(len(helper.library_entries()), 2)

    def test_a_locked_command_fills_the_list_without_waiting_on_itself(self):
        helper.PROFILE.write_text(json.dumps(profile("2026-10-01T10:00:00+00:00")))
        with helper.state_lock():
            self.assertEqual(len(helper.library_entries()), 1)

    def test_a_rejected_measurement_is_not_listed(self):
        helper.PROFILE.write_text(json.dumps(profile("2026-10-01T10:00:00+00:00")))
        rejected = profile("2026-11-01T10:00:00+00:00", quality={"accepted": False})
        helper.PROPOSAL.write_text(json.dumps(rejected))
        self.assertEqual(len(helper.library_entries()), 1)

    def test_running_it_again_finds_the_same_list(self):
        helper.PROFILE.write_text(json.dumps(profile("2026-10-01T10:00:00+00:00")))
        first = helper.library_entries()
        self.assertEqual(helper.library_entries(), first)


class SwitchTests(LibraryTestCase):
    """What activate_profile does when the controls alone cannot make the switch."""

    fake_activate = False

    def setUp(self):
        super().setUp()
        self.steps = []
        self.patch("sink_volume_db", lambda sink: 0.0)
        self.patch("service_active", lambda: True)
        self.patch("apply_controls_live", lambda controls: self.steps.append("live") or True)
        self.patch("restart_tuning", lambda: self.steps.append("restart"))

    def write_running_graph(self, speaker):
        helper.FRAGMENT.parent.mkdir(parents=True, exist_ok=True)
        helper.FRAGMENT.write_text(helper.filter_config(speaker, profile("x")["fit"]))

    def test_the_same_speaker_switches_live(self):
        self.write_running_graph(SPEAKER)
        self.assertEqual(helper.activate_profile(profile("2026-11-01T10:00:00+00:00")), "live")
        self.assertEqual(self.steps, ["live"])

    def test_another_speaker_is_never_taken_for_a_live_switch(self):
        # Every control would land, and the sound would stay on the old speaker.
        self.write_running_graph("alsa_output.usb-other.analog-stereo")
        self.assertEqual(helper.activate_profile(profile("2026-11-01T10:00:00+00:00")), "restart")
        self.assertEqual(self.steps, ["restart"])
        self.assertIn(f'target.object = "{SPEAKER}"', helper.FRAGMENT.read_text())

    def test_an_install_for_another_speaker_restarts_too(self):
        # A measurement, a shared file and a registry calibration all arrive
        # through install_profile, which must not write the graph first.
        self.write_running_graph("alsa_output.usb-other.analog-stereo")
        probe = self.config / "limiter_stereo.ttl"
        probe.write_text("")
        self.patch("LIMITER_PROBE", probe)
        self.patch("HOST", self.config / "pipewire/omarchy-speaker-tuning.conf")
        self.patch("UNIT", self.config / "systemd/user/omarchy-speaker-tuning.service")
        self.patch("run", lambda args, **options: subprocess.CompletedProcess(args, 0, "", ""))
        self.assertEqual(helper.install_profile(profile("2026-11-01T10:00:00+00:00")), "restart")
        self.assertEqual(self.steps, ["restart"])
        self.assertIn(f'target.object = "{SPEAKER}"', helper.FRAGMENT.read_text())

    def test_a_stopped_tuning_simply_starts(self):
        self.patch("service_active", lambda: False)
        self.assertEqual(helper.activate_profile(profile("2026-11-01T10:00:00+00:00")), "restart")
        self.assertEqual(self.steps, ["restart"])


class RestartTests(unittest.TestCase):
    """What a restart of the tuning does to the streams playing."""

    SINKS = [{"index": 1, "name": helper.VIRTUAL_SINK}, {"index": 2, "name": "headphones"}]
    STREAMS = [{"index": 10, "sink": 1, "properties": {"application.name": "mpv"}},
               {"index": 11, "sink": 2, "properties": {"application.name": "Firefox"}},
               {"index": 12, "sink": 1, "properties": {}}]

    def restart(self, *, running, default):
        ran, moved_all = [], []
        def fake_run(args, **options):
            ran.append(args)
            return subprocess.CompletedProcess(args, 0, default + "\n" if args[1:] == ["get-default-sink"] else "", "")
        with mock.patch.object(helper, "pactl_json", lambda kind: self.SINKS if kind == "sinks" else self.STREAMS), \
             mock.patch.object(helper, "run", fake_run), \
             mock.patch.object(helper, "calibration_sink_present", lambda: running), \
             mock.patch.object(helper, "wait_for_sink", lambda name, attempts=30: True), \
             mock.patch.object(helper, "move_apps", lambda target: moved_all.append(target)):
            helper.restart_tuning()
        return ran, moved_all

    def test_a_restart_puts_back_only_what_played_through_the_calibration(self):
        ran, moved_all = self.restart(running=True, default="headphones")
        self.assertEqual(moved_all, [])
        self.assertIn(["pactl", "move-sink-input", "10", helper.VIRTUAL_SINK], ran)
        self.assertFalse(any(args[:3] == ["pactl", "move-sink-input", "11"] for args in ran))
        self.assertFalse(any(args[1] == "set-default-sink" for args in ran))   # the default was the headphones

    def test_a_restart_keeps_the_calibration_the_default_when_it_was(self):
        ran, _ = self.restart(running=True, default=helper.VIRTUAL_SINK)
        self.assertIn(["pactl", "set-default-sink", helper.VIRTUAL_SINK], ran)

    def test_switching_it_on_takes_everything_that_plays(self):
        _, moved_all = self.restart(running=False, default="headphones")
        self.assertEqual(moved_all, [helper.VIRTUAL_SINK])


if __name__ == "__main__":
    unittest.main()
