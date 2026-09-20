#!/usr/bin/env python3

import importlib.util
import json
import re
import time
import os
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

        def read1(self, size):
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


    def test_one_clock_covers_the_whole_request_the_name_lookup_included(self):
        import threading
        release = threading.Event()
        self.addCleanup(release.set)
        opener = mock.Mock()
        opener.open.side_effect = lambda *args, **kwargs: release.wait(30)       # a resolver that never answers
        started = time.monotonic()
        with mock.patch("urllib.request.build_opener", return_value=opener), \
             mock.patch.object(speaker_calibrate, "REGISTRY_TIMEOUT_SECONDS", 0.3):
            with self.assertRaisesRegex(SystemExit, "too long"):
                speaker_calibrate.registry_fetch("index/a/b.json")
        self.assertLess(time.monotonic() - started, 3.0)


class PanelPageTests(unittest.TestCase):
    """The panel opens two kinds of page and no other; the shapes are in Service.qml, checked here as they stand."""

    def shapes(self):
        source = (Path(__file__).resolve().parents[1] / "Service.qml").read_text()
        block = source[source.index("_registryPages: ["):source.index("function openRegistryPage")]
        found = re.findall(r"^\s*/(\^.+\$)/,?\s*$", block, re.M)
        self.assertEqual(len(found), 2)
        return [re.compile(item.replace("\\/", "/")) for item in found]

    def opens(self, address):
        return len(address) <= 600 and any(shape.fullmatch(address) for shape in self.shapes())

    def test_what_the_helper_answers_is_opened(self):
        self.assertTrue(self.opens(f"https://github.com/{share.REGISTRY_REPOSITORY}/issues/12"))
        import urllib.parse
        for label in ("SLIMBOOK Executive-14-UC2", "Dell Inc. XPS 13 9310 (2-in-1) / A&B #3, rev:2"):
            title = f"Calibration for {label} (external microphone)"
            address = (f"https://github.com/{share.REGISTRY_REPOSITORY}/issues/new?"
                       + urllib.parse.urlencode({"template": "profile.yml", "title": title}))
            self.assertTrue(self.opens(address), address)

    def test_nothing_else_is(self):
        base = f"https://github.com/{share.REGISTRY_REPOSITORY}"
        for address in (f"{base}/../../evil/repo/issues/1", f"{base}/issues/1/../../../../evil", f"{base}/issues/1?x=1",
                        f"{base}/issues/1#x", f"{base}.evil.example/issues/1", f"https://github.com.evil.example/issues/1",
                        f"http://github.com/{share.REGISTRY_REPOSITORY}/issues/1", f"{base}/issues/new?template=evil.yml&title=x",
                        f"{base}/issues/new?template=profile.yml&title=x&body=y", f"{base}/issues/new?template=profile.yml&title=<img>",
                        f"https://user@github.com/{share.REGISTRY_REPOSITORY}/issues/1", "file:///etc/passwd", "javascript:alert(1)",
                        f"{base}/issues/1\nhttps://evil.example", f"{base}/issues/new?template=profile.yml&title=" + "a" * 700, ""):
            self.assertFalse(self.opens(address), address)


