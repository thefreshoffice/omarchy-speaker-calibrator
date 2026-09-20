"""What a calibration looks like when it is shared with everyone.

Pure standard library, and no file or network access: the plugin uses it to
build what it uploads, and the public registry's own checks import this same
module from a pinned commit of the plugin, so both sides agree by construction
on what a public profile is, how it travels, how it is scored and how its
graph is drawn.

A public profile is an ordinary shared calibration file (the same format the
plugin already exports and imports) with everything left out that the other
side does not need: the per-repeat and per-channel recordings' curves, the
level search's attempts, anything that names a device by serial.  It carries
no free text at all.  The hardware is named by the firmware's own strings,
held to a plain character set; every other string is one of a closed set of
words.  There is therefore nothing in it to advertise with, and nothing that
says who made it.
"""

import base64
import gzip
import hashlib
import io
import json
import math
import re

REGISTRY_REPOSITORY = "thefreshoffice/omarchy-speaker-profiles"
REGISTRY_BRANCH = "main"
# One origin, fixed here: the registry's files as GitHub serves them raw.
REGISTRY_ORIGIN = f"https://raw.githubusercontent.com/{REGISTRY_REPOSITORY}/{REGISTRY_BRANCH}/"

SUBMISSION_PREFIX = "omarchy-speaker-profile:gz+b64:"
SUBMISSION_LINE = 76
# A full profile is some 80 kB of JSON and a public one some 50; both limits
# leave room and neither leaves room for anything else.
PUBLIC_LIMIT_BYTES = 200_000
SUBMISSION_LIMIT_CHARS = 60_000

HARDWARE_FIELDS = ("sys_vendor", "product_name", "product_version", "product_sku", "board_name")
HARDWARE_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()+/&,#:-]{0,79}")
DEVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,199}")
# The only speaker names that travel: a laptop's own.  PipeWire names those by
# where they sit on the board (a PCI address, or Asahi's model number), which
# says nothing the hardware record does not already say.  A USB or Bluetooth
# output is named after the device, serial number or address included.
PUBLIC_SPEAKER = re.compile(r"alsa_output\.pci-[A-Za-z0-9._:+-]{1,180}|audio_effect\.j[0-9]{1,4}-convolver")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
VERSION = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}")

# Kept from the measurement: what draws the graph and says how it went.
MEASUREMENT_KEPT = ("frequency_hz", "level_dbfs", "noise_floor_db", "quality", "microphone_array",
                    "measured_through", "sweep", "gate", "method", "rate_hz",
                    "microphone_channel", "microphone_channels")
# Dropped from the fit: bulky, and read by nothing on the loading side.
FIT_DROPPED = ("total_cut_limit_db", "boost_decisions")
PROFILE_KEPT = ("schema_version", "plugin_version", "created_at", "voicing", "loudness", "bass",
                "channel_trim", "quality", "safety")


class NotAPublicProfile(ValueError):
    """The document is not something the registry would publish."""


def slug(text, fallback="unknown"):
    """A path component made of a firmware string: lower case, digits, dashes."""
    made = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:60]
    return made or fallback


def hardware_text(value):
    """A firmware string held to a plain character set, or the empty string."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if HARDWARE_TEXT.fullmatch(text) else ""


def hardware_key(hardware):
    """(vendor, product) as path components: where this machine's profiles live."""
    hardware = hardware if isinstance(hardware, dict) else {}
    return slug(hardware_text(hardware.get("sys_vendor"))), slug(hardware_text(hardware.get("product_name")))


def index_path(hardware):
    vendor, product = hardware_key(hardware)
    return f"index/{vendor}/{product}.json"


def match_tier(theirs, ours):
    """How closely a profile's machine is this one: 3 exact SKU, 2 product, 1 vendor family, 0 none."""
    theirs = theirs if isinstance(theirs, dict) else {}
    ours = ours if isinstance(ours, dict) else {}

    def same(field):
        a, b = hardware_text(theirs.get(field)).lower(), hardware_text(ours.get(field)).lower()
        return bool(a) and a == b
    if not same("sys_vendor"):
        return 0
    if same("product_name"):
        if same("product_sku") or same("board_name"):
            return 3
        return 2
    return 1 if same("board_name") else 0


