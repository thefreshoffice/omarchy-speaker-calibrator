#!/usr/bin/env python3

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

import calibration_share as share  # noqa: E402
from test_calibration_share import VERIFIED, shared  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "speaker_calibrate_registry", Path(__file__).resolve().parents[1] / "speaker-calibrate.py"
)
speaker_calibrate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(speaker_calibrate)

OURS = {"sys_vendor": "SLIMBOOK", "product_name": "Executive-14-UC2", "product_version": "Standard",
        "product_sku": "Executive-14-UC2", "board_name": "Executive-14-UC2", "label": "SLIMBOOK Executive-14-UC2"}
SINK = {"name": "alsa_output.pci-0000_00_1f.3.analog-stereo", "description": "Built-in Audio"}
GOOD_ID = "2026-09-20-calibrated-0123456789"


def row(identifier=GOOD_ID, **changes):
    base = {"id": identifier, "microphone_kind": "calibrated measuring microphone", "created_at": "2026-09-20",
            "score": 88, "votes": 3, "issue": 7, "filters": 4, "error_before_db": 5.4, "error_after_db": 1.8,
            "hardware": dict(OURS), "path": "profiles/../../../etc/passwd", "submitted_by": "<b>someone</b>",
            "verification": {"verdict": "pass", "target_error_before_db": 5.5, "target_error_after_db": 1.8}}
    base.update(changes)
    return base


class ClientTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        data = Path(self.folder.name) / "data"
        data.mkdir(mode=0o700)
        self.data = data
        for name, value in (("DATA", data), ("REGISTRY_STATE", data / "registry.json"),
                            ("REGISTRY_CACHE", data / "registry-cache.json"),
                            ("PROFILE", data / "active-profile.json"), ("PROPOSAL", data / "proposed-profile.json"),
                            ("VERIFICATION", data / "verification.json")):
            patch = mock.patch.object(speaker_calibrate, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        for name, value in (("hardware_id", lambda: dict(OURS)), ("pactl_json", lambda kind: [SINK]),
                            ("plugin_version", lambda: "1.2.0")):
            patch = mock.patch.object(speaker_calibrate, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def allow(self):
        speaker_calibrate.write_registry_state(lookup="allowed")


class ConsentTests(ClientTestCase):
    def test_nothing_is_asked_of_anyone_before_a_yes(self):
        with mock.patch.object(speaker_calibrate, "registry_fetch") as fetched:
            found = speaker_calibrate.registry_lookup()
            self.assertEqual((found["consent"], found["profiles"]), (None, []))
            speaker_calibrate.write_registry_state(lookup="declined")
            self.assertEqual(speaker_calibrate.registry_lookup(refresh=True)["consent"], "declined")
            with self.assertRaisesRegex(SystemExit, "not been allowed"):
                speaker_calibrate.registry_load(GOOD_ID)
        fetched.assert_not_called()

    def test_after_a_yes_it_asks_once_a_day_and_the_status_only_reads_the_cache(self):
        self.allow()
        document = json.dumps({"profiles": [row()]})
        with mock.patch.object(speaker_calibrate, "registry_fetch", return_value=document) as fetched:
            first = speaker_calibrate.registry_lookup()
            again = speaker_calibrate.registry_lookup()
            cached = speaker_calibrate.status_registry()
            self.assertEqual(fetched.call_count, 1)
            fetched.assert_called_with("index/slimbook/executive-14-uc2.json")
            speaker_calibrate.registry_lookup(refresh=True)
            self.assertEqual(fetched.call_count, 2)
        self.assertEqual([item["id"] for item in first["profiles"]], [GOOD_ID])
        self.assertEqual(again["profiles"], first["profiles"])
        self.assertEqual(cached["profiles"], first["profiles"])

    def test_a_machine_with_no_shared_calibrations_is_an_empty_list_not_an_error(self):
        self.allow()
        with mock.patch.object(speaker_calibrate, "registry_fetch", return_value=None):
            self.assertEqual(speaker_calibrate.registry_lookup()["profiles"], [])

    def test_a_state_file_someone_else_wrote_is_read_with_suspicion(self):
        speaker_calibrate.REGISTRY_STATE.write_text(json.dumps({
            "lookup": "always", "share_explained": "yes",
            "uploads": {GOOD_ID: "https://evil.example/issues/1", "bad id": "x",
                        "2026-09-20-builtin-aaaaaaaaaa": f"https://github.com/{share.REGISTRY_REPOSITORY}/issues/12"}}))
        speaker_calibrate.REGISTRY_STATE.chmod(0o600)
        self.assertEqual(speaker_calibrate.registry_state(), {
            "lookup": None, "share_explained": False,
            "uploads": {"2026-09-20-builtin-aaaaaaaaaa": f"https://github.com/{share.REGISTRY_REPOSITORY}/issues/12"}})


class IndexTests(ClientTestCase):
    def test_only_what_can_be_shown_safely_survives_and_the_best_match_comes_first(self):
        rows = speaker_calibrate.valid_index_rows({"profiles": [
            row("2026-09-19-builtin-aaaaaaaaaa", microphone_kind="built-in microphone", score=30),
            row(score=88, hardware=dict(OURS, product_sku="Other", board_name="Other")),     # same product, tier 2
            row("2026-09-18-external-bbbbbbbbbb", microphone_kind="external microphone", score=60),
            row("../../etc/passwd"), row("2026-09-20-calibrated-cccccccccc", microphone_kind="<img src=x>"),
            row("2026-09-20-calibrated-dddddddddd", hardware=dict(OURS, product_name="Executive-16")),
            row("2026-09-20-calibrated-eeeeeeeeee", hardware=dict(OURS, sys_vendor="Dell Inc.")),
            "nonsense", None, {"id": 7},
        ]}, OURS)
        self.assertEqual([item["id"] for item in rows],
                         ["2026-09-18-external-bbbbbbbbbb", "2026-09-19-builtin-aaaaaaaaaa", GOOD_ID])
        self.assertEqual([item["tier"] for item in rows], [3, 3, 2])
        text = json.dumps(rows)
        for untrusted in ("etc/passwd", "someone", "<b>", "path", "submitted_by"):
            self.assertNotIn(untrusted, text)

    def test_numbers_that_are_not_numbers_become_nothing(self):
        kept = speaker_calibrate.valid_index_rows({"profiles": [row(
            score=10 ** 400, votes=-5, issue="7; rm -rf", filters=float("nan"),
            error_before_db="5", verification="pass", created_at="<script>")]}, OURS)[0]
        self.assertEqual((kept["score"], kept["votes"], kept["issue"], kept["filters"], kept["error_before_db"],
                          kept["checked"], kept["created_at"]), (0, 0, None, None, None, None, ""))

    def test_a_list_that_is_not_a_list_and_too_many_rows(self):
        self.assertEqual(speaker_calibrate.valid_index_rows({"profiles": "all of them"}, OURS), [])
        self.assertEqual(speaker_calibrate.valid_index_rows([], OURS), [])
        many = [row(f"2026-09-20-external-{index:010x}", microphone_kind="external microphone") for index in range(400)]
        self.assertEqual(len(speaker_calibrate.valid_index_rows({"profiles": many}, OURS)),
                         speaker_calibrate.REGISTRY_ROWS_LIMIT)


class FetchTests(ClientTestCase):
    class Response:
        def __init__(self, status, body):
            self.status, self.body, self.sent = status, body, 0

        def read(self, size):
            chunk = self.body[self.sent:self.sent + size]
            self.sent += len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fetch(self, path, response=None, error=None):
        opener = mock.Mock()
        if error:
            opener.open.side_effect = error
        else:
            opener.open.return_value = response
        with mock.patch("urllib.request.build_opener", return_value=opener) as built:
            try:
                return speaker_calibrate.registry_fetch(path), opener, built
            except SystemExit as stop:
                return stop, opener, built

    def test_one_fixed_origin_no_proxy_no_redirects_no_credentials(self):
        text, opener, built = self.fetch("index/slimbook/executive-14-uc2.json", self.Response(200, b'{"profiles": []}'))
        self.assertEqual(text, '{"profiles": []}')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, share.REGISTRY_ORIGIN + "index/slimbook/executive-14-uc2.json")
        self.assertTrue(request.full_url.startswith("https://raw.githubusercontent.com/thefreshoffice/omarchy-speaker-profiles/main/"))
        self.assertEqual(sorted(key.lower() for key in request.headers), ["accept", "user-agent"])
        handlers = built.call_args.args
        proxy = next(item for item in handlers if item.__class__.__name__ == "ProxyHandler")
        self.assertEqual(proxy.proxies, {})
        redirect = next(item for item in handlers if isinstance(item, type) and "Redirect" in item.__name__)
        self.assertIsNone(redirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/"))

    def test_paths_are_the_registry_s_two_shapes_and_nothing_else(self):
        for path in ("../secrets", "index/../../x.json", "https://evil.example/x.json", "index/a/b.json?x=1",
                     "profiles/a/b/c.svg", "index/A/b.json", "", "profiles/a/b/../../c.json"):
            stop, opener, _ = self.fetch(path, self.Response(200, b"{}"))
            self.assertIsInstance(stop, SystemExit, path)
            opener.open.assert_not_called()

    def test_missing_is_none_too_much_is_refused_and_unreachable_is_said(self):
        missing = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        self.assertIsNone(self.fetch("index/a/b.json", error=missing)[0])
        self.assertIsNone(self.fetch("index/a/b.json", self.Response(204, b""))[0])
        huge = self.fetch("index/a/b.json", self.Response(200, b"x" * (speaker_calibrate.REGISTRY_FETCH_LIMIT + 50)))[0]
        self.assertIn("more than any of its files", str(huge))
        broken = self.fetch("index/a/b.json", error=urllib.error.URLError("no route"))[0]
        self.assertIn("could not be reached", str(broken))
        refused = self.fetch("index/a/b.json", error=urllib.error.HTTPError("u", 500, "x", {}, None))[0]
        self.assertIn("error (500)", str(refused))


class LoadTests(ClientTestCase):
    def test_the_path_is_built_from_this_machine_s_model_and_the_file_is_held_to_the_import_rules(self):
        self.allow()
        public = share.public_payload(shared(), VERIFIED)
        with mock.patch.object(speaker_calibrate, "registry_fetch", return_value=json.dumps(public)) as fetched:
            result = speaker_calibrate.registry_load(GOOD_ID)
        fetched.assert_called_once_with(f"profiles/slimbook/executive-14-uc2/{GOOD_ID}.json")
        proposal = result["proposal"]
        self.assertEqual(proposal["speaker"]["name"], SINK["name"])              # this machine's, from its own list
        self.assertEqual(proposal["imported"]["registry"], GOOD_ID)
        self.assertTrue(result["matches"]["machine"])
        self.assertTrue(speaker_calibrate.PROPOSAL.exists())

    def test_what_is_not_a_calibration_is_refused_whoever_served_it(self):
        self.allow()
        outside = share.public_payload(shared())
        outside["profile"]["fit"]["filters"][0]["gain_db"] = 30.0
        for text, reason in ((json.dumps(outside), "gain outside"), ("[" * 100000, "not a shared calibration"),
                             (json.dumps({"format": "x"}), "not a shared calibration"), (None, "not in the registry")):
            with mock.patch.object(speaker_calibrate, "registry_fetch", return_value=text):
                with self.assertRaisesRegex(SystemExit, reason):
                    speaker_calibrate.registry_load(GOOD_ID)
        self.assertFalse(speaker_calibrate.PROPOSAL.exists())

    def test_a_name_that_is_not_a_registry_name_never_becomes_a_request(self):
        self.allow()
        with mock.patch.object(speaker_calibrate, "registry_fetch") as fetched:
            for identifier in ("../../etc/passwd", "2026-09-20-calibrated-XYZ", "", None, GOOD_ID + "/../x"):
                with self.assertRaises(SystemExit):
                    speaker_calibrate.registry_load(identifier)
        fetched.assert_not_called()


class ShareTests(ClientTestCase):
    def install(self, created="2026-09-20T11:27:27.253765+00:00"):
        profile = shared()["profile"]
        profile["created_at"] = created
        speaker_calibrate.PROFILE.write_text(json.dumps(profile))
        speaker_calibrate.PROFILE.chmod(0o600)

    def test_a_check_counts_only_for_the_calibration_it_checked(self):
        self.install()
        for checked, expected in (
            (dict(VERIFIED, profile_created_at="2026-09-20T11:27:27.253765+00:00"), "pass"),
            (dict(VERIFIED, profile_created_at="2026-09-01T00:00:00+00:00"), None),
            (dict(VERIFIED, profile_created_at="2026-09-20T11:27:27.253765+00:00", stale=True), None),
        ):
            speaker_calibrate.VERIFICATION.write_text(json.dumps(checked))
            speaker_calibrate.VERIFICATION.chmod(0o600)
            public, _, _ = speaker_calibrate.public_profile()
            self.assertEqual((public["public"].get("verification") or {}).get("verdict"), expected)

    def test_one_press_the_profile_goes_to_the_github_tool_on_standard_input(self):
        self.install()
        created = subprocess.CompletedProcess([], 0, stdout=f"https://github.com/{share.REGISTRY_REPOSITORY}/issues/12\n", stderr="")
        with mock.patch.object(speaker_calibrate, "gh_signed_in", return_value=True), \
             mock.patch.object(speaker_calibrate.subprocess, "run", return_value=created) as ran:
            result = speaker_calibrate.share_upload()
            again = speaker_calibrate.share_upload()
        self.assertEqual(ran.call_count, 1)                                     # the second press uploads nothing
        command = ran.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/gh", "issue", "create"])
        self.assertEqual(command[command.index("--repo") + 1], share.REGISTRY_REPOSITORY)
        self.assertEqual(command[-2:], ["--body-file", "-"])
        self.assertNotIn(share.SUBMISSION_PREFIX, " ".join(command))            # never on the command line
        body = ran.call_args.kwargs["input"]
        self.assertEqual(share.decode_submission(body), speaker_calibrate.public_profile()[0])
        self.assertEqual(result["url"], f"https://github.com/{share.REGISTRY_REPOSITORY}/issues/12")
        self.assertEqual(again["url"], result["url"])
        self.assertEqual(speaker_calibrate.registry_state()["uploads"], {result["id"]: result["url"]})

    def test_an_answer_that_is_not_the_registry_s_issue_is_not_recorded(self):
        self.install()
        for done in (subprocess.CompletedProcess([], 0, stdout="https://evil.example/issues/1\n", stderr=""),
                     subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 403 <b>forbidden</b>")):
            with mock.patch.object(speaker_calibrate, "gh_signed_in", return_value=True), \
                 mock.patch.object(speaker_calibrate.subprocess, "run", return_value=done):
                with self.assertRaisesRegex(SystemExit, "did not accept"):
                    speaker_calibrate.share_upload()
        self.assertEqual(speaker_calibrate.registry_state()["uploads"], {})

    def test_without_the_tool_it_says_so_and_runs_nothing(self):
        self.install()
        with mock.patch.object(speaker_calibrate, "gh_signed_in", return_value=False), \
             mock.patch.object(speaker_calibrate.subprocess, "run") as ran:
            with self.assertRaisesRegex(SystemExit, "browser"):
                speaker_calibrate.share_upload()
        ran.assert_not_called()

    def test_the_browser_way_puts_it_on_the_clipboard_and_names_the_form(self):
        self.install()
        with mock.patch.object(speaker_calibrate.subprocess, "run") as ran:
            result = speaker_calibrate.share_for_browser()
        self.assertEqual(ran.call_args.args[0], ["/usr/bin/wl-copy"])
        self.assertTrue(ran.call_args.kwargs["input"].startswith(share.SUBMISSION_PREFIX))
        self.assertTrue(result["url"].startswith(f"https://github.com/{share.REGISTRY_REPOSITORY}/issues/new?template=profile.yml&title="))
        self.assertNotIn(" ", result["url"])

    def test_the_status_says_what_would_be_sent_without_sending_it(self):
        self.install()
        with mock.patch.object(speaker_calibrate, "gh_signed_in", return_value=True), \
             mock.patch.object(speaker_calibrate, "registry_fetch") as fetched, \
             mock.patch.object(speaker_calibrate.subprocess, "run") as ran:
            status = speaker_calibrate.share_status()
        fetched.assert_not_called()
        ran.assert_not_called()
        self.assertEqual(status["name"], "SLIMBOOK Executive-14-UC2 · calibrated measuring microphone · 2026-09-20")
        self.assertTrue(status["one_press"])
        self.assertIsNone(status["uploaded"])
        self.assertLess(status["bytes"], 20000)


class ExportGraphTests(ClientTestCase):
    def test_an_export_comes_with_its_picture(self):
        downloads = Path(self.folder.name) / "Downloads"
        downloads.mkdir()
        profile = shared()["profile"]
        speaker_calibrate.PROFILE.write_text(json.dumps(profile))
        speaker_calibrate.PROFILE.chmod(0o600)
        with mock.patch.object(speaker_calibrate, "share_directory", lambda: downloads):
            result = speaker_calibrate.export_profile()
        self.assertTrue((downloads / result["file"]).exists())
        svg = (downloads / result["graph"]).read_text()
        self.assertTrue(svg.startswith("<svg "))
        self.assertIn("SLIMBOOK Executive-14-UC2", svg)


if __name__ == "__main__":
    unittest.main()
