#!/usr/bin/env python3

import base64
import copy
import gzip
import importlib.util
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

import calibration_share as share  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "speaker_calibrate_share", Path(__file__).resolve().parents[1] / "speaker-calibrate.py"
)
speaker_calibrate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(speaker_calibrate)

POINTS = 64
FREQUENCIES = [50.0 * (400.0 ** (index / (POINTS - 1))) for index in range(POINTS)]


def shared(**profile_changes):
    profile = {
        "schema_version": 5, "plugin_version": "1.2.0", "created_at": "2026-09-20T11:27:27.253765+00:00",
        "speaker": {"name": "alsa_output.pci-0000_00_1f.3.analog-stereo", "description": "Built-in Audio"},
        "microphone": {"name": "alsa_input.usb-Sennheiser_BTD_700_09B88CA972B269AB3C08-02.mono-fallback",
                       "description": "Usb Microphone", "channel": 0, "internal": False,
                       "calibration_file": "/home/someone/umik-7091234.txt"},
        "voicing": "neutral", "loudness": "matched", "bass": "full", "channel_trim": "off",
        "deep_bass": "on", "loudness_compensation": "on",
        "quality": {"accepted": True, "verdict": "warning",
                    "warnings": ["Sweep repeatability is limited (1.6 dB).", "<img src=x>"],
                    "guidance": ["Visit https://example.com for cheap speakers"],
                    "metrics": {"worst_repeatability_db": 0.4, "clipped_samples": 0, "measurement_level": "good"}},
        "safety": {"maximum_boost_db": 6.0},
        "fit": {"filter_count": 2, "input_gain_linear": 0.8, "makeup_db": 2.0,
                "weighted_rmse_before_db": 5.4, "weighted_rmse_after_db": 1.8,
                "filters": [{"type": "peaking", "frequency_hz": 600.0, "q": 2.6, "gain_db": -8.5},
                            {"type": "peaking", "frequency_hz": 2500.0, "q": 1.0, "gain_db": -6.0}],
                "highpass": {"frequency_hz": 190.0, "q": 0.707, "stages": 1},
                "measured_smoothed_db": [-30.0 + index * 0.1 for index in range(POINTS)],
                "correction_response_db": [-4.0] * POINTS,
                "predicted_response_db": [-34.0 + index * 0.1 for index in range(POINTS)],
                "total_cut_limit_db": [-15.0] * POINTS, "boost_decisions": [{"why": "x"}],
                "optimizer_message": "Buy now at https://example.com !!!"},
        "measurement": {"frequency_hz": FREQUENCIES, "level_dbfs": [-30.0] * POINTS,
                        "validation_curves": [{"level_dbfs": [0.0] * POINTS}] * 12,
                        "channels": [{"secret": "per channel detail"}],
                        "microphone_calibration": {"path": "/home/someone/umik-7091234.txt"},
                        "level_search": {"attempts": [{"level_dbfs": -24.0}]},
                        "method": "repeated-exponential-sine-sweep"},
    }
    profile.update(profile_changes)
    return {"format": speaker_calibrate.SHARE_FORMAT, "name": "anything <b>at all</b>", "plugin_version": "1.2.0",
            "hardware": {"sys_vendor": "SLIMBOOK", "product_name": "Executive-14-UC2", "product_version": "Standard",
                         "product_sku": "Executive-14-UC2", "board_name": "Executive-14-UC2",
                         "label": "ignored", "speaker": "alsa_output.pci-0000_00_1f.3.analog-stereo",
                         "speaker_description": "Visit https://example.com"},
            "profile": profile}


VERIFIED = {"usable": True, "verdict": "pass", "model_error_db": 0.8,
            "target_error_db": {"before": 5.5, "after": 1.8}}


def strings_in(value, found=None):
    found = [] if found is None else found
    if isinstance(value, dict):
        for item in value.values():
            strings_in(item, found)
    elif isinstance(value, list):
        for item in value:
            strings_in(item, found)
    elif isinstance(value, str):
        found.append(value)
    return found