def _finite(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _numbers_only(value, depth=0):
    """A copy that keeps numbers, booleans, None and the structure around them; strings go."""
    if depth > 8:
        raise NotAPublicProfile("nested too deeply")
    if isinstance(value, dict):
        return {str(key)[:64]: _numbers_only(item, depth + 1) for key, item in list(value.items())[:256]
                if isinstance(key, str)}
    if isinstance(value, list):
        return [_numbers_only(item, depth + 1) for item in value[:1024]]
    if value is None or isinstance(value, bool) or _finite(value):
        return value
    if isinstance(value, str):
        # Words the plugin writes itself: a mode, a verdict, a filter type.
        return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._:/+-]{0,63}", value) else None
    return None


def public_payload(shared, verification=None):
    """The public form of a shared calibration file.

    ``shared`` is what the plugin exports; ``verification`` is the summary of
    a check of that same calibration, when there is one.  Everything is copied
    by name, so a field this function does not know about is not published.
    """
    if not isinstance(shared, dict) or not isinstance(shared.get("profile"), dict):
        raise NotAPublicProfile("no calibration inside")
    source = shared["profile"]
    theirs = shared.get("hardware") if isinstance(shared.get("hardware"), dict) else {}
    hardware = {field: hardware_text(theirs.get(field)) for field in HARDWARE_FIELDS}
    hardware["label"] = hardware_text(" ".join(part for part in (hardware["sys_vendor"], hardware["product_name"]) if part))
    speaker = theirs.get("speaker")
    hardware["speaker"] = speaker if isinstance(speaker, str) and DEVICE_NAME.fullmatch(speaker) \
        and PUBLIC_SPEAKER.fullmatch(speaker) else None
    if not hardware["sys_vendor"] or not hardware["product_name"]:
        raise NotAPublicProfile("the firmware does not name this machine, so nobody could find the profile")

    profile = {key: _numbers_only(source.get(key)) for key in PROFILE_KEPT if key in source}
    created = str(source.get("created_at") or "")[:10]
    if not DATE.fullmatch(created):
        raise NotAPublicProfile("the measurement date is not a date")
    profile["created_at"] = created            # the day is enough; the second is nobody's business
    version = str(source.get("plugin_version") or shared.get("plugin_version") or "")
    profile["plugin_version"] = version if VERSION.fullmatch(version) else "0.0.0"
    quality = source.get("quality") if isinstance(source.get("quality"), dict) else {}
    # The warnings themselves are sentences and stay at home; how many there
    # were travels.  A profile that is already public carries only the count,
    # and rebuilding it must give the same profile again: the registry
    # rebuilds every upload, and the two sides name a profile by its content.
    if isinstance(quality.get("warnings"), list):
        warning_count = len(quality["warnings"])
    else:
        counted = quality.get("warning_count")
        warning_count = int(counted) if _finite(counted) and 0 <= counted <= 64 else 0
    profile["quality"] = {
        "accepted": quality.get("accepted") is True,
        "verdict": quality.get("verdict") if quality.get("verdict") in ("pass", "warning") else "warning",
        "warning_count": min(64, warning_count),
        "metrics": _numbers_only(quality.get("metrics") or {}),
    }
    microphone = source.get("microphone") if isinstance(source.get("microphone"), dict) else {}
    profile["microphone"] = {
        "internal": microphone.get("internal") is True,
        "calibration_file": bool(microphone.get("calibration_file")),
        "channel": microphone.get("channel") if microphone.get("channel") == "all"
        or (_finite(microphone.get("channel")) and 0 <= microphone.get("channel") < 64) else 0,
    }
    fit = source.get("fit") if isinstance(source.get("fit"), dict) else {}
    profile["fit"] = {key: _numbers_only(value) for key, value in fit.items()
                      if isinstance(key, str) and key not in FIT_DROPPED}
    measurement = source.get("measurement") if isinstance(source.get("measurement"), dict) else {}
    profile["measurement"] = {key: _numbers_only(measurement[key]) for key in MEASUREMENT_KEPT if key in measurement}

    public = {"microphone_kind": microphone_kind(profile["microphone"])}
    if isinstance(verification, dict) and verification.get("usable") is not False:
        # As the plugin writes a check: the distance from the target before,
        # as planned, and as measured through the correction; the model error
        # as a small table.  A public profile carries it flattened, and reading
        # that flat form back has to give the same again.
        error = verification.get("target_error_db") if isinstance(verification.get("target_error_db"), dict) else {}
        after = error.get("measured") if _finite(error.get("measured")) else error.get("after")
        model = verification.get("model_error_db")
        model = model.get("rms") if isinstance(model, dict) else model
        checked = {
            "verdict": verification.get("verdict") if verification.get("verdict") in ("pass", "warning", "fail") else None,
            "target_error_before_db": error.get("before") if _finite(error.get("before")) else None,
            "target_error_after_db": after if _finite(after) else None,
            "model_error_db": model if _finite(model) else None,
        }
        if checked["verdict"]:
            public["verification"] = checked
    payload = {
        "format": shared.get("format"),
        "name": f"{hardware['label']} · {public['microphone_kind']} · {created}",
        "plugin_version": profile["plugin_version"],
        "hardware": hardware,
        "public": public,
        "profile": profile,
    }
    if len(json.dumps(payload, separators=(",", ":"))) > PUBLIC_LIMIT_BYTES:
        raise NotAPublicProfile("larger than any calibration this plugin writes")
    return payload