class BoundedToolTests(unittest.TestCase):
    """The runner the GitHub tool goes through, against real processes."""

    def test_what_it_is_given_arrives_on_standard_input_and_the_answer_comes_back(self):
        # More than a pipe holds, so writing and reading have to take turns.
        code, out, err = speaker_calibrate.run_bounded(["/usr/bin/cat"], input_text="profile " * 20000, limit=200_000)
        self.assertEqual((code, len(out), err), (0, 160000, ""))
        code, out, err = speaker_calibrate.run_bounded(["/usr/bin/cat"], input_text="hello", limit=100)
        self.assertEqual((code, out, err), (0, "hello", ""))
        code, out, err = speaker_calibrate.run_bounded(["/usr/bin/sh", "-c", "echo no >&2; exit 3"])
        self.assertEqual((code, out, err), (3, "", "no\n"))

    def test_a_tool_that_talks_without_end_is_stopped_not_remembered(self):
        started = time.monotonic()
        with self.assertRaisesRegex(speaker_calibrate.ToolFailed, "more than it should"):
            speaker_calibrate.run_bounded(["/usr/bin/yes"], limit=50_000, seconds=20)
        self.assertLess(time.monotonic() - started, 10.0)

    def test_one_deadline_for_the_whole_run_and_nothing_left_behind(self):
        marker = f"calibrator-test-{os.getpid()}-{time.monotonic_ns()}"
        started = time.monotonic()
        with self.assertRaisesRegex(speaker_calibrate.ToolFailed, "too long"):
            # The tool starts a helper of its own; both belong to the group that is ended.
            speaker_calibrate.run_bounded(["/usr/bin/sh", "-c", f"sleep 60 & exec -a {marker} sleep 60"], seconds=0.5)
        self.assertLess(time.monotonic() - started, 8.0)
        time.sleep(0.2)
        left = subprocess.run(["/usr/bin/pgrep", "-f", marker], capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "")

    def test_a_tool_that_is_not_there_is_a_failure_not_a_crash(self):
        with self.assertRaises(speaker_calibrate.ToolFailed):
            speaker_calibrate.run_bounded(["/usr/bin/no-such-tool-here"])

    def test_the_github_tool_inherits_no_path_host_token_or_proxy(self):
        hostile = {"PATH": "/tmp/evil:/usr/bin", "GH_HOST": "evil.example", "GH_TOKEN": "ghp_x", "GH_REPO": "evil/repo",
                   "GH_CONFIG_DIR": "/tmp/evil", "HTTPS_PROXY": "http://evil.example:8080", "GIT_DIR": "/tmp/evil",
                   "LD_PRELOAD": "/tmp/evil.so", "BASH_ENV": "/tmp/evil.sh", "HOME": "/home/someone",
                   "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
        with mock.patch.dict(os.environ, hostile, clear=True):
            env = speaker_calibrate.gh_environment()
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["GH_HOST"], "github.com")
        self.assertEqual((env["HOME"], env["DBUS_SESSION_BUS_ADDRESS"]), ("/home/someone", "unix:path=/run/user/1000/bus"))
        for gone in ("GH_TOKEN", "GH_REPO", "GH_CONFIG_DIR", "HTTPS_PROXY", "GIT_DIR", "LD_PRELOAD", "BASH_ENV"):
            self.assertNotIn(gone, env)
        with mock.patch.object(speaker_calibrate, "run_bounded", return_value=(0, "", "")) as ran, \
             mock.patch.object(speaker_calibrate.Path, "exists", return_value=True):
            self.assertTrue(speaker_calibrate.gh_signed_in())
        self.assertEqual(ran.call_args.kwargs["env"]["PATH"], "/usr/bin:/bin")


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
        created = (0, f"https://github.com/{share.REGISTRY_REPOSITORY}/issues/12\n", "")
        with mock.patch.object(speaker_calibrate, "gh_signed_in", return_value=True), \
             mock.patch.object(speaker_calibrate, "run_bounded", return_value=created) as ran:
            result = speaker_calibrate.share_upload()
            again = speaker_calibrate.share_upload()
        self.assertEqual(ran.call_count, 1)                                     # the second press uploads nothing
        command = ran.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/gh", "issue", "create"])
        self.assertEqual(command[command.index("--repo") + 1], "github.com/" + share.REGISTRY_REPOSITORY)   # the host is pinned
        self.assertEqual(command[-2:], ["--body-file", "-"])
        self.assertNotIn(share.SUBMISSION_PREFIX, " ".join(command))            # never on the command line
        body = ran.call_args.kwargs["input_text"]
        self.assertEqual(ran.call_args.kwargs["env"], speaker_calibrate.gh_environment())
        self.assertEqual(share.decode_submission(body), speaker_calibrate.public_profile()[0])
        self.assertEqual(result["url"], f"https://github.com/{share.REGISTRY_REPOSITORY}/issues/12")
        self.assertEqual(again["url"], result["url"])
        self.assertEqual(speaker_calibrate.registry_state()["uploads"], {result["id"]: result["url"]})

    def test_an_answer_that_is_not_the_registry_s_issue_is_not_recorded(self):
        self.install()
        for done in ((0, "https://evil.example/issues/1\n", ""),
                     (1, "", "HTTP 403 <b>forbidden</b>")):
            with mock.patch.object(speaker_calibrate, "gh_signed_in", return_value=True), \
                 mock.patch.object(speaker_calibrate, "run_bounded", return_value=done):
                with self.assertRaisesRegex(SystemExit, "did not accept"):
                    speaker_calibrate.share_upload()
        self.assertEqual(speaker_calibrate.registry_state()["uploads"], {})

    def test_without_the_tool_it_says_so_and_runs_nothing(self):
        self.install()
        with mock.patch.object(speaker_calibrate, "gh_signed_in", return_value=False), \
             mock.patch.object(speaker_calibrate, "run_bounded") as ran:
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

    def test_the_button_asks_the_first_time_and_is_one_press_after_that(self):
        self.install()
        created = (0, f"https://github.com/{share.REGISTRY_REPOSITORY}/issues/5\n", "")
        with mock.patch.object(speaker_calibrate, "gh_signed_in", return_value=True), \
             mock.patch.object(speaker_calibrate, "run_bounded", return_value=created) as ran:
            first = speaker_calibrate.share_press()
            self.assertEqual((first["state"], first["one_press"]), ("confirm", True))
            ran.assert_not_called()                                              # asked, nothing sent
            second = speaker_calibrate.share_press(confirmed=True)
            self.assertEqual(second["state"], "uploaded")
            self.assertEqual(ran.call_count, 1)
            third = speaker_calibrate.share_press()
            self.assertEqual((third["state"], third["url"]), ("uploaded", second["url"]))
            self.assertEqual(ran.call_count, 1)                                  # already shared: nothing sent again
        # A later calibration, once the explanation has been seen, is one press.
        self.install(created="2026-10-01T09:00:00+00:00")
        with mock.patch.object(speaker_calibrate, "gh_signed_in", return_value=True), \
             mock.patch.object(speaker_calibrate, "run_bounded", return_value=created) as ran:
            self.assertEqual(speaker_calibrate.share_press()["state"], "uploaded")
            self.assertEqual(ran.call_count, 1)

    def test_without_the_tool_the_same_button_goes_by_the_browser(self):
        self.install()
        with mock.patch.object(speaker_calibrate, "gh_signed_in", return_value=False), \
             mock.patch.object(speaker_calibrate.subprocess, "run") as ran:
            self.assertEqual(speaker_calibrate.share_press()["one_press"], False)
            ran.assert_not_called()
            pressed = speaker_calibrate.share_press(confirmed=True)
        self.assertEqual(pressed["state"], "browser")
        self.assertEqual(ran.call_args.args[0], ["/usr/bin/wl-copy"])

    def test_the_status_knows_what_was_shared_without_asking_anyone(self):
        self.install()
        identifier = speaker_calibrate.public_profile()[1]
        url = f"https://github.com/{share.REGISTRY_REPOSITORY}/issues/9"
        with mock.patch.object(speaker_calibrate, "gh_signed_in") as asked, \
             mock.patch.object(speaker_calibrate, "registry_fetch") as fetched, \
             mock.patch.object(speaker_calibrate.subprocess, "run") as ran:
            self.assertIsNone(speaker_calibrate.status_registry()["shared_url"])
            speaker_calibrate.write_registry_state(uploads={identifier: url})
            self.assertEqual(speaker_calibrate.status_registry()["shared_url"], url)
        for untouched in (asked, fetched, ran):
            untouched.assert_not_called()

    def test_the_panel_asks_nothing_of_github_before_a_press(self):
        root = Path(speaker_calibrate.__file__).parent
        panel, service = (root / "Panel.qml").read_text(), (root / "Service.qml").read_text()
        self.assertNotIn("share-status-json", service)
        self.assertNotIn("shareStatusCheck", panel + service)
        self.assertEqual(panel.count("service.share("), 1)                       # only the button's own click
        button = panel.index("Share this calibration with everyone")
        self.assertLess(button, panel.index('label: "Advanced"'))                # in the main panel, above the switch
        self.assertGreater(button, panel.index('"Calibrate again"'))

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