class PublicPayloadTests(unittest.TestCase):
    def test_nothing_that_names_a_person_a_path_or_a_device_serial_travels(self):
        text = json.dumps(share.public_payload(shared(), VERIFIED))
        for private in ("/home/", "someone", "7091234", "09B88CA972B269AB3C08", "Sennheiser", "example.com",
                        "<img", "<b>", "Buy now", "per channel detail", "validation_curves", "level_search",
                        "boost_decisions", "total_cut_limit_db"):
            self.assertNotIn(private, text, private)

    def test_the_only_strings_left_are_the_firmware_s_and_the_plugin_s_own_words(self):
        public = share.public_payload(shared(), VERIFIED)
        for value in strings_in(public):
            self.assertRegex(value, r"^[A-Za-z0-9][A-Za-z0-9 ._()+/&,#:·-]{0,119}$", value)
        self.assertEqual(public["name"], "SLIMBOOK Executive-14-UC2 · calibrated measuring microphone · 2026-09-20")
        self.assertEqual(public["profile"]["created_at"], "2026-09-20")        # the day, not the second

    def test_it_is_still_a_calibration_the_plugin_can_load(self):
        public = share.public_payload(shared(), VERIFIED)
        loaded = speaker_calibrate.valid_shared_payload(json.loads(json.dumps(public)))
        self.assertEqual(len(loaded["profile"]["fit"]["filters"]), 2)
        self.assertNotIn("speaker", loaded["profile"])

    def test_a_machine_the_firmware_does_not_name_cannot_be_found_and_is_refused(self):
        nameless = shared()
        nameless["hardware"]["product_name"] = "To be filled by O.E.M. <script>"
        with self.assertRaisesRegex(share.NotAPublicProfile, "firmware does not name"):
            share.public_payload(nameless)

    def test_firmware_strings_are_held_to_a_plain_character_set(self):
        self.assertEqual(share.hardware_text("  Dell   Inc. "), "Dell Inc.")
        self.assertEqual(share.hardware_text("HP Laptop 14 (2024) #A1"), "HP Laptop 14 (2024) #A1")
        self.assertEqual(share.hardware_text("a\nb\t c"), "a b c")            # white space is one space
        for hostile in ("<img src=x>", 'x" onload="y', "x" * 81, "", None, "’smart’", "../../etc", "-rf"):
            self.assertEqual(share.hardware_text(hostile), "", repr(hostile))

    def test_a_check_that_was_not_usable_is_not_a_credential(self):
        self.assertNotIn("verification", share.public_payload(shared(), {"usable": False, "verdict": "pass"})["public"])
        self.assertNotIn("verification", share.public_payload(shared(), None)["public"])
        kept = share.public_payload(shared(), VERIFIED)["public"]["verification"]
        self.assertEqual(kept, {"verdict": "pass", "target_error_before_db": 5.5,
                                "target_error_after_db": 1.8, "model_error_db": 0.8})

    def test_rebuilding_a_public_profile_gives_the_same_profile(self):
        # The registry rebuilds every upload with this same function, and both
        # sides name a profile by its content, so this has to be a fixed point.
        public = share.public_payload(shared(), VERIFIED)
        self.assertEqual(public["profile"]["quality"]["warning_count"], 2)
        claimed = public["public"]["verification"]
        again = share.public_payload(json.loads(json.dumps(public)), {
            "usable": True, "verdict": claimed["verdict"], "model_error_db": claimed["model_error_db"],
            "target_error_db": {"before": claimed["target_error_before_db"], "after": claimed["target_error_after_db"]}})
        self.assertEqual(again, public)
        self.assertEqual(share.profile_id(again), share.profile_id(public))
        self.assertEqual(share.objective_score(again), share.objective_score(public))
        hostile = json.loads(json.dumps(public))
        hostile["profile"]["quality"]["warning_count"] = -10 ** 9
        self.assertEqual(share.public_payload(hostile)["profile"]["quality"]["warning_count"], 0)

    def test_the_microphone_is_a_kind_never_a_name(self):
        kinds = {}
        for internal, calibrated in ((True, False), (False, False), (False, True)):
            profile = shared()
            profile["profile"]["microphone"].update(internal=internal, calibration_file="/x" if calibrated else None)
            kinds[(internal, calibrated)] = share.public_payload(profile)["public"]["microphone_kind"]
        self.assertEqual(kinds, {(True, False): "built-in microphone", (False, False): "external microphone",
                                 (False, True): "calibrated measuring microphone"})


class TransportTests(unittest.TestCase):
    def test_it_survives_an_issue_body_and_whatever_stands_around_it(self):
        public = share.public_payload(shared(), VERIFIED)
        text = share.encode_submission(public)
        self.assertLess(len(text), 20_000)
        body = "### Profile\n\n```text\n" + text.replace("\n", "\r\n") + "\n```\n\n### Anything else\n\nthanks!"
        self.assertEqual(share.decode_submission(body), public)

    def test_the_same_content_always_travels_as_the_same_text(self):
        public = share.public_payload(shared(), VERIFIED)
        self.assertEqual(share.encode_submission(public), share.encode_submission(copy.deepcopy(public)))
        self.assertEqual(share.profile_id(public), share.profile_id(copy.deepcopy(public)))
        self.assertRegex(share.profile_id(public), r"^2026-09-20-calibrated-[0-9a-f]{10}$")

    def test_what_is_not_a_profile_is_refused_with_a_reason(self):
        def packed(raw):
            return share.SUBMISSION_PREFIX + "\n" + base64.b64encode(gzip.compress(raw)).decode()
        for body, reason in (
            ("just a bug report", "no profile found"),
            (share.SUBMISSION_PREFIX + "\n!!!not base64!!!", "empty or too long"),
            (share.SUBMISSION_PREFIX + "\n" + base64.b64encode(b"not gzip at all").decode(), "cannot be unpacked"),
            (packed(b"[1, 2, 3]"), "not a document"),
            (packed(b"{not json"), "not JSON"),
            (packed(b"[" * 150_000), "not JSON"),
            (packed(b" " * 5_000_000), "unpacks to more"),                       # a few kB that inflate to 5 MB
            ("x" * 300_000, "too long"),
        ):
            with self.assertRaisesRegex(share.NotAPublicProfile, reason, msg=reason):
                share.decode_submission(body)