def microphone_kind(microphone):
    if microphone.get("internal"):
        return "built-in microphone"
    return "calibrated measuring microphone" if microphone.get("calibration_file") else "external microphone"


def profile_id(payload):
    """A name for this exact content: the day, the microphone and a digest."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    kind = {"built-in microphone": "builtin", "external microphone": "external",
            "calibrated measuring microphone": "calibrated"}[payload["public"]["microphone_kind"]]
    return f"{payload['profile']['created_at']}-{kind}-{hashlib.sha256(body).hexdigest()[:10]}"


def encode_submission(payload):
    """The payload as text that survives an issue body: gzip, base64, wrapped lines."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    packed = base64.b64encode(gzip.compress(raw, 9, mtime=0)).decode()
    lines = [packed[index:index + SUBMISSION_LINE] for index in range(0, len(packed), SUBMISSION_LINE)]
    text = SUBMISSION_PREFIX + "\n" + "\n".join(lines)
    if len(text) > SUBMISSION_LIMIT_CHARS:
        raise NotAPublicProfile("too large to submit")
    return text


def decode_submission(text):
    """The payload inside a submission, or NotAPublicProfile.  Bounded at every step."""
    text = str(text or "")
    if len(text) > 4 * SUBMISSION_LIMIT_CHARS:
        raise NotAPublicProfile("the submission is too long")
    start = text.find(SUBMISSION_PREFIX)
    if start < 0:
        raise NotAPublicProfile("no profile found in the submission")
    body = text[start + len(SUBMISSION_PREFIX):]
    packed = "".join(re.match(r"\s*([A-Za-z0-9+/=]*)", line).group(1)
                     for line in _until_break(body.splitlines()))
    if not packed or len(packed) > SUBMISSION_LIMIT_CHARS:
        raise NotAPublicProfile("the profile in the submission is empty or too long")
    try:
        compressed = base64.b64decode(packed, validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(PUBLIC_LIMIT_BYTES + 1)
    except (ValueError, OSError, EOFError) as error:
        raise NotAPublicProfile(f"the profile in the submission cannot be unpacked ({type(error).__name__})")
    if len(raw) > PUBLIC_LIMIT_BYTES:
        raise NotAPublicProfile("the profile unpacks to more than any calibration this plugin writes")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as error:
        raise NotAPublicProfile(f"the profile in the submission is not JSON ({type(error).__name__})")
    if not isinstance(payload, dict):
        raise NotAPublicProfile("the profile in the submission is not a document")
    return payload


def _until_break(lines):
    """The base64 lines that follow the prefix, up to the first line that is not base64."""
    started = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if started:
                return
            continue
        if not re.fullmatch(r"[A-Za-z0-9+/=]+", stripped):
            return
        started = True
        yield stripped


# A quiet laptop speaker reaches the loudest allowed sweep and still leaves the
# microphone far below full scale, which earns the measurement up to two
# warnings about level.  They are advice for the person measuring.  Whether the
# level hurt is measured too: a signal far above the noise in the midrange that
# repeats to within half a decibel was loud enough, and a stranger loses
# nothing by it.
SCORE_LOW_PEAK_DBFS = -9.0
SCORE_CLEAN_SNR_MID_DB = 40.0
SCORE_CLEAN_REPEATABILITY_DB = 0.5
SCORE_LEVEL_WARNINGS = 2

# What a score means in a word, because 67 reads like a poor grade and is a good profile.
SCORE_BANDS = ((80, "excellent"), (60, "good"), (40, "fair"), (0, "rough"))


# What a calibration has to show before it is rendered as a tuning for
# everyone with that machine: a score that rates good, and a check, because a
# tuning that ships to people who never measured anything needs proof that the
# filters did what the measurement said they would.
VENDOR_MINIMUM_SCORE = 60
VENDOR_CHECKS = ("pass", "warning")


def vendor_eligible(payload, score):
    """Whether this public profile may become its machine's vendor tuning, and if not, why."""
    verdict = ((payload.get("public") or {}).get("verification") or {}).get("verdict")
    if not (payload.get("hardware") or {}).get("speaker"):
        return False, "it does not name the speakers it was made for"
    if verdict not in VENDOR_CHECKS:
        return False, "it has not been checked"
    if not _finite(score) or score < VENDOR_MINIMUM_SCORE:
        return False, f"it scores below {VENDOR_MINIMUM_SCORE}"
    return True, ""


def score_band(score):
    score = score if _finite(score) else 0
    return next(word for floor, word in SCORE_BANDS if score >= floor)


def excused_warnings(quality):
    """How many of a measurement's warnings are about a level that provably did no harm."""
    metrics = quality.get("metrics") or {}
    peak, snr, repeat = (metrics.get("maximum_accepted_peak_dbfs"), metrics.get("snr_mid_db"),
                         metrics.get("worst_repeatability_db"))
    if not (_finite(peak) and _finite(snr) and _finite(repeat)):
        return 0
    if peak >= SCORE_LOW_PEAK_DBFS or snr < SCORE_CLEAN_SNR_MID_DB or repeat > SCORE_CLEAN_REPEATABILITY_DB:
        return 0
    return SCORE_LEVEL_WARNINGS


SCORE_MICROPHONE = {"calibrated measuring microphone": 40.0, "external microphone": 32.0,
                    "built-in microphone": 18.0}


def score_parts(payload, votes=0):
    """What a profile's score is made of, part by part, so that it can be shown and argued with.

    A measurement has to be able to score decently on its own merits: a clean,
    repeatable one that predicts a large improvement is a good calibration
    before anyone has checked it, and a check then adds what only a check can,
    proof.  The microphone still matters most, because a built-in one measures
    its own position rather than the listener's.
    """
    public = payload.get("public") or {}
    profile = payload.get("profile") or {}
    fit = profile.get("fit") or {}
    quality = profile.get("quality") or {}
    parts = {"microphone": SCORE_MICROPHONE.get(public.get("microphone_kind"), 0.0)}
    repeat = (quality.get("metrics") or {}).get("worst_repeatability_db")
    parts["repeatability"] = (15.0 if repeat <= 0.5 else 10.0 if repeat <= 1.0 else 5.0 if repeat <= 2.0 else 0.0) \
        if _finite(repeat) else 0.0
    before, after = fit.get("weighted_rmse_before_db"), fit.get("weighted_rmse_after_db")
    parts["predicted_improvement"] = 15.0 * min(1.0, max(0.0, (before - after) / before)) \
        if _finite(before) and _finite(after) and before > 0 else 0.0
    checked = public.get("verification") or {}
    parts["checked"] = {"pass": 20.0, "warning": 10.0}.get(checked.get("verdict"), 0.0)
    before, after = checked.get("target_error_before_db"), checked.get("target_error_after_db")
    parts["measured_improvement"] = 8.0 * min(1.0, max(0.0, (before - after) / before)) \
        if parts["checked"] and _finite(before) and _finite(after) and before > 0 else 0.0
    counted = max(0, int(quality.get("warning_count") or 0) - excused_warnings(quality))
    parts["warnings"] = -min(8.0, 2.0 * counted) or 0.0
    parts["votes"] = min(10.0, 2.0 * max(0, int(votes or 0)))
    return parts


def objective_score(payload, votes=0):
    """0 to 100: how much a stranger should trust this calibration, from the file alone plus votes."""
    return int(round(min(100.0, max(0.0, sum(score_parts(payload, votes).values())))))


def score_words(payload):
    """The score's two main facts in words, because a number alone reads as a verdict."""
    parts = score_parts(payload)
    quality = (payload.get("profile") or {}).get("quality") or {}
    measurement = ("clean measurement" if parts["repeatability"] >= 15.0 and parts["warnings"] >= -2.0
                   else "good measurement" if parts["repeatability"] >= 10.0 else "usable measurement")
    verdict = ((payload.get("public") or {}).get("verification") or {}).get("verdict")
    check = {"pass": "checked and passed", "warning": "checked, passed with warnings",
             "fail": "checked and did not pass"}.get(verdict, "not checked yet")
    return f"{measurement}, {check}"


def index_entry(payload, *, identifier, path, issue=None, submitted_by=None, votes=0):
    """One row of a machine's index: enough to choose from without downloading anything."""
    profile, public = payload["profile"], payload["public"]
    fit = profile.get("fit") or {}
    return {
        "id": identifier, "path": path, "name": payload["name"],
        "created_at": profile["created_at"], "plugin_version": profile["plugin_version"],
        "hardware": payload["hardware"],
        "microphone_kind": public["microphone_kind"],
        "verdict": profile["quality"]["verdict"],
        "verification": public.get("verification"),
        "filters": fit.get("filter_count"),
        "error_before_db": fit.get("weighted_rmse_before_db"),
        "error_after_db": fit.get("weighted_rmse_after_db"),
        "score": objective_score(payload, votes), "votes": int(votes or 0),
        "score_parts": {key: round(value, 1) for key, value in score_parts(payload, votes).items()},
        "issue": issue, "submitted_by": submitted_by,
    }


# ---- the graph ----------------------------------------------------------------
GRAPH_WIDTH, GRAPH_HEIGHT = 760, 360
GRAPH_LEFT, GRAPH_RIGHT, GRAPH_TOP, GRAPH_BOTTOM = 52, 18, 46, 40
GRAPH_LOW_HZ, GRAPH_HIGH_HZ = 50.0, 20000.0


def _escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def render_svg(payload):
    """The calibration as a picture: what was measured, the correction, and what comes out.

    Self-contained SVG with no scripts, no links and no external references;
    the only text in it is the machine's name, already held to a plain
    character set and escaped again here.
    """
    profile = payload.get("profile") or {}
    fit = profile.get("fit") or {}
    frequencies = [value for value in (fit.get("frequency_hz") or (profile.get("measurement") or {}).get("frequency_hz") or [])
                   if _finite(value)]
    curves = [
        ("measured", "#8a97a5", 1.6, "", fit.get("measured_smoothed_db")),
        ("correction", "#eb6834", 1.6, "5 4", fit.get("correction_response_db")),
        ("result", "#2a78d6", 2.4, "", fit.get("predicted_response_db")),
    ]
    curves = [(name, color, width, dash, [value for value in values if _finite(value)])
              for name, color, width, dash, values in curves
              if isinstance(values, list) and len(values) == len(frequencies) and len(frequencies) >= 8]
    plot_w = GRAPH_WIDTH - GRAPH_LEFT - GRAPH_RIGHT
    plot_h = GRAPH_HEIGHT - GRAPH_TOP - GRAPH_BOTTOM
    # Centred on the result's midrange, so differently loud profiles line up
    # and the bass roll-off of a small speaker does not drag everything upward.
    reference = 0.0
    for name, _, _, _, values in curves:
        if name == "result" and values:
            middle = sorted(db for hz, db in zip(frequencies, values) if 300.0 <= hz <= 5000.0) or sorted(values)
            reference = middle[len(middle) // 2]
    low_db, high_db = -24.0, 18.0

    def x_of(hz):
        span = math.log10(GRAPH_HIGH_HZ / GRAPH_LOW_HZ)
        return GRAPH_LEFT + plot_w * min(1.0, max(0.0, math.log10(max(hz, 1e-3) / GRAPH_LOW_HZ) / span))

    def y_of(db):
        return GRAPH_TOP + plot_h * (1.0 - (min(high_db, max(low_db, db)) - low_db) / (high_db - low_db))

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GRAPH_WIDTH} {GRAPH_HEIGHT}" '
             f'width="{GRAPH_WIDTH}" height="{GRAPH_HEIGHT}" font-family="system-ui,sans-serif" font-size="11">',
             f'<rect width="{GRAPH_WIDTH}" height="{GRAPH_HEIGHT}" rx="6" fill="#f7fafc" stroke="#c3cfda"/>',
             f'<text x="{GRAPH_LEFT}" y="22" font-size="14" font-weight="600" fill="#14212e">{_escape(payload.get("name", ""))}</text>']
    for db in range(int(low_db), int(high_db) + 1, 6):
        y = y_of(db)
        parts.append(f'<line x1="{GRAPH_LEFT}" x2="{GRAPH_LEFT + plot_w}" y1="{y:.1f}" y2="{y:.1f}" '
                     f'stroke="{"#9aa9b8" if db == 0 else "#dfe6ec"}" stroke-width="1"/>')
        parts.append(f'<text x="{GRAPH_LEFT - 6}" y="{y + 4:.1f}" text-anchor="end" fill="#66788a">{db:+d}</text>')
    for hz, label in ((50, "50"), (100, "100"), (200, "200"), (500, "500"), (1000, "1k"), (2000, "2k"),
                      (5000, "5k"), (10000, "10k"), (20000, "20k")):
        x = x_of(hz)
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{GRAPH_TOP}" y2="{GRAPH_TOP + plot_h}" stroke="#dfe6ec" stroke-width="1"/>')
        parts.append(f'<text x="{x:.1f}" y="{GRAPH_TOP + plot_h + 16}" text-anchor="middle" fill="#66788a">{label}</text>')
    for name, color, width, dash, values in curves:
        offset = 0.0 if name == "correction" else reference
        points = " ".join(f"{x_of(hz):.1f},{y_of(db - offset):.1f}" for hz, db in zip(frequencies, values)
                          if GRAPH_LOW_HZ <= hz <= GRAPH_HIGH_HZ)
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="{width}" '
                     f'stroke-linejoin="round"{f" stroke-dasharray={chr(34)}{dash}{chr(34)}" if dash else ""}/>')
    # The legend shares the bottom row with the caption, clear of the title.
    legend_x = GRAPH_LEFT + plot_w
    legend_y = GRAPH_HEIGHT - 12
    for name, color, width, dash, _ in reversed(curves):
        legend_x -= 7 * len(name) + 40
        parts.append(f'<line x1="{legend_x}" x2="{legend_x + 18}" y1="{legend_y}" y2="{legend_y}" stroke="{color}" stroke-width="{width}"'
                     f'{f" stroke-dasharray={chr(34)}{dash}{chr(34)}" if dash else ""}/>')
        parts.append(f'<text x="{legend_x + 23}" y="{legend_y + 4}" fill="#44566a">{name}</text>')
    parts.append(f'<text x="{GRAPH_LEFT}" y="{GRAPH_HEIGHT - 8}" fill="#66788a">dB against frequency in Hz · '
                 f'{_escape((payload.get("public") or {}).get("microphone_kind", ""))}</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"