class FindingTests(unittest.TestCase):
    OURS = {"sys_vendor": "SLIMBOOK", "product_name": "Executive-14-UC2", "product_sku": "Executive-14-UC2",
            "board_name": "Executive-14-UC2"}

    def test_a_machine_s_profiles_live_under_its_vendor_and_product(self):
        self.assertEqual(share.index_path(self.OURS), "index/slimbook/executive-14-uc2.json")
        self.assertEqual(share.index_path({"sys_vendor": "Dell Inc.", "product_name": "XPS 13 9340"}),
                         "index/dell-inc/xps-13-9340.json")
        # A string that is not a firmware name never becomes part of a path.
        self.assertEqual(share.index_path({"sys_vendor": "../../etc", "product_name": "<x>"}),
                         "index/unknown/unknown.json")
        self.assertEqual(share.slug("A/B\\..//C"), "a-b-c")

    def test_how_close_a_profile_s_machine_is(self):
        tier = share.match_tier
        self.assertEqual(tier(dict(self.OURS), self.OURS), 3)
        self.assertEqual(tier(dict(self.OURS, product_sku="Other", board_name="Other"), self.OURS), 2)
        self.assertEqual(tier(dict(self.OURS, product_name="Executive-16", product_sku="x"), self.OURS), 1)
        self.assertEqual(tier(dict(self.OURS, sys_vendor="Dell Inc."), self.OURS), 0)
        self.assertEqual(tier({}, self.OURS), 0)
        self.assertEqual(tier("nonsense", None), 0)

    def test_the_score_ranks_a_checked_measuring_microphone_over_a_built_in_one(self):
        def scored(internal, calibrated, verification, votes=0):
            profile = shared()
            profile["profile"]["microphone"].update(internal=internal, calibration_file="/x" if calibrated else None)
            return share.objective_score(share.public_payload(profile, verification), votes)
        best, plain, built_in = scored(False, True, VERIFIED), scored(False, False, None), scored(True, False, None)
        self.assertGreater(best, plain)
        self.assertGreater(plain, built_in)
        self.assertLessEqual(best, 100)
        # Votes help, and cannot carry a built-in measurement past a checked, calibrated one.
        self.assertGreater(scored(True, False, None, votes=3), built_in)
        self.assertLess(scored(True, False, None, votes=1000), best)

    def test_an_index_row_is_enough_to_choose_from(self):
        public = share.public_payload(shared(), VERIFIED)
        row = share.index_entry(public, identifier=share.profile_id(public), path="profiles/x.json",
                                issue=7, submitted_by="someone", votes=2)
        for key in ("id", "path", "name", "created_at", "hardware", "microphone_kind", "verdict", "verification",
                    "filters", "error_before_db", "error_after_db", "score", "votes", "issue", "submitted_by"):
            self.assertIn(key, row)
        self.assertLess(len(json.dumps(row)), 1500)


class GraphTests(unittest.TestCase):
    def test_it_is_a_picture_and_nothing_else(self):
        public = share.public_payload(shared(), VERIFIED)
        svg = share.render_svg(public)
        self.assertTrue(svg.startswith("<svg "))
        self.assertEqual(svg.count("<polyline"), 3)
        for forbidden in ("<script", "href", "<image", "<foreignObject", "onload", "url(", "<style", "javascript:"):
            self.assertNotIn(forbidden, svg, forbidden)

    def test_a_name_is_escaped_even_though_it_was_already_checked(self):
        public = share.public_payload(shared(), VERIFIED)
        public["name"] = 'x"><script>alert(1)</script>&'
        svg = share.render_svg(public)
        self.assertNotIn("<script", svg)
        self.assertIn("&lt;script&gt;", svg)

    def test_a_profile_without_curves_still_draws_its_frame(self):
        public = share.public_payload(shared(), VERIFIED)
        public["profile"]["fit"].pop("predicted_response_db")
        public["profile"]["measurement"]["frequency_hz"] = "nonsense"
        self.assertIn("</svg>", share.render_svg(public))


if __name__ == "__main__":
    unittest.main()
