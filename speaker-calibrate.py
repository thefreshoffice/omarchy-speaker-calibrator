#!/usr/bin/python3
"""Guided, measurement-gated PipeWire speaker calibration for Omarchy."""

import argparse
import contextlib
import datetime as dt
import json
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

# Python caches the bytecode of the sibling modules in a __pycache__ directory
# next to them, which is inside the plugin directory. Omarchy's shell watches
# that directory and reloads the plugin on any change, killing the helper it had
# just started (issue #2). So never write bytecode: the sibling modules compile
# in a few milliseconds, and the system's own caches are still read. This must
# run before the first sibling import.
sys.dont_write_bytecode = True

# Reading and writing anything another process could have replaced first.
from calibration_io import (  # noqa: E402
    MAX_DESCRIPTION_BYTES,
    UnsafeFile,
    read_text_bounded,
    secure_directory,
    write_atomic,
)

# The measurement and fitting code pulls in NumPy and SciPy, a fifth of a
# second every time the process starts.  Reading the status, listing devices
# and flipping any switch need none of it, and those are the calls the panel
# makes constantly, so the import waits until something actually measures.
DSP_NAMES = (
    "LEVEL_SEARCH_ABORT_STATUSES", "SweepSpec", "analyse_capture",
    "analyse_level_probe", "build_measurement_signal",
    "combine_microphone_measurements", "level_after_clipping",
    "level_search_advice", "parse_mic_calibration", "read_pcm16_wave_channels",
    "search_measurement_level", "write_pcm16_wave",
)
OPTIMIZER_NAMES = (
    "apply_refinement", "optimize_peq", "refinement_residual", "verification_report",
)


# Measuring needs numpy and scipy.  Omarchy does not ship either, so on a
# fresh machine they are simply absent, and everything except measuring works
# without them: the panel, the switches, the status.  They are named here so
# the panel can offer to install them instead of letting a press of Calibrate
# end in a stack trace.
MEASUREMENT_PACKAGES = ("python-numpy", "python-scipy")
# The filter chain ends in an LV2 limiter.  Omarchy pacstraps this package from
# its own list, so it is normally present; the check is here so that removing
# it by hand gives a button rather than a dead end.
LIMITER_PACKAGE = "lsp-plugins-lv2"
LIMITER_PROBE = Path("/usr/lib/lv2/lsp-plugins.lv2/limiter_stereo.ttl")


def measurement_support():
    """Whether this machine can measure yet, and what it needs if not."""
    missing = []
    for module, package in zip(("numpy", "scipy"), MEASUREMENT_PACKAGES):
        try:
            __import__(module)
        except Exception:
            missing.append(package)
    if not LIMITER_PROBE.exists():
        missing.append(LIMITER_PACKAGE)
    return {
        "available": not missing,
        "missing": missing,
        "packages": list(MEASUREMENT_PACKAGES) + [LIMITER_PACKAGE],
        "command": "omarchy pkg add " + " ".join(missing or
                                                 list(MEASUREMENT_PACKAGES)),
    }


def install_measurement_support():
    """Install what measuring needs, in a terminal the user can watch."""
    support = measurement_support()
    if support["available"]:
        return {**support, "started": False,
                "message": "Measurement support is already installed."}
    started = run(
        ["omarchy", "launch", "floating", "terminal", "with", "presentation",
         support["command"]],
        check=False,
    ).returncode == 0
    if not started:
        raise SystemExit(
            "Could not open a terminal for the installation. Run this yourself:\n"
            f"  {support['command']}"
        )
    return {
        **support, "started": True,
        "message": ("Installing " + " and ".join(support["missing"])
                    + " from Omarchy's own packages in a terminal window. "
                    "When it finishes, press Calibrate."),
    }


def load_dsp():
    """Bind the measurement and fitting names; called only when they are used."""
    if "np" in globals():
        return
    support = measurement_support()
    if not support["available"]:
        raise SystemExit(
            "Measuring needs " + " and ".join(support["missing"])
            + ", which this machine does not have yet. The panel can install "
            "them for you, or run:\n  " + support["command"]
        )
    import numpy
    import calibration_dsp
    import calibration_optimizer
    globals()["np"] = numpy
    for name in DSP_NAMES:
        globals()[name] = getattr(calibration_dsp, name)
    for name in OPTIMIZER_NAMES:
        globals()[name] = getattr(calibration_optimizer, name)


# Kept as plain numbers so nothing has to be imported to read them; the tests
# check they still match what the sweep actually defaults to.
RATE = 48_000
EXTERNAL_SPEAKER_LEVEL_DBFS = -27.0
RECORD_LEAD_SECONDS = 1.0
INTERNAL_SPEAKER_LEVEL_DBFS = -12.0
# Before the long sweeps, a short two-sided sweep is played at a quiet level,
# the microphone peak is measured, and the level is moved toward the target so
# the real measurement neither clips nor sinks into the room noise.  Bounds and
# the first probe are relative to the sink's default sweep level.
LEVEL_PROBE_SPEC = dict(
    seconds=0.4, repeats=1, pre_silence=0.1, block_gap=0.05, response_tail=0.15
)
# The lead must exceed the probe's background window (0.3 s) by more than
# pw-record's start-up time, so the opening of every recording is room sound.
LEVEL_PROBE_LEAD_SECONDS = 0.8
LEVEL_PROBE_TAIL_SECONDS = 0.25
LEVEL_SEARCH_ATTEMPTS = 4
LEVEL_SEARCH_START_OFFSET_DB = -12.0
LEVEL_SEARCH_BOUNDS_DB = (-24.0, 6.0)

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "omarchy-speaker-calibrator"
HOST = CONFIG / "pipewire/omarchy-speaker-tuning.conf"
FRAGMENT = CONFIG / "pipewire/omarchy-speaker-tuning.conf.d/90-tuning.conf"
UNIT = CONFIG / "systemd/user/omarchy-speaker-tuning.service"
PROFILE = DATA / "active-profile.json"
PROPOSAL = DATA / "proposed-profile.json"
# The profile that the last install replaced, kept so the two can be swapped
# in and out for a listening comparison.  Graphs are regenerated from a
# profile's filters whenever it is activated, so only the profile is kept.
PREVIOUS_PROFILE = DATA / "previous-profile.json"
COMPARE_STATE = DATA / "compare-state.json"
# The verification plays through the corrected output, so its capture must
# never land on the calibration capture: refitting an already-corrected
# recording would correct the sound twice.
VERIFICATION = DATA / "verification.json"
# The last answer to "what is playing", so the panel can draw itself before
# the real query has finished.  Never read back by this program: it is written
# here and read by the panel, which then refreshes over the top of it.
STATUS_CACHE = DATA / "status-cache.json"
# One measurement kept per kind of microphone, so the two can be compared.
MICROPHONE_ARCHIVE = DATA / "microphone-archive"
# Where two microphones are compared: shape only, so a difference in
# sensitivity between them does not read as a difference in the speakers.
MICROPHONE_ALIGN_BAND_HZ = (250.0, 1000.0)
# A device names itself.  A USB microphone's product string is whatever its
# firmware says, so it is text from outside kept in a file and then drawn in
# the shell: long enough to wreck a row, and there is no reason to store more
# than fits one.
DEVICE_LABEL_LIMIT = 120
# The analysis grid is under two hundred points.  This is only a ceiling, so a
# file that has been rewritten cannot ask the panel to draw a million.
MICROPHONE_CURVE_LIMIT = 4096


# The panel puts a verdict into a section heading, and a heading is drawn by
# a shell component the plugin cannot pin to plain text.  The value comes from
# a file at a predictable name, so it is checked against the words this code
# actually produces rather than passed through.
VERDICTS = ("pass", "warning", "fail", "inconclusive")


def safe_verdict(value):
    """One of the words this produces, or None."""
    return value if value in VERDICTS else None


# PipeWire reports a description of "(null)" for a node that has none, and it
# should never be shown to anybody as if it were a name.
PLACEHOLDER_LABELS = {"(null)", "null", "none", "unknown"}


def short_label(value, limit=DEVICE_LABEL_LIMIT):
    """A device's own name, made safe to put in front of the shell.

    Length is not the only problem.  These names reach a button's label and
    the panel's heading, and both are drawn by shell components the plugin
    cannot pin to plain text, so Qt decides for itself whether the string is
    markup.  A name containing a tag would be rendered as one, and rich text
    fetches whatever an image tag points at.  The three characters that begin
    markup come out, along with anything non-printing.
    """
    if not isinstance(value, str):
        return None
    stripped = "".join(
        " " if character in "<>&" else character
        for character in value
        if character.isprintable() or character in " \t"
    )
    trimmed = " ".join(stripped.split())
    if not trimmed or trimmed.strip().lower() in PLACEHOLDER_LABELS:
        return None
    return trimmed[:limit]
VERIFICATION_SWEEPS = DATA / "verification-sweeps.wav"
VERIFICATION_RECORDING = DATA / "verification.wav"
SERVICE = "omarchy-speaker-tuning.service"
VIRTUAL_SINK = "omarchy_speaker_tuning"
# What the sink is called in Omarchy's sound menu, output switcher and OSD. ASCII
# only: pactl's JSON output turns any string with a non-ASCII character into
# "(null)", and every Omarchy audio script reads that output (issue #1). One name
# for nick, description and media.name, so the shell (which prefers the nick) and
# the switcher (which shows the description) never disagree.
SINK_LABEL = "Calibrated Speakers"

HOST_TEXT = """context.properties = { log.level = 0 }
context.spa-libs = {
  audio.convert.* = audioconvert/libspa-audioconvert
  support.* = support/libspa-support
}
context.modules = [
  { name = libpipewire-module-rt args = { } flags = [ ifexists nofail ] }
  { name = libpipewire-module-protocol-native }
  { name = libpipewire-module-client-node }
  { name = libpipewire-module-adapter }
]
"""

UNIT_TEXT = """[Unit]
Description=Omarchy speaker tuning filter-chain
After=pipewire.service wireplumber.service
Requires=pipewire.service
Wants=wireplumber.service
PartOf=pipewire.service

[Service]
Type=simple
ExecStart=/usr/bin/pipewire -c omarchy-speaker-tuning.conf
Restart=on-failure
RestartSec=2

[Install]
WantedBy=graphical-session.target
"""


def plugin_version():
    try:
        manifest = json.loads(
            # Root owns this when the plugin is installed system-wide, and it
            # sits next to this file either way.
            read_text_bounded(Path(__file__).resolve().parent / "manifest.json",
                              missing_ok=False, allow_root=True))
        return str(manifest.get("version", "unknown"))
    except (OSError, ValueError):
        return "unknown"


def run(args, *, check=True, capture=False):
    return subprocess.run(args, check=check, text=True,
                          capture_output=capture)


def pactl_json(kind):
    proc = run(["pactl", "-f", "json", "list", kind], capture=True)
    return json.loads(proc.stdout)


def harmonic_bass_status():
    """Deep bass is part of the graph: nothing to detect, nothing to install."""
    return {"available": True, "installed": True, "usable": True, "builtin": True,
            "package": None, "path": None, "missing_ports": []}


LOUDNESS_UNIT_TEXT = """[Unit]
Description=Omarchy speaker loudness compensation
After=omarchy-speaker-tuning.service
PartOf=omarchy-speaker-tuning.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 -s {tracker}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=graphical-session.target
"""


def loudness_unit_path():
    return CONFIG / "systemd/user" / LOUDNESS_SERVICE


def loudness_tracker_path():
    return Path(__file__).resolve().parent / LOUDNESS_TRACKER


def loudness_running():
    return run(
        ["systemctl", "--user", "is-active", LOUDNESS_SERVICE], check=False, capture=True
    ).stdout.strip() == "active"


def ensure_loudness_unit():
    """Register the tracker with systemd, doing nothing if it already is.

    Writing the unit, reloading, and enabling it together cost more than
    everything else a toggle does, so all three are skipped once they have
    been done: the unit only changes when the plugin moves, and the
    registration outlives any number of switches.
    """
    unit = loudness_unit_path()
    wanted = LOUDNESS_UNIT_TEXT.format(tracker=loudness_tracker_path())
    fresh = True
    try:
        fresh = read_text_bounded(unit) != wanted
    except OSError:
        pass
    if fresh:
        write_atomic(unit, wanted)
        run(["systemctl", "--user", "daemon-reload"], check=False)
    enabled = run(
        ["systemctl", "--user", "is-enabled", LOUDNESS_SERVICE], check=False, capture=True
    ).stdout.strip()
    if fresh or enabled != "enabled":
        run(["systemctl", "--user", "enable", LOUDNESS_SERVICE], check=False)
    return fresh


def start_loudness_tracker():
    """Ask for the tracker without waiting for it to come up.

    The compensation has already been switched on in the running filter by the
    time this is called, so nothing audible is waiting on systemd.
    """
    ensure_loudness_unit()
    run(["systemctl", "--user", "start", "--no-block", LOUDNESS_SERVICE], check=False)


def stop_loudness_tracker():
    """Stop the tracker but leave it registered.

    The unit stays enabled between switches: unregistering costs about as long
    as the rest of a toggle, and an enabled tracker that starts while the
    compensation is off reads the profile and stops again on its own.
    """
    run(["systemctl", "--user", "stop", "--no-block", LOUDNESS_SERVICE], check=False)


def forget_loudness_tracker():
    """Stop the tracker and unregister it, for when the plugin is switched off."""
    run(["systemctl", "--user", "disable", "--now", LOUDNESS_SERVICE], check=False)


def loudness_toggle():
    """Switch volume-following loudness compensation on or off."""
    profile = load_profile(PROFILE)
    if profile is None:
        raise SystemExit("Calibrate the speakers first; there is nothing to compensate.")
    wanted = "off" if profile.get("loudness_compensation") == "on" else "on"
    profile["loudness_compensation"] = wanted

    # The sound changes here, before anything slow is asked of systemd or the
    # disk.  Only
    # the compensator and the gain that pays its attenuation back move, so
    # this is a handful of milliseconds.
    fit = profile.get("fit") or {}
    controls = loudness_controls(
        sink_volume_db(listening_sink(profile)), wanted == "on",
        fit.get("input_gain_linear", 1.0))
    if apply_controls_live(controls):
        method = "live"
        # Keep the graph on disk in step, so a restart keeps the setting.
        write_atomic(FRAGMENT, filter_config(
            profile["speaker"]["name"], profile.get("fit") or {},
            deep_bass=profile.get("deep_bass") == "on",
            loudness_compensation=wanted == "on",
            sink_volume_db=sink_volume_db(listening_sink(profile)),
        ))
    else:
        method = activate_profile(profile)
    write_atomic(PROFILE, json.dumps(profile, indent=2) + "\n")

    if wanted == "on":
        start_loudness_tracker()
    else:
        stop_loudness_tracker()
    return {
        "loudness_compensation": wanted,
        "tracker": "starting" if wanted == "on" else "stopping",
        "method": method,
        "message": ("Loudness compensation on, following the volume"
                    if wanted == "on" else "Loudness compensation off"),
    }


def parse_sink_volume_db(text):
    """The loudest channel's volume in dB from what pactl prints.

    The reading looks like "front-left: 36044 /  55% / -15.58 dB", with the
    number and its unit as separate words, and one group per channel.  The
    loudest is the one to follow: it is what sets how loud the speakers are.
    """
    levels = [
        float(value)
        for value, unit in zip(text.split(), text.split()[1:])
        if unit.rstrip(",") == "dB" and _is_number(value)
    ]
    return max(levels) if levels else 0.0


def _is_number(value):
    try:
        float(value)
        return True
    except ValueError:
        return False


def sink_volume_db(name):
    """The output's current volume in dB, or 0 when it cannot be read."""
    result = run(["pactl", "get-sink-volume", name], check=False, capture=True)
    if result.returncode != 0:
        return 0.0
    return parse_sink_volume_db(result.stdout)


def is_physical_sink(name):
    """True for a real output device, never the calibrated sink in front of one."""
    return str(name).startswith("alsa_output.") and str(name) != VIRTUAL_SINK


def listening_sink(profile=None):
    """The sink whose volume says how loud the speakers actually are.

    Not this filter's own.  The volume keys deliberately resolve through a DSP
    sink to the device behind it, so that the keys move real loudness and the
    processing keeps seeing full-scale input; this filter's volume therefore
    never moves, and reading it reports full volume however quiet the room is.
    Following it would leave the contour flat at every setting, which is what
    it did.
    """
    if profile is None:
        profile = load_profile(PROFILE)
    name = ((profile or {}).get("speaker") or {}).get("name")
    return name or VIRTUAL_SINK


def physical_sinks():
    """Only real outputs are offered, so a calibration cannot measure itself."""
    return [item for item in pactl_json("sinks") if is_physical_sink(item.get("name", ""))]


def is_measurement_microphone(name):
    """True for a capture device that can describe a speaker.

    The speaker side already refuses anything that is not a real output; this
    is the same rule for the other end.  A Bluetooth headset offers a source,
    but its microphone runs over HFP or HSP: mono, eight to sixteen kilohertz,
    with automatic gain and noise suppression applied inside the headset. It
    cannot measure a loudspeaker, and a measurement taken through one would be
    fitted to the headset's processing rather than to the speakers.
    """
    return str(name).startswith("alsa_input.")


def microphones():
    return [item for item in pactl_json("sources")
            if not item.get("name", "").endswith(".monitor")
            and is_measurement_microphone(item.get("name", ""))]


def unusable_microphones():
    """Capture devices deliberately left out, so the panel can say why."""
    return [label(item) for item in pactl_json("sources")
            if not item.get("name", "").endswith(".monitor")
            and not is_measurement_microphone(item.get("name", ""))]


def channel_count(item):
    spec = item.get("sample_specification") or item.get("sample_spec") or ""
    found = re.search(r"(\d+)ch", str(spec))
    return int(found.group(1)) if found else 1


def devices_payload():
    def public(item, kind):
        name = item["name"]
        return {
            "name": name,
            # A device names itself, and some do it with ragged spacing.
            "description": short_label(label(item)) or label(item),
            "channels": channel_count(item),
            "kind": kind,
            "internal": name.startswith(("alsa_input.pci-", "alsa_output.pci-")),
        }
    return {
        "sinks": [public(item, "speaker") for item in physical_sinks()],
        "microphones": [public(item, "microphone") for item in microphones()],
    }


def label(item):
    return item.get("description") or item.get("properties", {}).get("device.description") or item["name"]


def select(items, title, predicate=None):
    choices = [item for item in items if predicate is None or predicate(item)]
    if not choices:
        raise SystemExit(f"No matching {title.lower()} found.")
    print(f"\n{title}")
    for index, item in enumerate(choices, 1):
        print(f"  {index}. {label(item)}\n     {item['name']}")
    while True:
        answer = input(f"Select 1-{len(choices)}: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]


# The graph always has the same nodes, in this order per channel, so that any
# profile can be applied to the running filter by updating its controls instead
# of restarting the PipeWire client, which would drop the sink and stop every
# player attached to it.  Unused sections are transparent: a peaking or shelf
# biquad at 0 dB is unity.
# The optional psychoacoustic bass add-on.  A small speaker cannot move enough
# air to make a low note at all; this plays that note's harmonics instead and
# the ear supplies the fundamental it never heard.  The recipe is bankstown's
# (James Calligeros, MIT), written in PipeWire's built-in nodes, so nothing
# has to be installed.
HARMONIC_AMOUNT = 1.45
HARMONIC_DRIVE = 1.75
HARMONIC_FLOOR_HZ = 20.0
HARMONIC_MAX_HZ = 250.0
HARMONIC_SCALE = math.pi / (0.5 + math.e)   # bankstown scales its tanh by this
NATURAL_BASE = math.e
INVERSE_BASE = 1.0 / math.e
# A new calibration has deep bass on; the switch turns it off.
DEEP_BASS_DEFAULT = "on"

# Volume-dependent loudness compensation.  The ear loses bass and, less so,
# treble as the level drops, which is why quiet music sounds thin.  The LSP
# compensator applies the equal-loudness contour for a given listening level;
# it is already a dependency, so nothing new has to be installed.
LOUDNESS_URI = "http://lsp-plug.in/plugins/lv2/loud_comp_stereo"
LOUDNESS_STANDARD = 4.0        # ISO 226:2023
LOUDNESS_MODE = 1.0            # IIR: minimum phase, so it adds no latency
LOUDNESS_APPROX = 2.0
# Its volume control is where the listening level goes, and it attenuates by
# that much as well as choosing the contour.  The attenuation has to be paid
# back somewhere downstream of the plugin: its own input gain looks like the
# obvious place and is not, because the plugin works out how much to
# compensate from the level it sees, so gain in front of it is read as the
# music being loud again and cancels the contour exactly.  Measured at 60 Hz
# against 1500 Hz, the contour is worth +25 dB of bass at -20 dB with unity
# input and -1 dB with the input gain that matches it.  The make-up therefore
# goes on the limiter's input gain, which is past the compensator.
#
# Full volume is the reference: at 0 dB nothing is compensated and nothing is
# paid back.  That is also what keeps this safe, because the make-up is
# exactly the attenuation the volume control already applied, so the level
# arriving at the limiter is the same as it would be at full volume however
# far down the contour goes.
#
# The floor is where the make-up stops growing, and it has to be a real limit
# rather than a backstop.  Every decibel of contour is a decibel of make-up
# handed to the limiter, and the contour's bass lift rides on top of that: at
# -36 dB the compensator was being given +36 dB of gain, and the limiter
# audibly caught the bass.  Past -30 the warmth barely moves anyway, +4.6 dB
# against +6.1 dB at -42, so the depth was buying artefacts rather than sound.
LOUDNESS_FLOOR_DB = -18.0
# How much deeper than the volume reading to take the listening level.  The
# reading understates how quiet it really is, because a laptop speaker at full
# scale is already well short of the level the contour is calibrated against,
# so the same setting earns more compensation than the number alone suggests.
#
# It is never more than the volume has actually come down.  That keeps full
# volume untouched, where there is no headroom for a boost at all, and keeps
# what the contour lifts inside the headroom the attenuation just freed: the
# lift in the band this speaker can play grows at about a seventh of the
# contour's depth, so it stays comfortably under the room the volume made.
LOUDNESS_EXTRA_DEPTH_DB = 12.0
# How much of the contour's attenuation the make-up pays back.  Not all of it:
# the contour also lifts the bass, and on music that is real energy, so paying
# the midband back in full made songs two to three decibels louder with the
# compensation on and pushed their bass transients into the limiter.  Paying
# back this share keeps the loudness roughly where it was and leaves the
# limiter the headroom the lift needs.
LOUDNESS_MAKEUP_SHARE = 0.85
# A new calibration follows the volume from the start; the switch under
# Advanced turns it off.  An existing profile keeps whatever it had.
LOUDNESS_COMPENSATION_DEFAULT = "on"
LOUDNESS_SERVICE = "omarchy-speaker-loudness.service"
LOUDNESS_TRACKER = "loudness-tracker.py"

PEAKING_SLOTS = 12
DEFAULT_HIGHPASS_HZ = 55.0
# A high-pass section is switched off by moving it below the audible band
# rather than by removing it, so the graph keeps its fixed shape and a profile
# with fewer stages can still be applied to the running filter.
PARKED_HIGHPASS_HZ = 10.0
SECTION_LABELS = {
    "hp": "bq_highpass",
    "ls": "bq_lowshelf",
    "bs": "bq_lowshelf",
    "p": "bq_peaking",
    "hs": "bq_highshelf",
    # A plain gain, used to centre the stereo image when that is measurable.
    "bal": "linear",
}


def graph_sections():
    """Section names per channel, in signal order.

    ``ls`` and ``hs`` are the optimizer's shelves; ``bs`` is the full-bass
    option's shelf, kept separate so both can be present at once.
    """
    return (
        ["hp1", "hp2", "ls", "bs"]
        + [f"p{slot}" for slot in range(1, PEAKING_SLOTS + 1)]
        + ["hs", "bal"]
    )


def fit_sections(fit_payload):
    """(peaking list, low shelf, high shelf, bass shelf) from a fit payload."""
    filters = fit_payload.get("filters")
    if filters is None:
        peaking = list(zip(
            fit_payload.get("centers_hz", []),
            fit_payload.get("q", []),
            fit_payload.get("gains_db", []),
        ))
        low_shelf = high_shelf = None
    else:
        peaking = [
            (item["frequency_hz"], item["q"], item["gain_db"])
            for item in filters if item.get("type", "peaking") == "peaking"
        ]
        low_shelf = next((item for item in filters if item.get("type") == "lowshelf"), None)
        high_shelf = next((item for item in filters if item.get("type") == "highshelf"), None)
    # Profiles from 0.10.0 stored the full-bass shelf as "low_shelf".
    bass = fit_payload.get("bass_shelf")
    if bass is None and low_shelf is None:
        bass = fit_payload.get("low_shelf")
    return peaking, low_shelf, high_shelf, bass


def _shelf_controls(controls, name, shelf, default_hz):
    shelf = shelf or {}
    controls[f"{name}:Freq"] = float(shelf.get("frequency_hz", default_hz))
    controls[f"{name}:Q"] = float(shelf.get("q", 0.707))
    controls[f"{name}:Gain"] = float(shelf.get("gain_db", 0.0))


def highpass_settings(fit_payload):
    """(corner, q, stages) of the protective high-pass, with old defaults."""
    highpass = fit_payload.get("highpass") or {}
    corner = float(highpass.get(
        "frequency_hz", fit_payload.get("highpass_hz", DEFAULT_HIGHPASS_HZ)
    ))
    q = float(highpass.get("q", 0.707))
    stages = int(highpass.get("stages", fit_payload.get("highpass_stages", 2)))
    return corner, q, max(1, min(2, stages))


def loudness_level_db(sink_volume_db):
    """The listening level the contour is chosen for, in dB below full volume.

    How loud full volume is in the room is not known here, so it is taken as
    the reference and left alone; how far the volume has come down from it is
    known, and is taken as understating the case by up to
    ``LOUDNESS_EXTRA_DEPTH_DB``.
    """
    volume = float(sink_volume_db)
    extra = min(LOUDNESS_EXTRA_DEPTH_DB, max(0.0, -volume))
    return float(np_free_clip(volume - extra, LOUDNESS_FLOOR_DB, 0.0))


def loudness_controls(sink_volume_db, enabled, input_gain_linear=1.0):
    """Compensator settings for the level the speakers are playing at.

    ``volume`` carries the listening level, which selects the contour and
    attenuates by the same amount.  Its own ``input`` gain stays at unity:
    the plugin reads the level in front of it to decide how much to
    compensate, so paying the attenuation back there would tell it the music
    is loud and leave a contour worth nothing.  It is paid back on the
    limiter instead, which the signal reaches after the compensator.
    """
    level = loudness_level_db(sink_volume_db) if enabled else 0.0
    makeup = 10.0 ** (-level * LOUDNESS_MAKEUP_SHARE / 20.0)
    return {
        "loudcomp:enabled": 1.0 if enabled else 0.0,
        "loudcomp:volume": level,
        "loudcomp:input": 1.0,
        "loudcomp:std": LOUDNESS_STANDARD,
        "loudcomp:mode": LOUDNESS_MODE,
        "loudcomp:approx": LOUDNESS_APPROX,
        "limiter:g_in": round(float(input_gain_linear) * makeup, 6),
    }


def np_free_clip(value, low, high):
    """A clamp that costs no import; the fast paths must stay light."""
    return max(low, min(high, value))


def harmonic_settings(corner_hz):
    """Where the deep bass works, tuned to where this speaker gives up."""
    limit = float(min(HARMONIC_MAX_HZ, max(10.0, corner_hz)))
    # Harmonics are made from what lies below the knee and kept above it,
    # which is the only place the speaker can reproduce them.
    return {"floor_hz": HARMONIC_FLOOR_HZ, "ceil_hz": limit, "final_hp_hz": limit,
            "drive": HARMONIC_DRIVE, "amount": HARMONIC_AMOUNT, "scale": HARMONIC_SCALE}


def harmonic_controls(corner_hz, deep_bass):
    """The live controls of the deep-bass path, for both channels."""
    h = harmonic_settings(corner_hz)
    mult = 2.0 * h["scale"] * h["amount"] if deep_bass else 0.0
    controls = {}
    for side in ("l", "r"):
        controls[f"hb_lp_{side}:Freq"] = h["ceil_hz"]
        controls[f"hb_fh_{side}:Freq"] = h["final_hp_hz"]
        controls[f"hb_fl_{side}:Freq"] = round(3.0 * h["ceil_hz"], 3)
        controls[f"hb_out_{side}:Mult"] = round(mult, 6)
        # The stage before this one is a sigmoid, so the output carries half the
        # multiplier as a constant. Subtract it here rather than leaving it to
        # the high-pass: that filter starts from zero whenever the graph starts
        # or resumes, and the constant then steps through it as a loud thump.
        controls[f"hb_out_{side}:Add"] = round(-0.5 * mult, 6)
    return controls


def harmonic_nodes(side, settings, mult):
    """The deep-bass path for one channel: node lines, links, and its ends.

    bankstown's recipe in PipeWire built-ins: the band below the knee, then
    k·amt·tanh(drive·x) written as 2·k·amt·s with s the logistic 1/(1+e^-2u)
    (exp with base 1/e, +1, log, exp with base 1/e; the constant this adds is
    removed by the path's own high-pass, and no multiplier is negative because
    PipeWire's linear node drops the sign), then the harmonics' own band,
    summed with the untouched signal.  ``mult`` is 2·k·amt with deep bass on
    and 0 with it off: the one control that switches it live.
    """
    h = settings
    specs = (
        ("hb_in", "copy", None),
        ("hb_cl", "clamp", '"Min" = -10 "Max" = 10'),
        ("hb_hp", "bq_highpass", f'"Freq" = {_plain(h["floor_hz"])} "Q" = 0.707'),
        ("hb_lp", "bq_lowpass", f'"Freq" = {_plain(h["ceil_hz"])} "Q" = 0.707'),
        ("hb_g", "linear", f'"Mult" = {_plain(2.0 * h["drive"])} "Add" = 0'),
        ("hb_e1", "exp", f'"Base" = {INVERSE_BASE:.9f}'),
        ("hb_p1", "linear", '"Mult" = 1 "Add" = 1'),
        ("hb_ln", "log", f'"Base" = {NATURAL_BASE:.9f} "M1" = 1 "M2" = 1'),
        ("hb_e2", "exp", f'"Base" = {INVERSE_BASE:.9f}'),
        ("hb_out", "linear", f'"Mult" = {float(mult):.6f} "Add" = {-0.5 * float(mult):.6f}'),
        ("hb_fh", "bq_highpass", f'"Freq" = {_plain(h["final_hp_hz"])} "Q" = 0.707'),
        ("hb_fl", "bq_lowpass", f'"Freq" = {_plain(3.0 * h["ceil_hz"])} "Q" = 0.707'),
        ("hb_mix", "mixer", '"Gain 1" = 1 "Gain 2" = 1'),
    )
    nodes = [f'{{ type = builtin name = {name + "_" + side:<8} label = {label:<12}'
             + (f' control = {{ {control} }}' if control else "") + " }"
             for name, label, control in specs]
    path = [name for name, _, _ in specs[:-1]]
    links = [f'{{ output = "{before}_{side}:Out" input = "{after}_{side}:In" }}'
             for before, after in zip(path, path[1:])]
    links.append(f'{{ output = "hb_cl_{side}:Out" input = "hb_mix_{side}:In 1" }}')
    links.append(f'{{ output = "hb_fl_{side}:Out" input = "hb_mix_{side}:In 2" }}')
    return nodes, links, f"hb_in_{side}:In", f"hb_mix_{side}:Out"


def graph_controls(fit_payload, *, deep_bass=False,
                   loudness_compensation=False, sink_volume_db=0.0):
    """Every control of the fixed-shape graph, for both channels, in order."""
    peaking, low_shelf, high_shelf, bass = fit_sections(fit_payload)
    if len(peaking) > PEAKING_SLOTS:
        raise ValueError(
            f"The graph has {PEAKING_SLOTS} parametric slots; this fit needs {len(peaking)}."
        )
    corner, highpass_q, stages = highpass_settings(fit_payload)
    controls = {}
    for side in ("l", "r"):
        for index in (1, 2):
            controls[f"hp{index}_{side}:Freq"] = (
                corner if index <= stages else PARKED_HIGHPASS_HZ
            )
            controls[f"hp{index}_{side}:Q"] = highpass_q
        _shelf_controls(controls, f"ls_{side}", low_shelf, 100.0)
        _shelf_controls(controls, f"bs_{side}", bass, 100.0)
        for slot in range(1, PEAKING_SLOTS + 1):
            if slot <= len(peaking):
                frequency, q, gain = peaking[slot - 1]
            else:
                frequency, q, gain = 1000.0, 1.0, 0.0
            controls[f"p{slot}_{side}:Freq"] = float(frequency)
            controls[f"p{slot}_{side}:Q"] = float(q)
            controls[f"p{slot}_{side}:Gain"] = float(gain)
        _shelf_controls(controls, f"hs_{side}", high_shelf, 8000.0)
        gain_db = float((fit_payload.get("channel_trim") or {}).get(f"{'left' if side == 'l' else 'right'}_db", 0.0))
        controls[f"bal_{side}:Mult"] = round(10.0 ** (gain_db / 20.0), 6)
        controls[f"bal_{side}:Add"] = 0.0
    controls.update(harmonic_controls(corner, deep_bass))
    # Last, because the compensation's make-up rides on the limiter's input
    # gain and needs the calibrated value to build on.
    controls.update(loudness_controls(
        sink_volume_db, loudness_compensation, fit_payload["input_gain_linear"]))
    return controls


def _number(value):
    return f"{float(value):.4f}".rstrip("0").rstrip(".") or "0"


def filter_config(sink, fit_payload, *, deep_bass=False,
                  loudness_compensation=False, sink_volume_db=0.0):
    controls = graph_controls(
        fit_payload, deep_bass=deep_bass,
        loudness_compensation=loudness_compensation, sink_volume_db=sink_volume_db,
    )
    harmonic = harmonic_settings(highpass_settings(fit_payload)[0])
    nodes, links, inputs, outputs = [], [], [], []
    for side, port in (("l", "l"), ("r", "r")):
        chain = []
        for section in graph_sections():
            name = f"{section}_{side}"
            kind = section.rstrip("0123456789")
            label = SECTION_LABELS[kind]
            if kind == "bal":
                settings = (
                    f'"Mult" = {_number(controls[f"{name}:Mult"])} '
                    f'"Add" = {_number(controls[f"{name}:Add"])}'
                )
            else:
                settings = f'"Freq" = {_number(controls[f"{name}:Freq"])} "Q" = {_number(controls[f"{name}:Q"])}'
                if kind != "hp":
                    settings += f' "Gain" = {_number(controls[f"{name}:Gain"])}'
            nodes.append(
                f'{{ type = builtin name = {name} label = {label} control = {{ {settings} }} }}'
            )
            chain.append(name)
        # The compensator lifts what the ear loses at low level; the
        # high-pass after it still throws away whatever the speaker cannot
        # play, so the lift only survives where it can be heard.
        links.append(f'{{ output = "loudcomp:out_{port}" input = "{chain[0]}:In" }}')
        # Deep bass has to see the low notes before anything takes them away,
        # so its path comes first and feeds the compensator.
        hb_nodes, hb_links, hb_input, hb_output = harmonic_nodes(
            side, harmonic, controls[f"hb_out_{side}:Mult"])
        nodes.extend(hb_nodes)
        links.extend(hb_links)
        links.append(f'{{ output = "{hb_output}" input = "loudcomp:in_{port}" }}')
        inputs.append(f'"{hb_input}"')
        for before, after in zip(chain, chain[1:]):
            links.append(f'{{ output = "{before}:Out" input = "{after}:In" }}')
        links.append(f'{{ output = "{chain[-1]}:Out" input = "limiter:in_{port}" }}')
        outputs.append(f'"limiter:out_{port}"')
    loudness = " ".join(
        f'"{name.split(":", 1)[1]}" = {_number(value)}'
        for name, value in controls.items() if name.startswith("loudcomp:")
    )
    nodes.insert(0, f'''{{ type = lv2 name = loudcomp
      plugin = "{LOUDNESS_URI}"
      control = {{ {loudness} }}
    }}''')
    input_gain = controls["limiter:g_in"]
    nodes.append(f'''{{ type = lv2 name = limiter
      plugin = "http://lsp-plug.in/plugins/lv2/limiter_stereo"
      control = {{ "alr" = 0 "boost" = 0 "g_in" = {input_gain:.6f} "th" = 0.891 }}
    }}''')
    indented_nodes = "\n          ".join(nodes)
    indented_links = "\n          ".join(links)
    return f'''# Generated by Omarchy Speaker Calibrator. Boosts require reliable broad deficits and matching headroom.
context.modules = [
  {{ name = libpipewire-module-filter-chain args = {{
    node.description = "{SINK_LABEL}"
    media.name = "{SINK_LABEL}"
    filter.graph = {{
      nodes = [
          {indented_nodes}
      ]
      links = [
          {indented_links}
      ]
      inputs = [ {' '.join(inputs)} ]
      outputs = [ {' '.join(outputs)} ]
    }}
    audio.channels = 2
    audio.position = [ FL FR ]
    capture.props = {{
      node.name = "{VIRTUAL_SINK}"
      media.class = Audio/Sink
      node.nick = "{SINK_LABEL}"
      node.description = "{SINK_LABEL}"
    }}
    playback.props = {{
      node.name = "{VIRTUAL_SINK}_output"
      node.passive = true
      target.object = "{sink}"
      node.dont-move = true
      node.dont-fallback = true
      node.linger = true
    }}
  }} }}
]
'''


def backup(path):
    if path.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = DATA / "backups" / f"{path.name}.{stamp}"
        secure_directory(destination.parent)
        shutil.copy2(path, destination)


def sink_description(name):
    """A readable name for any output, including ones this never calibrates.

    Bluetooth and HDMI outputs are not candidates for a calibration, so they
    are absent from the device list, but the panel still has to be able to say
    where the sound went.
    """
    if not name:
        return None
    for item in pactl_json("sinks"):
        if item.get("name") == name:
            return item.get("description") or name
    return name


def use_calibrated_output():
    """Send the sound back through the calibration.

    Plugging in headphones or connecting a speaker moves the default output,
    which takes the filter out of the path.  Nothing is broken when that
    happens and nothing is changed behind the user's back; this is the way
    back, asked for.
    """
    if not any(item.get("name") == VIRTUAL_SINK for item in pactl_json("sinks")):
        raise SystemExit("The calibrated output is not running; install a calibration first.")
    move_apps(VIRTUAL_SINK)
    return {**status_payload(), "message": "Sound is going through the calibration again."}


def move_apps(target):
    run(["pactl", "set-default-sink", target])
    for stream in pactl_json("sink-inputs"):
        props = stream.get("properties", {})
        if props.get("application.name") and props.get("application.name") != "EasyEffects":
            run(["pactl", "move-sink-input", str(stream["index"]), target], check=False)


def compare_state():
    try:
        state = json.loads(read_text_bounded(COMPARE_STATE) or '')
    except (OSError, ValueError):
        state = {}
    if state.get("active") not in ("current", "previous"):
        state["active"] = "current"
    state["bypass"] = bool(state.get("bypass", False))
    return state


def write_compare_state(state):
    write_atomic(COMPARE_STATE, json.dumps(state) + "\n")


def transparent_controls(level_match_db=0.0):
    """Controls that make the running graph pass audio through unchanged.

    The high-pass sections drop to 10 Hz, every gain goes to 0 dB and the bass
    add-on is bypassed, so what is left is the speaker as it was; only the
    -1 dBFS ceiling remains.  ``level_match_db`` turns that plain sound down
    to the loudness the correction plays at, so switching between them is a
    question about tone rather than about volume.
    """
    controls = graph_controls(
        {"filters": [], "input_gain_linear": 10.0 ** (float(level_match_db) / 20.0)},
        deep_bass=False,
    )
    for name in list(controls):
        if name.startswith("hp") and name.endswith(":Freq"):
            controls[name] = 10.0
    return controls


def profile_summary(path):
    """A short label for a saved profile, or None when there is none."""
    try:
        profile = json.loads(read_text_bounded(path) or '')
    except (OSError, ValueError):
        return None
    fit = profile.get("fit") or {}
    created = str(profile.get("created_at", ""))[:16].replace("T", " ")
    # The microphone comes right after the date: with one profile per kind of
    # microphone, that is the word that tells the two apart.
    kind = microphone_kind(profile)
    return {
        "created_at": profile.get("created_at"),
        "microphone_kind": kind,
        "label": f"{created} · {MICROPHONE_KIND_LABELS[kind]} · "
                 f"{fit.get('filter_count', 0)} filters · "
                 f"{VOICING_LABELS.get(profile.get('voicing'), 'neutral')} · "
                 f"{BASS_LABELS.get(profile.get('bass'), 'normal bass')} · "
                 f"{LOUDNESS_LABELS.get(profile.get('loudness'), 'protected')}",
        "voicing": profile.get("voicing", "neutral"),
        "bass": profile.get("bass", "normal"),
        "loudness": profile.get("loudness", "protected"),
        "plugin_version": profile.get("plugin_version"),
    }


def keep_previous_profile():
    """Set aside the profile being listened to, for comparison with the next one."""
    if PROFILE.exists() and compare_state()["active"] == "current":
        shutil.copy2(PROFILE, PREVIOUS_PROFILE)
    # When the previous profile was playing, it stays "previous": that is the
    # sound the new install will be compared against.
    write_compare_state({"active": "current", "bypass": False})


def service_active():
    return run(
        ["systemctl", "--user", "is-active", SERVICE], check=False, capture=True
    ).stdout.strip() == "active"


def tuning_node_id():
    """PipeWire id of the running tuning sink, or None."""
    # pactl carries PipeWire's object id and answers in a third of the time a
    # full dump takes; the dump remains as the fallback.
    for item in pactl_json("sinks"):
        if item.get("name") == VIRTUAL_SINK:
            try:
                return int((item.get("properties") or {})["object.id"])
            except (KeyError, TypeError, ValueError):
                break
    try:
        nodes = json.loads(run(["pw-dump"], capture=True).stdout)
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None
    for node in nodes:
        if node.get("type") != "PipeWire:Interface:Node":
            continue
        props = node.get("info", {}).get("props", {})
        if props.get("node.name") == VIRTUAL_SINK and props.get("media.class") == "Audio/Sink":
            return node.get("id")
    return None


def live_controls(node_id):
    """The running filter graph's controls by name."""
    try:
        nodes = json.loads(run(["pw-dump", str(node_id)], capture=True).stdout)
    except (subprocess.CalledProcessError, ValueError, OSError):
        return {}
    for node in nodes:
        for entry in node.get("info", {}).get("params", {}).get("Props", []):
            items = entry.get("params")
            if items and "limiter:g_in" in items:
                return dict(zip(items[0::2], items[1::2]))
    return {}


def write_controls(node_id, controls):
    """Set controls on a running filter without reading them back.

    The verification below is worth its cost when a profile is installed and
    wrong values would be heard for as long as it plays.  It is not worth it
    on every turn of the volume knob, so the tracker uses this directly.
    """
    payload = " ".join(f'"{name}" {float(value):.6f}' for name, value in controls.items())
    return run(
        ["pw-cli", "set-param", str(node_id), "Props", f"{{ params = [ {payload} ] }}"],
        check=False, capture=True,
    ).returncode == 0


def apply_controls_live(controls):
    """Update the running filter in place; False when it must be restarted."""
    node_id = tuning_node_id()
    if node_id is None:
        return False
    if not write_controls(node_id, controls):
        return False
    # One read-back does both jobs: a control the running graph does not have
    # (an older shape, which only a restart can replace) shows up as missing,
    # and a value that did not land shows up as different.
    after = live_controls(node_id)
    for name, value in controls.items():
        try:
            readback = float(after[name])
        except (KeyError, TypeError, ValueError):
            return False
        if abs(readback - float(value)) > 1e-3 * max(1.0, abs(float(value))):
            return False
    return True


def activate_profile(profile):
    """Make the running tuning play this profile, live when possible.

    The graph file is regenerated from the profile's filters either way, so
    the on-disk graph always has the fixed shape and the next activation can
    be live even if this one had to restart an older graph.
    """
    fit = profile.get("fit") or {}
    deep_bass = profile.get("deep_bass") == "on"
    compensation = profile.get("loudness_compensation") == "on"
    volume = sink_volume_db(listening_sink(profile))
    controls = graph_controls(
        fit, deep_bass=deep_bass,
        loudness_compensation=compensation, sink_volume_db=volume,
    )
    graph = filter_config(
        profile["speaker"]["name"], fit, deep_bass=deep_bass,
        loudness_compensation=compensation, sink_volume_db=volume,
    )
    state = compare_state()
    if state["bypass"]:
        state["bypass"] = False
        write_compare_state(state)
    # The sound changes first.  The running graph never reads the file on
    # disk, so nothing audible should wait for its fsyncs; and a live update
    # touches no unit file, so systemd has nothing to reload (restart_tuning
    # reloads before it restarts).
    live = service_active() and apply_controls_live(controls)
    # The graph file is written only when it changed, so that a restart, now
    # or later, loads what is playing.
    try:
        unchanged = read_text_bounded(FRAGMENT) == graph
    except (OSError, UnsafeFile):
        unchanged = False
    if not unchanged:
        write_atomic(FRAGMENT, graph)
    if live:
        return "live"
    restart_tuning()
    return "restart"


@contextlib.contextmanager
def added_sound_silenced():
    """Mute everything that invents sound while the filters are measured.

    The bass add-on makes harmonics that were never in the signal, and the
    loudness compensator bends the response by an amount that depends on the
    volume knob.  Neither is something a linear model of the filters
    predicts, so a check that left them running would measure them as error
    exactly where they act, and feeding that back would have the optimizer
    cut away what they had just added.  Only their own controls are touched,
    so the filters under test are untouched.
    """
    node_id = tuning_node_id() if service_active() else None
    if node_id is None:
        yield False
        return
    live = live_controls(node_id)
    running, quiet = {}, {}

    harmonics = {name: value for name, value in live.items()
                 if name.startswith("hb_out_")}
    if any(name.endswith(":Mult") and float(value) > 0.0 for name, value in harmonics.items()):
        running.update(harmonics)
        quiet.update({name: 0.0 for name in harmonics})

    compensation = {
        name: value for name, value in live.items() if name.startswith("loudcomp:")
    }
    if compensation and compensation.get("loudcomp:enabled", 0.0) >= 0.5:
        # The make-up that pays back the contour's attenuation lives on the
        # limiter, so silencing the contour has to take that back off too, or
        # the speaker would be measured louder than it plays.
        makeup = 10.0 ** (-float(compensation.get("loudcomp:volume", 0.0)) / 20.0)
        gain = float(live.get("limiter:g_in", 1.0))
        running.update(compensation)
        running["limiter:g_in"] = gain
        quiet.update({**compensation, "loudcomp:enabled": 0.0,
                      "loudcomp:volume": 0.0, "loudcomp:input": 1.0,
                      "limiter:g_in": round(gain / makeup, 6)})

    if not quiet or not apply_controls_live(quiet):
        yield False
        return
    try:
        yield True
    finally:
        apply_controls_live(running)


@contextlib.contextmanager
def correction_silenced():
    """Flatten the running filter while the raw speakers are measured.

    Sweeps are played straight at the physical device, which already bypasses
    the filter chain, but that relies on the stream landing where it was
    aimed.  Flattening the filter as well makes a calibration measure the bare
    speakers even if the stream is routed through the correction, so a profile
    can never be fitted to a sound that was already corrected.  The previous
    control values are restored afterwards, without restarting the tuning.
    """
    node_id = tuning_node_id() if service_active() else None
    if node_id is None or compare_state()["bypass"]:
        yield False
        return
    # Flattened, not level-matched: a calibration measures the speaker as it
    # is, and an attenuation here would be measured as the speaker being quiet.
    wanted = set(transparent_controls())
    live = live_controls(node_id)
    saved = {name: value for name, value in live.items() if name in wanted}
    if len(saved) != len(wanted) or not apply_controls_live(transparent_controls()):
        # The filter could not be flattened, so leave it alone and rely on the
        # stream target; the measurement records that this happened.
        yield False
        return
    try:
        yield True
    finally:
        apply_controls_live(saved)


def playing_profile():
    """The profile that should be audible now, per the compare state."""
    state = compare_state()
    target = PREVIOUS_PROFILE if state["active"] == "previous" else PROFILE
    return load_profile(target) or load_profile(PROFILE)


def bypass_level_match(profile):
    """Attenuation for the plain speakers, matched to the correction's loudness."""
    fit = (profile or {}).get("fit") or {}
    if "loudness_loss_db" not in fit:
        # A profile from before the loudness of a correction was measured.
        return 0.0
    # From the light module rather than the optimizer: the panel asks for this
    # on every status read, and the optimizer would load numpy to answer it.
    from calibration_levels import bypass_level_match_db
    return bypass_level_match_db(fit)


def bypass_toggle():
    """Switch the running graph between unity and the playing profile, live."""
    state = compare_state()
    profile = playing_profile()
    if profile is None:
        raise SystemExit("No calibration is installed, so there is nothing to switch off.")
    match = bypass_level_match(profile)
    if not state["bypass"]:
        method = "live"
        flat = transparent_controls(level_match_db=match)
        if not (service_active() and apply_controls_live(flat)):
            # The running graph has an older shape, so its controls cannot be
            # zeroed by name.  Activating the profile regenerates the graph in
            # the current shape (restarting once); then unity can be applied.
            method = activate_profile(profile)
            if not apply_controls_live(transparent_controls(level_match_db=match)):
                raise SystemExit(
                    "Could not switch the calibration off; the tuning was restarted with it on."
                )
        state = compare_state()
        state["bypass"] = True
        write_compare_state(state)
    else:
        # activate_profile clears the bypass flag itself.
        method = activate_profile(profile)
    payload = compare_payload()
    payload["method"] = method
    payload["level_match_db"] = match
    return payload


def restart_tuning():
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", SERVICE])
    run(["systemctl", "--user", "restart", SERVICE])
    for _ in range(30):
        if any(item.get("name") == VIRTUAL_SINK for item in pactl_json("sinks")):
            move_apps(VIRTUAL_SINK)
            return
        time.sleep(0.25)
    raise SystemExit("The tuning sink did not appear; inspect the user service status.")


def install_profile(profile, graph):
    if not LIMITER_PROBE.exists():
        raise SystemExit(
            f"The filter chain needs {LIMITER_PACKAGE}, which this machine does "
            "not have. The panel can install it for you, or run:\n"
            f"  omarchy pkg add {LIMITER_PACKAGE}"
        )
    secure_directory(DATA, repair_contents=True)
    keep_previous_profile()
    for path in (HOST, FRAGMENT, UNIT):
        backup(path)
        secure_directory(path.parent)
    write_atomic(HOST, HOST_TEXT)
    write_atomic(FRAGMENT, graph)
    write_atomic(UNIT, UNIT_TEXT)
    write_atomic(PROFILE, json.dumps(profile, indent=2) + "\n")
    return activate_profile(profile)


def load_profile(path):
    try:
        profile = json.loads(read_text_bounded(path) or '')
    except (OSError, ValueError):
        return None
    if not profile.get("fit") or not profile.get("speaker", {}).get("name"):
        return None
    return profile


def compare_payload():
    state = compare_state()
    return {
        "available": load_profile(PREVIOUS_PROFILE) is not None
        and load_profile(PROFILE) is not None,
        "active": state["active"],
        "bypass": state["bypass"],
        "level_match_db": bypass_level_match(playing_profile()),
        "current": profile_summary(PROFILE),
        "previous": profile_summary(PREVIOUS_PROFILE),
    }


def compare_toggle():
    """Play the other of the two most recent profiles, live when possible."""
    state = compare_state()
    state["active"] = "previous" if state["active"] == "current" else "current"
    target = load_profile(PREVIOUS_PROFILE if state["active"] == "previous" else PROFILE)
    if target is None:
        raise SystemExit("No previous profile to compare with; install a second profile first.")
    state["bypass"] = False
    write_compare_state(state)
    payload = compare_payload()
    payload["method"] = activate_profile(target)
    payload["bypass"] = False
    return payload


def analyze_recording(
    recording, channel, schedule, measurement_spec, *, internal_mic, calibration
):
    load_dsp()
    captures = read_pcm16_wave_channels(recording, measurement_spec.rate)
    recorded_channels = captures.shape[1]
    if channel == "all":
        if not internal_mic:
            raise ValueError("All-channel mode is reserved for built-in microphone arrays.")
        input_channels = list(range(recorded_channels))
        measurements = [
            analyse_capture(
                captures[:, input_channel],
                schedule,
                measurement_spec,
                record_lead_seconds=RECORD_LEAD_SECONDS,
                internal_mic=True,
                calibration=None,
            )
            for input_channel in input_channels
        ]
        measurement = combine_microphone_measurements(measurements, input_channels)
    else:
        if channel < 0 or channel >= recorded_channels:
            raise ValueError(
                f"Microphone has {recorded_channels} channel(s), not channel {channel + 1}."
            )
        measurement = analyse_capture(
            captures[:, channel],
            schedule,
            measurement_spec,
            record_lead_seconds=RECORD_LEAD_SECONDS,
            internal_mic=internal_mic,
            calibration=calibration,
        )
        for curve in measurement.get("validation_curves", []):
            curve["input_channel"] = channel
    measurement["microphone_channels"] = recorded_channels
    measurement["microphone_channel"] = channel
    return measurement


def default_sweep_level(sink_name):
    """Built-in speakers get a louder sweep than external outputs."""
    if sink_name.startswith("alsa_output.pci-"):
        return INTERNAL_SPEAKER_LEVEL_DBFS
    return EXTERNAL_SPEAKER_LEVEL_DBFS


def die_with_parent():
    """Have the kernel end this child when the helper dies without cleaning up.

    Runs in the child between fork and exec. The panel ends a cancelled run with
    SIGTERM and, two seconds later, SIGKILL; nothing in this process runs after
    the latter, so the recorder's parent-death signal is the last line.
    """
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def stop_recorder(recorder):
    """Stop pw-record and make sure it is gone; a stray one keeps the microphone."""
    if recorder.poll() is None:
        recorder.send_signal(signal.SIGINT)
    try:
        recorder.wait(timeout=5)
    except subprocess.TimeoutExpired:
        recorder.kill()
        recorder.wait(timeout=5)


def record_while_playing(
    sink_name, mic_name, channels, program, recording, lead_seconds, tail_seconds
):
    """Record the microphone while a program plays on the selected sink."""
    recorder = subprocess.Popen([
        "pw-record", f"--target={mic_name}", f"--rate={RATE}",
        f"--channels={channels}", "--format=s16", str(recording)],
        preexec_fn=die_with_parent)
    try:
        time.sleep(lead_seconds)
        run(["pw-play", f"--target={sink_name}", str(program)])
        time.sleep(tail_seconds)
    finally:
        stop_recorder(recorder)


def playing_applications():
    """Names of applications with an unpaused playback stream right now."""
    names = []
    try:
        streams = pactl_json("sink-inputs")
    except (subprocess.CalledProcessError, ValueError, OSError):
        return names
    for stream in streams:
        if stream.get("corked") or stream.get("mute"):
            continue
        properties = stream.get("properties", {})
        name = properties.get("application.name") or properties.get("media.name")
        if name and name not in ("pw-play", "pw-record", "(null)") and name not in names:
            names.append(name)
    return names


def find_measurement_level(sink_name, mic_name, channel, channels, level_sink=None):
    """Probe the speaker/microphone pair and choose the sweep level."""
    load_dsp()
    default_level = default_sweep_level(level_sink or sink_name)
    probe_program = DATA / "level-probe.wav"
    probe_recording = DATA / "level-probe-recording.wav"

    def run_probe(level_dbfs):
        spec = SweepSpec(level_dbfs=level_dbfs, **LEVEL_PROBE_SPEC)
        program, _ = build_measurement_signal(spec)
        write_pcm16_wave(probe_program, program, RATE)
        record_while_playing(
            sink_name, mic_name, channels, probe_program, probe_recording,
            LEVEL_PROBE_LEAD_SECONDS, LEVEL_PROBE_TAIL_SECONDS,
        )
        captures = read_pcm16_wave_channels(probe_recording, RATE)
        if channel != "all":
            if channel < 0 or channel >= captures.shape[1]:
                raise ValueError(
                    f"Microphone has {captures.shape[1]} channel(s), not channel {channel + 1}."
                )
            captures = captures[:, [channel]]
        return analyse_level_probe(captures, RATE)

    search = search_measurement_level(
        run_probe,
        start_level_dbfs=default_level + LEVEL_SEARCH_START_OFFSET_DB,
        bounds=(
            default_level + LEVEL_SEARCH_BOUNDS_DB[0],
            default_level + LEVEL_SEARCH_BOUNDS_DB[1],
        ),
        attempts=LEVEL_SEARCH_ATTEMPTS,
    )
    search["default_level_dbfs"] = default_level
    if search["status"] in LEVEL_SEARCH_ABORT_STATUSES:
        warnings, guidance = level_search_advice(search)
        playing = playing_applications()
        if playing:
            guidance.append("Currently playing audio: " + ", ".join(playing) + ".")
        raise ValueError(" ".join(warnings + guidance))
    return search


def capture_measurement(
    sink_name, mic_name, channel, mic_cal_file=None, *,
    level_sink=None, sweeps=None, recording=None,
):
    load_dsp()
    """Find a safe level, play the Phase 1 program, capture it, and analyze it.

    ``level_sink`` names the output the level default should be taken from,
    which differs from the played sink when measuring through the calibrated
    sink that fronts it.
    """
    channels = next(
        (channel_count(item) for item in microphones() if item["name"] == mic_name), 1
    )
    secure_directory(DATA, repair_contents=True)
    level_search = find_measurement_level(
        sink_name, mic_name, channel, channels, level_sink=level_sink
    )
    sweeps = sweeps or DATA / "calibration-sweeps.wav"
    recording = recording or DATA / "measurement.wav"
    calibration = parse_mic_calibration(mic_cal_file)
    level = level_search["selected_level_dbfs"]
    # The probe is a fraction of the length of the real sweep, so a resonance
    # has less time to ring up during it.  If the full capture clips anyway,
    # one quieter retry costs less than a rejected measurement.
    for attempt in range(2):
        measurement_spec = SweepSpec(level_dbfs=level)
        program, schedule = build_measurement_signal(measurement_spec)
        write_pcm16_wave(sweeps, program, RATE)
        record_while_playing(
            sink_name, mic_name, channels, sweeps, recording, RECORD_LEAD_SECONDS, 0.75
        )
        measurement = analyze_recording(
            recording,
            channel,
            schedule,
            measurement_spec,
            internal_mic=mic_name.startswith("alsa_input.pci-"),
            calibration=calibration,
        )
        metrics = measurement["quality"]["metrics"]
        if metrics["clipped_samples"] == 0 or attempt == 1:
            break
        retry = level_after_clipping(
            level, metrics["maximum_accepted_peak_dbfs"], level_search["level_bounds_dbfs"][0]
        )
        if retry >= level:
            break
        level_search = dict(level_search, selected_level_dbfs=round(retry, 2),
                            status="retried-after-clipping", confirmed=False)
        level = retry
    attach_level_search(measurement, level_search)
    return measurement


def attach_level_search(measurement, level_search):
    """Record the level search and surface an unsettled search as guidance."""
    if not level_search:
        return
    measurement["level_search"] = level_search
    warnings, guidance = level_search_advice(level_search)
    quality = measurement["quality"]
    quality["warnings"] = list(dict.fromkeys(quality["warnings"] + warnings))
    quality["guidance"] = list(dict.fromkeys(quality["guidance"] + guidance))
    if quality["accepted"]:
        quality["verdict"] = "warning" if quality["warnings"] else "pass"


VOICING_LABELS = {"neutral": "flat", "warm": "warm"}
LOUDNESS_LABELS = {"protected": "protected", "balanced": "balanced", "matched": "matched"}
BASS_LABELS = {"normal": "normal bass", "full": "full bass"}
MICROPHONE_KIND_LABELS = {"internal": "internal mic", "external": "external mic"}


def profile_from_measurement(
    sink, mic, channel, voicing, measurement, loudness="protected", bass="normal",
    channel_trim="off",
):
    load_dsp()
    quality = measurement["quality"]
    internal_mic = mic["name"].startswith("alsa_input.pci-")
    fit_payload = None
    if quality["accepted"]:
        fit_payload = optimize_peq(
            measurement,
            voicing,
            internal_mic=internal_mic,
            loudness=loudness,
            bass=bass,
            channel_trim=channel_trim,
        )
    profile = {
        "schema_version": 5,
        "plugin_version": plugin_version(),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "speaker": {"name": sink["name"], "description": label(sink)},
        "microphone": {
            "name": mic["name"],
            "description": label(mic),
            "channel": channel,
            "internal": internal_mic,
            "calibration_file": measurement["microphone_calibration"]["path"]
                if measurement["microphone_calibration"] else None,
        },
        "voicing": voicing,
        "loudness": loudness,
        "bass": bass,
        "channel_trim": channel_trim,
        # Carried across refits so switching voicing does not lose them.
        "deep_bass": (load_profile(PROFILE) or {}).get("deep_bass", DEEP_BASS_DEFAULT),
        "loudness_compensation":
            (load_profile(PROFILE) or {}).get("loudness_compensation", LOUDNESS_COMPENSATION_DEFAULT),
        "safety": {
            "eq_max_db": fit_payload["maximum_allowed_boost_db"] if fit_payload else 0,
            "eq_min_db": fit_payload["cut_limit_db"] if fit_payload else 0,
            "per_filter_min_db": fit_payload["per_filter_cut_limit_db"] if fit_payload else 0,
            "highpass_hz": fit_payload["highpass_hz"] if fit_payload else DEFAULT_HIGHPASS_HZ,
            "highpass_stages": fit_payload["highpass_stages"] if fit_payload else 2,
            "limiter_ceiling_dbfs": -1,
            "input_trim_db": -fit_payload["headroom_db"] if fit_payload else -1,
            "makeup_gain_db": fit_payload["makeup_db"] if fit_payload else 0,
            "boost_policy": "Cuts are preferred; any boost must be broad, reliable, and improve a held-out repeat.",
            "loudness_policy": "Make-up gain pays back part of the loudness the cuts removed, never more than 6 dB; the -1 dBFS limiter absorbs the peaks.",
            "bass_policy": "Full bass is a +3 dB low shelf at the measured knee, paid for by input trim like any boost.",
        },
        "measurement": measurement,
        "quality": quality,
        "fit": fit_payload,
    }
    write_atomic(PROPOSAL, json.dumps(profile, indent=2) + "\n")
    archive_measurement(profile)
    return profile


def microphone_kind(profile):
    """Which microphone made this: the built-in array, or a measuring one."""
    return "internal" if (profile.get("microphone") or {}).get("internal") else "external"


def archive_measurement(profile):
    """Keep this measurement so the two microphones can be put side by side.

    Only the curve is kept, not the recording: a few kilobytes rather than
    megabytes, and the comparison is about shape.  Each kind of microphone has
    one slot, so measuring again with the same kind replaces it.
    """
    import numpy as np

    measurement = profile.get("measurement") or {}
    frequencies = measurement.get("frequency_hz") or []
    channels = measurement.get("channels") or []
    if len(frequencies) < 2 or not channels:
        return None
    responses = np.asarray(
        [channel["response_db"] for channel in channels], dtype=float
    )
    # The median across speakers, so one bad channel cannot define the curve.
    combined = np.median(responses, axis=0)
    grid = np.asarray(frequencies, dtype=float)
    band = ((grid >= MICROPHONE_ALIGN_BAND_HZ[0])
            & (grid <= MICROPHONE_ALIGN_BAND_HZ[1]))
    if np.any(band):
        combined = combined - float(np.median(combined[band]))
    uncertainty = np.mean(
        np.asarray([channel.get("uncertainty_db") or [] for channel in channels],
                   dtype=float),
        axis=0,
    ) if all(channel.get("uncertainty_db") for channel in channels) else None

    mic = profile.get("microphone") or {}
    record = {
        "kind": microphone_kind(profile),
        "created_at": profile.get("created_at"),
        "microphone": short_label(mic.get("description")),
        "calibration_file": bool(mic.get("calibration_file")),
        "frequency_hz": [round(float(value), 2) for value in grid],
        "response_db": [round(float(value), 3) for value in combined],
        "uncertainty_db": ([round(float(value), 3) for value in uncertainty]
                           if uncertainty is not None else None),
        "verdict": safe_verdict((profile.get("quality") or {}).get("verdict")),
        "speaker": short_label((profile.get("speaker") or {}).get("description")),
    }
    try:
        write_atomic(MICROPHONE_ARCHIVE / f"{record['kind']}.json",
                     json.dumps(record) + "\n")
    except OSError:
        return None
    return record


def backfill_archive():
    """Fill an empty slot from a profile that already exists.

    The archive was added after the fact, so somebody who measured before it
    existed would otherwise have nothing to compare until they measured twice
    more.  Only slots that are missing are written, so a real measurement is
    never replaced by an older profile.
    """
    for path in (PROFILE, PREVIOUS_PROFILE):
        profile = load_profile(path)
        if not profile:
            continue
        kind = microphone_kind(profile)
        if (MICROPHONE_ARCHIVE / f"{kind}.json").exists():
            continue
        archive_measurement(profile)


def valid_microphone_record(record):
    """A stored curve, or None if it is not the shape this wrote.

    The file is the plugin's own, but it sits at a predictable name under the
    data directory, so what comes back is checked rather than trusted: two
    equal-length arrays of finite numbers, no more points than the analysis
    grid could ever hold, and labels cut to a row.
    """
    if not isinstance(record, dict):
        return None
    frequencies = record.get("frequency_hz")
    response = record.get("response_db")
    if not isinstance(frequencies, list) or not isinstance(response, list):
        return None
    if not 2 <= len(frequencies) <= MICROPHONE_CURVE_LIMIT:
        return None
    if len(response) != len(frequencies):
        return None
    for series in (frequencies, response):
        for value in series:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None
            if value != value or value in (float("inf"), float("-inf")):
                return None
    record["microphone"] = short_label(record.get("microphone"))
    record["speaker"] = short_label(record.get("speaker"))
    record["verdict"] = safe_verdict(record.get("verdict"))
    return record


def microphone_comparison():
    """Both archived measurements, and where they disagree.

    A built-in microphone sits inside the case, inches from one driver and
    behind whatever the lid is made of; a measuring microphone sits where the
    listener does.  Where the two disagree, the built-in one is describing its
    own position rather than the sound arriving at the ear.
    """
    backfill_archive()
    records = {}
    for kind in ("internal", "external"):
        try:
            raw = read_text_bounded(MICROPHONE_ARCHIVE / f"{kind}.json")
        except OSError:
            raw = None
        if raw:
            try:
                records[kind] = valid_microphone_record(json.loads(raw))
            except ValueError:
                pass
    records = {kind: record for kind, record in records.items() if record}
    payload = {
        "internal": records.get("internal"),
        "external": records.get("external"),
        "bands": [],
        "available": len(records) == 2,
    }
    if not payload["available"]:
        return payload

    import numpy as np

    grid = np.asarray(records["external"]["frequency_hz"], dtype=float)
    external = np.asarray(records["external"]["response_db"], dtype=float)
    internal = np.interp(
        grid,
        np.asarray(records["internal"]["frequency_hz"], dtype=float),
        np.asarray(records["internal"]["response_db"], dtype=float),
    )
    difference = internal - external
    for label, low, high in (("bass", 80.0, 250.0), ("midrange", 250.0, 2000.0),
                             ("presence", 2000.0, 6000.0), ("treble", 6000.0, 16000.0)):
        window = (grid >= low) & (grid < high)
        if not np.any(window):
            continue
        payload["bands"].append({
            "band": label,
            "low_hz": low,
            "high_hz": high,
            "difference_db": round(float(np.mean(difference[window])), 2),
        })
    worst = max(payload["bands"], key=lambda entry: abs(entry["difference_db"]),
                default=None)
    payload["worst"] = worst
    payload["rms_difference_db"] = round(float(np.sqrt(np.mean(difference ** 2))), 2)
    return payload


def build_profile(
    sink, mic, channel, voicing, mic_cal_file=None, loudness="protected", bass="normal",
    channel_trim="off",
):
    if not is_physical_sink(sink["name"]):
        raise SystemExit(
            "A calibration must measure a real speaker output, never the calibrated "
            "one, or the correction would be fitted on top of itself."
        )
    with correction_silenced() as silenced:
        measurement = capture_measurement(
            sink["name"], mic["name"], channel, mic_cal_file
        )
    measurement["measured_through"] = {
        "sink": sink["name"],
        "corrected": False,
        "correction_silenced_during_measurement": bool(silenced),
    }
    return profile_from_measurement(
        sink, mic, channel, voicing, measurement, loudness, bass, channel_trim
    )


def reanalyze_saved_capture(
    voicing=None, channel_override=None, loudness=None, bass=None, channel_trim=None
):
    """Re-run current analysis and optimization on the last capture, without sound."""
    load_dsp()
    recording = DATA / "measurement.wav"
    if not PROPOSAL.exists() or not recording.exists():
        raise SystemExit("No saved capture and proposal are available to reanalyze.")
    previous = json.loads(read_text_bounded(PROPOSAL) or '')
    sink = previous["speaker"]
    mic = previous["microphone"]
    channel = parse_channel_selection(
        channel_override if channel_override is not None else mic.get("channel", 0)
    )
    # The saved capture was recorded at whatever level the search chose, so
    # the level must come from the saved measurement, not from the sink type.
    previous_measurement = previous.get("measurement", {})
    if (previous_measurement.get("measured_through") or {}).get("corrected"):
        raise SystemExit(
            "The saved capture was recorded through the correction, so it cannot be "
            "refitted. Calibrate again."
        )
    saved_level = previous_measurement.get("sweep", {}).get("level_dbfs")
    measurement_spec = SweepSpec(
        level_dbfs=float(saved_level) if saved_level is not None
        else default_sweep_level(sink["name"])
    )
    _, schedule = build_measurement_signal(measurement_spec)
    calibration_path = mic.get("calibration_file")
    calibration = parse_mic_calibration(calibration_path) if calibration_path else None
    measurement = analyze_recording(
        recording,
        channel,
        schedule,
        measurement_spec,
        internal_mic=mic["name"].startswith("alsa_input.pci-"),
        calibration=calibration,
    )
    attach_level_search(measurement, previous_measurement.get("level_search"))
    # A refit re-reads the raw capture, so anything learned from a check has to
    # be put back or changing the voicing would silently undo it.
    refinement = previous_measurement.get("refinement")
    if refinement and refinement.get("residual_db"):
        measurement = apply_refinement(
            measurement,
            np.interp(
                np.log(np.asarray(measurement["frequency_hz"], dtype=float)),
                np.log(np.asarray(previous_measurement["frequency_hz"], dtype=float)),
                np.asarray(refinement["residual_db"], dtype=float),
            ),
        )
        measurement["refinement"] = refinement
    return profile_from_measurement(
        sink, mic, channel, voicing or previous.get("voicing", "neutral"), measurement,
        loudness or previous.get("loudness", "protected"),
        bass or previous.get("bass", "normal"),
        channel_trim or previous.get("channel_trim", "off"),
    )


def parse_channel_selection(value):
    if isinstance(value, str) and value.lower() == "all":
        return "all"
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise SystemExit("Microphone channel must be a zero-based number or 'all'.") from error


def calibrate_noninteractive(
    sink_name, mic_name, channel, voicing, mic_cal_file=None, loudness="protected",
    bass="normal", channel_trim="off",
):
    sink = next((item for item in physical_sinks() if item["name"] == sink_name), None)
    mic = next((item for item in microphones() if item["name"] == mic_name), None)
    if sink is None or mic is None:
        raise SystemExit("Selected audio device is no longer available.")
    channel = parse_channel_selection(channel)
    channels = channel_count(mic)
    internal_mic = mic["name"].startswith("alsa_input.pci-")
    if channel == "all" and not internal_mic:
        raise SystemExit("All-channel mode is available only for built-in microphone arrays.")
    if channel != "all" and (channel < 0 or channel >= channels):
        raise SystemExit(f"Microphone channel must be between 1 and {channels}.")
    try:
        return build_profile(
            sink, mic, channel, voicing, mic_cal_file, loudness, bass, channel_trim
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error


def install_proposal():
    if not PROPOSAL.exists():
        raise SystemExit("No measured proposal is available.")
    profile = json.loads(read_text_bounded(PROPOSAL) or '')
    return install_now(profile)


def install_now(profile):
    if not profile.get("quality", {}).get("accepted") or not profile.get("fit"):
        raise SystemExit("This measurement failed its quality checks and cannot be installed.")
    profile["activation"] = install_profile(
        profile,
        filter_config(
            profile["speaker"]["name"], profile["fit"],
            deep_bass=profile.get("deep_bass") == "on",
            loudness_compensation=profile.get("loudness_compensation") == "on",
            sink_volume_db=sink_volume_db(listening_sink(profile)),
        ),
    )
    profile["installed"] = True
    # The tracker follows the profile that is now playing: started when it
    # asks for compensation, stopped when it does not.
    if profile.get("loudness_compensation") == "on":
        start_loudness_tracker()
    else:
        stop_loudness_tracker()
    return profile


def install_if_accepted(profile):
    """Install a fresh profile when it passed, otherwise return it unchanged."""
    if profile.get("quality", {}).get("accepted") and profile.get("fit"):
        return install_now(profile)
    profile["installed"] = False
    return profile


def load_verification():
    """The stored verification, marked stale when the profile has moved on."""
    try:
        report = json.loads(read_text_bounded(VERIFICATION) or '')
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict):
        return None
    profile = load_profile(PROFILE)
    created = profile.get("created_at") if profile else None
    report["stale"] = report.get("profile_created_at") != created
    # The panel puts this one in a section heading, which the shell draws.
    report["verdict"] = safe_verdict(report.get("verdict"))
    return report


def deep_bass_toggle():
    """Switch deep bass on or off, live, without refitting."""
    status = harmonic_bass_status()
    profile = load_profile(PROFILE)
    if profile is None:
        raise SystemExit("Calibrate the speakers first; there is nothing to add bass to.")
    wanted = "off" if profile.get("deep_bass") == "on" else "on"
    profile["deep_bass"] = wanted
    method = activate_profile(profile)
    write_atomic(PROFILE, json.dumps(profile, indent=2) + "\n")
    return {**status, "started": False, "deep_bass": wanted, "method": method,
            "message": ("Deep bass on" if wanted == "on" else "Deep bass off")}


# The fields of a fit that the bass and loudness switches decide.  A fit made
# by this version carries them for every combination; older fits get them
# worked out on the fly by the same arithmetic.
LEVEL_VARIANT_FIELDS = (
    "bass_mode", "loudness_mode", "bass_shelf", "headroom_db", "boost_budget",
    "loudness_loss_db", "makeup_db", "net_input_gain_db", "input_gain_linear",
    "predicted_response_db", "correction_response_db", "weighted_rmse_after_db",
    "cross_validation_rmse_after_db",
)


def level_variants_for(profile):
    """Every bass and loudness combination of a fit, stored once.

    A fit made by this version carries them.  An older one gets all six worked
    out on the fly, which means loading the DSP stack once; they are kept with
    the profile from then on, so the next switch is a lookup.
    """
    fit = profile["fit"]
    if fit.get("variants"):
        return fit["variants"]
    load_dsp()
    from calibration_optimizer import (
        BASS_OPTIONS, LOUDNESS_OPTIONS, level_variant, _lowshelf_response_db,
    )
    measurement = profile.get("measurement") or {}
    frequencies = np.asarray(measurement["frequency_hz"], dtype=float)
    measured_smooth = np.asarray(fit["measured_smoothed_db"], dtype=float)
    correction = np.asarray(fit["correction_response_db"], dtype=float)
    shelf = fit.get("bass_shelf") or fit.get("low_shelf")
    if shelf:
        # Stored with its shelf in; the shelf is the one thing that moves.
        correction = correction - _lowshelf_response_db(
            frequencies, shelf["frequency_hz"], shelf["q"], shelf["gain_db"], measurement["rate_hz"],
        )
    fit["variants"] = {
        f"{bass_option}/{loudness_option}": level_variant(
            frequencies, measured_smooth, correction,
            safe_boost_floor=fit.get("safe_boost_floor_hz", fit["highpass"]["frequency_hz"]),
            highpass_hz=fit["highpass"]["frequency_hz"], rate_hz=measurement["rate_hz"],
            bass=bass_option, loudness=loudness_option,
        )[0]
        for bass_option in BASS_OPTIONS for loudness_option in LOUDNESS_OPTIONS
    }
    return fit["variants"]


def level_variant_for(profile, bass, loudness):
    return level_variants_for(profile)[f"{bass}/{loudness}"]


def apply_level_variant(profile, bass, loudness):
    """Move a profile to another bass and loudness setting, in place."""
    variant = level_variant_for(profile, bass, loudness)
    fit = profile["fit"]
    for field in LEVEL_VARIANT_FIELDS:
        if field in variant:
            fit[field] = variant[field]
    fit.pop("low_shelf", None)
    profile["bass"] = bass
    profile["loudness"] = loudness
    safety = profile.setdefault("safety", {})
    safety["input_trim_db"] = -fit["headroom_db"]
    safety["makeup_gain_db"] = fit["makeup_db"]
    return profile


def relevel(bass=None, loudness=None):
    """The Loudness and Make-it-louder switches: no refit, applied live.

    The fit does not depend on either, so nothing is measured or optimized
    again; the stored variant is put in place and the running graph updated.
    The proposal follows when it is this same measurement, so a later refit
    starts from the settings that are playing.
    """
    profile = load_profile(PROFILE)
    if profile is None:
        raise SystemExit("Calibrate the speakers first; there is nothing to switch.")
    bass = bass or profile.get("bass", "normal")
    loudness = loudness or profile.get("loudness", "protected")
    if bass not in BASS_LABELS or loudness not in LOUDNESS_LABELS:
        raise SystemExit("Unknown bass or loudness setting.")
    apply_level_variant(profile, bass, loudness)
    # Heard first; written down after.
    method = activate_profile(profile)
    write_atomic(PROFILE, json.dumps(profile, indent=2) + "\n")
    proposal = load_profile(PROPOSAL)
    if (proposal and not proposal.get("imported")
            and proposal.get("created_at") == profile.get("created_at")):
        apply_level_variant(proposal, bass, loudness)
        write_atomic(PROPOSAL, json.dumps(proposal, indent=2) + "\n")
    else:
        proposal = None
    return {
        "profile": profile, "proposal": proposal, "method": method,
        "bass": bass, "loudness": loudness,
        "message": f"{BASS_LABELS[bass]} · {LOUDNESS_LABELS[loudness]}"
                   + (" · switched live" if method == "live" else " · tuning restarted"),
    }


def refine_from_check():
    """Fold what the check measured back into the raw estimate and fit again."""
    load_dsp()
    profile = load_profile(PROFILE)
    if profile is None:
        raise SystemExit("No calibration is installed to improve.")
    check = load_verification()
    if check is None:
        raise SystemExit("Check the calibration first; there is nothing to learn from yet.")
    if check.get("stale"):
        raise SystemExit(
            "The last check was of a different profile. Check this one first."
        )
    if not check.get("measurement_quality", {}).get("accepted"):
        raise SystemExit(
            "The last check did not measure cleanly, so it cannot be used to improve "
            "anything. Run it again in a quiet room."
        )
    measurement = profile["measurement"]
    frequencies = np.asarray(measurement["frequency_hz"], dtype=float)
    residual = refinement_residual(check, frequencies)
    refined = apply_refinement(measurement, residual)

    previous = measurement.get("refinement") or {}
    total = np.asarray(
        previous.get("residual_db") or np.zeros(frequencies.size), dtype=float
    ) + residual
    refined["refinement"] = {
        "iterations": int(previous.get("iterations", 0)) + 1,
        "residual_db": np.round(total, 3).tolist(),
        "last_step_db": np.round(residual, 3).tolist(),
        "largest_step_db": round(float(np.max(np.abs(residual))), 2),
        "from_check_at": check.get("checked_at"),
    }
    mic = profile["microphone"]
    return profile_from_measurement(
        profile["speaker"], mic, mic.get("channel", 0), profile.get("voicing", "neutral"),
        refined, profile.get("loudness", "protected"), profile.get("bass", "normal"),
        profile.get("channel_trim", "off"),
    )


def verify_calibration():
    """Measure through the corrected output and compare it with the fit.

    Always with the microphone the calibration was made with, on the same
    channels and through the same correction file.  A check with another
    microphone measures the difference between two microphones, not between
    the speakers and the plan, so there is no way to ask for one.
    """
    load_dsp()
    profile = load_profile(PROFILE)
    if profile is None:
        raise SystemExit("No calibration is installed, so there is nothing to check.")
    if not service_active():
        raise SystemExit("The tuning is not running; switch the calibration on first.")
    if compare_state()["bypass"]:
        raise SystemExit(
            "The calibration is switched off, so a check would measure the plain "
            "speakers. Switch it on and try again."
        )
    if not any(item.get("name") == VIRTUAL_SINK for item in pactl_json("sinks")):
        raise SystemExit("The calibrated output is not present; switch the calibration on first.")

    mic = profile["microphone"]
    if not any(item["name"] == mic["name"] for item in microphones()):
        raise SystemExit(
            "The check needs the microphone this calibration was made with "
            f"({label(mic)}), and it is not connected."
        )
    channel = parse_channel_selection(mic.get("channel", 0))
    calibration_file = mic.get("calibration_file")
    # The one measurement that is deliberately made through the correction,
    # but never through the add-on that invents frequencies.
    with added_sound_silenced() as muted:
        measurement = capture_measurement(
            VIRTUAL_SINK, mic["name"], channel, calibration_file,
            # The level default belongs to the real speakers behind the filter.
            level_sink=profile["speaker"]["name"],
            sweeps=VERIFICATION_SWEEPS, recording=VERIFICATION_RECORDING,
        )
    measurement["measured_through"] = {
        "sink": VIRTUAL_SINK, "corrected": True, "bass_enhancer_muted": bool(muted),
    }
    quality = measurement["quality"]
    channels = measurement.get("channels", [])
    snr = (
        np.min(np.vstack([
            np.asarray(item["snr_db"], dtype=float) for item in channels if "snr_db" in item
        ]), axis=0).tolist()
        if channels and all("snr_db" in item for item in channels) else None
    )
    report = verification_report(
        measurement["frequency_hz"],
        measurement["level_dbfs"],
        snr,
        profile["fit"],
        profile["measurement"]["frequency_hz"],
    )
    report.update({
        "bass_enhancer_muted": measurement["measured_through"]["bass_enhancer_muted"],
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "profile_created_at": profile.get("created_at"),
        "profile_label": profile_summary(PROFILE)["label"] if PROFILE.exists() else None,
        "plugin_version": plugin_version(),
        "measurement_quality": quality,
        "level_search": measurement.get("level_search"),
        "stale": False,
    })
    if not quality["accepted"]:
        report["verdict"] = "inconclusive"
        report["notes"] = [
            "The check itself did not measure cleanly, so it says nothing about the "
            "calibration."
        ] + list(quality["failures"])
    write_atomic(VERIFICATION, json.dumps(report, indent=2) + "\n")
    return report


def archived_microphones():
    """Which kinds of microphone have a measurement kept, without the curves.

    The status is read on every panel refresh, so this stays a couple of names
    and dates; the curves themselves are asked for only when they are drawn.
    """
    found = {}
    for kind in ("internal", "external"):
        try:
            raw = read_text_bounded(MICROPHONE_ARCHIVE / f"{kind}.json")
        except OSError:
            continue
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        found[kind] = {
            "microphone": short_label(record.get("microphone")),
            "created_at": short_label(record.get("created_at"), 40),
            "verdict": safe_verdict(record.get("verdict")),
            "calibrated": bool(record.get("calibration_file")),
        }
    return found


# ---- sharing a calibration ---------------------------------------------------
# A calibration is specific to one model's speakers.  An export therefore
# carries the machine it was made on, in the DMI fields Omarchy's own speaker
# tunings are keyed on, and the speaker device; a load says so plainly when
# they differ from this machine's, and never silently.
SHARE_FORMAT = "omarchy-speaker-calibration/1"
SHARE_SUFFIX = ".speaker-calibration.json"
SHARE_LIMIT_BYTES = 1 << 20
SHARE_LIST_LIMIT = 12
SHARE_SCAN_LIMIT = 5000
SHARE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,150}")
DMI_FIELDS = ("sys_vendor", "product_name", "product_version", "product_sku", "board_name")
DMI_PLACEHOLDERS = {
    "", "to be filled by o.e.m.", "default string", "system product name",
    "system version", "system manufacturer", "not specified", "not applicable",
    "none", "n/a", "unknown", "type1productconfigid", "0123456789",
}
# The envelope every shared filter has to fit in, whatever the file says about
# its own limits: the protection the optimizer works under, with a margin.
SHARE_FILTER_LIMIT = 12
SHARE_GAIN_DB = (-18.0, 6.0)
SHARE_FREQUENCY_HZ = (20.0, 20000.0)
SHARE_Q = (0.1, 20.0)
SHARE_HIGHPASS_HZ = (10.0, 400.0)
SHARE_HIGHPASS_Q = (0.3, 2.0)
SHARE_TRIM_DB = 6.0
SHARE_INPUT_GAIN = (0.05, 2.0)
SHARE_ARRAY_LIMIT = 4096
SHARE_TEXT_LIMIT = 400
SHARE_DEPTH_LIMIT = 10
SHARE_KEYS_LIMIT = 200
SHARE_FILTER_TYPES = ("peaking", "lowshelf", "highshelf")


def share_directory():
    """Where exports go and where shared files are looked for: Downloads."""
    for candidate in (os.environ.get("XDG_DOWNLOAD_DIR"), str(Path.home() / "Downloads")):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return DATA / "shared"


def dmi_value(field):
    try:
        raw = read_text_bounded(Path("/sys/class/dmi/id") / field, 4096,
                                errors="ignore", allow_root=True)
    except (OSError, UnsafeFile):
        return ""
    value = short_label((raw or "").strip()) or ""
    return "" if value.lower() in DMI_PLACEHOLDERS else value


def hardware_id():
    """This machine, as the firmware describes it."""
    info = {field: dmi_value(field) for field in DMI_FIELDS}
    label = " ".join(part for part in (info["sys_vendor"], info["product_name"]) if part)
    return {**info, "label": label or "this machine"}


def hardware_matches(theirs, ours):
    """Same model: the SKU when both sides have one, else vendor and product."""
    theirs, ours = theirs or {}, ours or {}
    if theirs.get("product_sku") and ours.get("product_sku"):
        return theirs["product_sku"] == ours["product_sku"]
    if not theirs.get("product_name"):
        return False
    key = lambda info: ((info.get("sys_vendor") or "").lower(), (info.get("product_name") or "").lower())
    return key(theirs) == key(ours)


def write_shared(name, text, subdirectory=None):
    """Publish a shareable file in Downloads without touching the folder itself.

    ``write_atomic`` tightens its directory to 0700, which is right for the
    plugin's own state and wrong for a folder the user shares with a browser,
    so this does the same exclusive-temporary-then-rename dance by hand and
    leaves the folder's mode alone.  The file is 0644: it is meant to be
    handed around.
    """
    directory = share_directory()
    if directory == DATA / "shared":
        destination = directory / subdirectory / name if subdirectory else directory / name
        write_atomic(destination, text, mode=0o644)
        return destination
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(dfd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise UnsafeFile(f"refusing to write into {directory}: not a directory of ours")
        if subdirectory:
            # One level down, created if missing and checked on its descriptor
            # like the folder above it; never followed through a symlink.
            try:
                os.mkdir(subdirectory, 0o755, dir_fd=dfd)
            except FileExistsError:
                pass
            sub = os.open(subdirectory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
            os.close(dfd)
            dfd = sub
            directory = directory / subdirectory
            info = os.fstat(dfd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise UnsafeFile(f"refusing to write into {directory}: not a directory of ours")
        temporary = f".{name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o644, dir_fd=dfd)
        try:
            os.fchmod(fd, 0o644)
            view = memoryview(text.encode("utf-8"))
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
            os.rename(temporary, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            os.fsync(dfd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=dfd)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    return directory / name


def export_profile():
    """The calibration that is playing, as one file to hand to someone."""
    profile = load_profile(PROFILE)
    if profile is None:
        raise SystemExit("No calibration is installed, so there is nothing to export.")
    hardware = hardware_id()
    kind = MICROPHONE_KIND_LABELS[microphone_kind(profile)]
    day = str(profile.get("created_at", ""))[:10] or dt.date.today().isoformat()
    name = f"{hardware['label']} · {kind} · {day}"
    shared = dict(profile)
    for key in ("activation", "installed", "imported"):
        shared.pop(key, None)
    mic = dict(shared.get("microphone") or {})
    # The correction file is a path on the exporting machine; only whether
    # there was one travels.
    mic["calibration_file"] = bool(mic.get("calibration_file"))
    shared["microphone"] = mic
    speaker = profile.get("speaker") or {}
    payload = {
        "format": SHARE_FORMAT,
        "name": name,
        "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "plugin_version": plugin_version(),
        "hardware": {**hardware, "speaker": speaker.get("name"),
                     "speaker_description": short_label(speaker.get("description"))},
        "profile": shared,
    }
    slug = re.sub(r"[^a-z0-9]+", "-", f"{hardware['label']} {kind} {day}".lower()).strip("-")[:80]
    path = write_shared(f"{slug or 'calibration'}{SHARE_SUFFIX}", json.dumps(payload, indent=1) + "\n")
    return {
        "file": path.name, "directory": str(path.parent), "name": name, "hardware": hardware,
        "message": f"Saved {path.name} in {path.parent}. Hand that file to someone with the same "
                   "machine; dropped into their Downloads folder, it shows up in their panel.",
    }


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _within(value, bounds):
    return _finite(value) and bounds[0] <= float(value) <= bounds[1]


def bounded_copy(value, depth=0):
    """A copy of a document with every string, number, list and dict bounded."""
    if depth > SHARE_DEPTH_LIMIT:
        raise ValueError("nested too deeply")
    if isinstance(value, dict):
        if len(value) > SHARE_KEYS_LIMIT:
            raise ValueError("too many keys")
        return {str(key)[:64]: bounded_copy(item, depth + 1)
                for key, item in value.items() if isinstance(key, str)}
    if isinstance(value, list):
        if len(value) > SHARE_ARRAY_LIMIT:
            raise ValueError("an array is too long")
        return [bounded_copy(item, depth + 1) for item in value]
    if isinstance(value, str):
        return short_label(value, SHARE_TEXT_LIMIT) or ""
    if value is None or isinstance(value, bool) or _finite(value):
        return value
    raise ValueError("a number is not finite")


def _check_section(section, what, gain_bounds=SHARE_GAIN_DB):
    if not isinstance(section, dict):
        raise ValueError(f"{what} is not a filter")
    if not _within(section.get("frequency_hz"), SHARE_FREQUENCY_HZ):
        raise ValueError(f"{what} has a frequency outside {SHARE_FREQUENCY_HZ}")
    if not _within(section.get("q", 0.707), SHARE_Q):
        raise ValueError(f"{what} has a Q outside {SHARE_Q}")
    if not _within(section.get("gain_db", 0.0), gain_bounds):
        raise ValueError(f"{what} has a gain outside {gain_bounds} dB")


def valid_shared_payload(payload):
    """The document a shared file must be, or ValueError saying why not.

    Everything is bounded first, then every value that reaches the filter
    chain is held to the same envelope the optimizer works in.  A file that
    fails is refused, not repaired: a calibration with one value out of range
    is not one to install with that value clamped.
    """
    if not isinstance(payload, dict) or payload.get("format") != SHARE_FORMAT:
        raise ValueError("not a shared calibration file")
    payload = bounded_copy(payload)
    profile = payload.get("profile")
    if not isinstance(profile, dict):
        raise ValueError("no calibration inside")
    fit = profile.get("fit")
    if not isinstance(fit, dict):
        raise ValueError("no filter set")
    filters = fit.get("filters")
    if not isinstance(filters, list) or not 1 <= len(filters) <= SHARE_FILTER_LIMIT:
        raise ValueError(f"between 1 and {SHARE_FILTER_LIMIT} filters are expected")
    for index, item in enumerate(filters, 1):
        if not isinstance(item, dict) or item.get("type", "peaking") not in SHARE_FILTER_TYPES:
            raise ValueError(f"filter {index} has an unknown type")
        _check_section(item, f"filter {index}")
    for key in ("bass_shelf", "low_shelf", "high_shelf"):
        if fit.get(key):
            _check_section(fit[key], key)
    highpass = fit.get("highpass")
    if highpass is not None:
        if not isinstance(highpass, dict) or not _within(highpass.get("frequency_hz"), SHARE_HIGHPASS_HZ):
            raise ValueError(f"the high-pass corner is outside {SHARE_HIGHPASS_HZ} Hz")
        if not _within(highpass.get("q", 0.707), SHARE_HIGHPASS_Q):
            raise ValueError("the high-pass Q is out of range")
        if highpass.get("stages", 1) not in (1, 2):
            raise ValueError("the high-pass must have one or two stages")
    if "highpass_hz" in fit and not _within(fit["highpass_hz"], SHARE_HIGHPASS_HZ):
        raise ValueError(f"the high-pass corner is outside {SHARE_HIGHPASS_HZ} Hz")
    trim = fit.get("channel_trim")
    if trim:
        if not isinstance(trim, dict) or not all(
                _within(trim.get(side, 0.0), (-SHARE_TRIM_DB, SHARE_TRIM_DB)) for side in ("left_db", "right_db")):
            raise ValueError(f"channel trim beyond ±{SHARE_TRIM_DB:.0f} dB")
    if not _within(fit.get("input_gain_linear"), SHARE_INPUT_GAIN):
        raise ValueError("the input gain is missing or out of range")
    quality = profile.get("quality")
    if not isinstance(quality, dict) or quality.get("accepted") is not True:
        raise ValueError("the measurement did not pass its own quality checks")
    quality["verdict"] = safe_verdict(quality.get("verdict"))
    for key, labels, default in (("voicing", VOICING_LABELS, "neutral"), ("bass", BASS_LABELS, "normal"),
                                 ("loudness", LOUDNESS_LABELS, "protected")):
        if profile.get(key) not in labels:
            profile[key] = default
    mic = profile.get("microphone")
    if not isinstance(mic, dict):
        raise ValueError("no microphone record")
    mic["internal"] = bool(mic.get("internal"))
    mic["calibration_file"] = None
    return payload


def local_speaker():
    """The speaker output a loaded calibration is applied to here."""
    current = load_profile(PROFILE)
    if current and (current.get("speaker") or {}).get("name"):
        return current["speaker"]
    sinks = [item for item in pactl_json("sinks")
             if str(item.get("name", "")).startswith("alsa_output.") and item.get("name") != VIRTUAL_SINK]
    if not sinks:
        raise SystemExit("No speaker output was found on this machine.")
    return {"name": sinks[0]["name"], "description": label(sinks[0])}


def shared_summary(path, ours, sinks):
    """One row for the panel: what the file is and whether it fits this machine."""
    try:
        raw = read_text_bounded(path, SHARE_LIMIT_BYTES, missing_ok=False)
        payload = valid_shared_payload(json.loads(raw or ""))
    except (OSError, UnsafeFile, ValueError) as error:
        return {"file": path.name, "valid": False,
                "reason": short_label(str(error), SHARE_TEXT_LIMIT) or "cannot be read"}
    theirs = payload.get("hardware") or {}
    profile = payload["profile"]
    return {
        "file": path.name, "valid": True,
        "name": short_label(payload.get("name")) or path.name,
        "hardware": short_label(theirs.get("label")) or "unknown hardware",
        "created_at": short_label(profile.get("created_at"), 40),
        "microphone": MICROPHONE_KIND_LABELS[microphone_kind(profile)],
        "matches": {"machine": hardware_matches(theirs, ours), "speakers": theirs.get("speaker") in sinks},
    }


def shared_profiles():
    """Shared calibrations sitting in Downloads, newest first, a dozen at most."""
    directory = share_directory()
    candidates = []
    try:
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                if count >= SHARE_SCAN_LIMIT:
                    break
                if entry.name.endswith(SHARE_SUFFIX) and entry.is_file(follow_symlinks=False):
                    candidates.append((entry.stat(follow_symlinks=False).st_mtime, entry.name))
    except OSError:
        return []
    candidates.sort(reverse=True)
    ours = hardware_id()
    sinks = {item.get("name") for item in pactl_json("sinks")}
    return [shared_summary(directory / name, ours, sinks) for _, name in candidates[:SHARE_LIST_LIMIT]]


def import_profile(name=None, path=None):
    """Make a shared calibration the last measurement, ready to install.

    It goes through the same door as a fresh measurement: it becomes the
    proposal, and Install applies it while the previous profile is kept for
    comparison.  When the hardware differs from this machine the result says
    so, and when the exporting machine's speaker device does not exist here
    the calibration is pointed at this machine's speakers instead.
    """
    if path:
        source = Path(path)
    else:
        if (not name or "/" in name or name in (".", "..") or not name.endswith(SHARE_SUFFIX)
                or not SHARE_NAME_PATTERN.fullmatch(name)):
            raise SystemExit("That is not the name of a shared calibration file.")
        source = share_directory() / name
    try:
        raw = read_text_bounded(source, SHARE_LIMIT_BYTES, missing_ok=False)
        payload = valid_shared_payload(json.loads(raw or ""))
    except FileNotFoundError:
        raise SystemExit(f"{source.name} is not there any more.")
    except (OSError, UnsafeFile) as error:
        raise SystemExit(f"{source.name} cannot be read: {error}")
    except ValueError as error:
        raise SystemExit(f"{source.name} cannot be loaded: {error}.")
    ours = hardware_id()
    theirs = payload.get("hardware") or {}
    sinks = {item.get("name") for item in pactl_json("sinks")}
    matches = {"machine": hardware_matches(theirs, ours), "speakers": theirs.get("speaker") in sinks}
    profile = payload["profile"]
    if not matches["speakers"]:
        profile["speaker"] = local_speaker()
    # The switches are this machine's, not the exporter's.
    current = load_profile(PROFILE) or {}
    profile["deep_bass"] = current.get("deep_bass", DEEP_BASS_DEFAULT)
    profile["loudness_compensation"] = current.get("loudness_compensation", LOUDNESS_COMPENSATION_DEFAULT)
    profile["imported"] = {
        "file": source.name, "name": short_label(payload.get("name")),
        "exported_at": short_label(payload.get("exported_at"), 40),
        "hardware": {key: short_label(theirs.get(key)) for key in DMI_FIELDS + ("label", "speaker_description")},
        "this_machine": ours["label"], "matches": matches,
    }
    write_atomic(PROPOSAL, json.dumps(profile, indent=2) + "\n")
    warning = None if matches["machine"] else (
        f"It was made on {theirs.get('label') or 'another machine'}; this is {ours['label']}. "
        "Speakers differ between models, so it may sound wrong here."
    )
    return {
        "proposal": profile, "matches": matches, "warning": warning,
        "message": f"Loaded {profile['imported']['name'] or source.name}. Press Install last "
                   "measurement to hear it; Switch profile brings your own back."
                   + (f" {warning}" if warning else ""),
    }


# ---- an Omarchy vendor tuning --------------------------------------------------
# Omarchy ships speaker tunings for known laptops under
# default/audio/tunings/<vendor>-<model>/ as a tuning.conf and a
# filter-chain.conf, matched by DMI at first run.  A calibration made here is
# most of such a tuning already: the same host, the same sink name, the same
# kind of sections and the same limiter.  This renders it in that layout, with
# the four figures Omarchy asks a tuning to report, so that it can be offered
# as a pull request.  What Omarchy does not ship stays out: the bass add-on,
# the loudness compensator and the volume following.
VENDOR_LIMITER_THRESHOLD_DB = -1.0
VENDOR_SIMULATION_SECONDS = 20.0
VENDOR_REFERENCE_SECONDS = 60.0
VENDOR_RATE_HZ = 48000


def vendor_slug(hardware):
    text = f"{hardware.get('sys_vendor') or 'laptop'} {hardware.get('product_name') or 'speakers'}"
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "laptop-speakers"


def vendor_sections(fit):
    """The fit as (kind, frequency, q, gain) sections in Omarchy's order.

    High-pass stages first, then the shelves and peaking sections by
    frequency, then the high shelf; sections that do nothing are left out.
    """
    corner, q, stages = highpass_settings(fit)
    sections = [("highpass", float(corner), float(q), 0.0)] * stages
    peaking, low_shelf, high_shelf, bass = fit_sections(fit)
    for shelf in (low_shelf, bass):
        if shelf and abs(float(shelf.get("gain_db", 0.0))) > 0.005:
            sections.append(("lowshelf", float(shelf["frequency_hz"]),
                             float(shelf.get("q", 0.707)), float(shelf["gain_db"])))
    for frequency, q_value, gain in sorted(peaking, key=lambda item: float(item[0])):
        if abs(float(gain)) > 0.005:
            sections.append(("peaking", float(frequency), float(q_value), float(gain)))
    if high_shelf and abs(float(high_shelf.get("gain_db", 0.0))) > 0.005:
        sections.append(("highshelf", float(high_shelf["frequency_hz"]),
                         float(high_shelf.get("q", 0.707)), float(high_shelf["gain_db"])))
    return sections


def _plain(value):
    return f"{round(float(value), 3):g}"


def vendor_harmonics(profile):
    """Deep bass for a chain without the add-on: bankstown's own recipe.

    The add-on takes what lies below the knee, saturates it, keeps the
    harmonics between the knee and three times the knee, and adds them back
    ahead of the EQ.  Every step is a filter, a tanh and a sum, and a tanh is
    exp, log and a few linear stages, so the whole of it is expressible in
    PipeWire's built-in nodes and Omarchy can ship it without the add-on.
    Only when the profile has deep bass on; the numbers are the plugin's.
    """
    if profile.get("deep_bass") != "on":
        return None
    return harmonic_settings(highpass_settings(profile["fit"])[0])


def vendor_chain(sections, trim_db, input_gain, header, harmonics=None):
    """filter-chain.conf in the layout Omarchy ships."""
    nodes, links, inputs = [], [], []
    for side in ("l", "r"):
        names = []
        if harmonics:
            hb_nodes, hb_links, _, _ = harmonic_nodes(
                side, harmonics, 2.0 * harmonics["scale"] * harmonics["amount"])
            nodes.extend(hb_nodes)
            links.extend(hb_links)
            names.append(f"hb_mix_{side}")
        for index, (kind, frequency, q, gain) in enumerate(sections):
            name = f"s{index}_{side}"
            control = f'"Freq" = {_plain(frequency)} "Q" = {_plain(q)}'
            if kind != "highpass":
                control += f' "Gain" = {_plain(gain)}'
            nodes.append(f'{{ type = builtin name = {name:<8} label = bq_{kind:<10} control = {{ {control} }} }}')
            names.append(name)
        gain_db = float(trim_db.get(side, 0.0))
        if abs(gain_db) > 0.005:
            name = f"s{len(sections)}_{side}"
            nodes.append(f'{{ type = builtin name = {name:<8} label = linear        control = {{ "Mult" = {10.0 ** (gain_db / 20.0):.6f} "Add" = 0 }} }}')
            names.append(name)
        nodes.append("")
        for before, after in zip(names, names[1:]):
            links.append(f'{{ output = "{before}:Out" input = "{after}:In" }}')
        links.append(f'{{ output = "{names[-1]}:Out" input = "limiter:in_{side}" }}')
        inputs.append(f'"hb_in_{side}:In"' if harmonics else f'"{names[0]}:In"')
    nodes.append(f'''{{ type   = lv2
            name   = limiter
            plugin = "http://lsp-plug.in/plugins/lv2/limiter_stereo"
            control = {{
              # Both default to enabled: "alr" regulates level toward the
              # threshold and "boost" normalises the threshold up to full
              # scale. A fixed tuning must switch them off or its tone drifts
              # with programme level.
              "alr"   = 0
              "boost" = 0
              "g_in"  = {float(input_gain):.4f}
              "th"    = 0.891
            }}
          }}''')
    joined_nodes = "\n          ".join(nodes).rstrip()
    joined_links = "\n          ".join(links)
    return f'''{header}
context.modules = [
  {{ name = libpipewire-module-filter-chain
    args = {{
      node.description = "Laptop Speakers"
      media.name       = "Laptop Speakers"

      filter.graph = {{
        nodes = [
          {joined_nodes}
        ]

        links = [
          {joined_links}
        ]

        inputs  = [ {" ".join(inputs)} ]
        outputs = [ "limiter:out_l" "limiter:out_r" ]
      }}

      audio.channels = 2
      audio.position = [ FL FR ]

      capture.props = {{
        node.name   = "{VIRTUAL_SINK}"
        media.class = Audio/Sink
      }}
      playback.props = {{
        node.name     = "{VIRTUAL_SINK}_output"
        node.passive  = true
        target.object = "@SPEAKER_SINK@"
        # The filter's output is a movable sink input like any other; pinned so
        # that rerouting "all streams" cannot drag the processing along.
        node.dont-move = true
        # Wait for the named target rather than linking to whatever default
        # exists while the speaker sink is still being discovered.
        node.dont-fallback = true
        node.linger = true
      }}
    }}
  }}
]
'''


def _pink_noise(seconds, rate, seed):
    samples = int(seconds * rate)
    spectrum = np.fft.rfft(np.random.default_rng(seed).standard_normal(samples))
    frequencies = np.fft.rfftfreq(samples, 1.0 / rate)
    spectrum[1:] /= np.sqrt(frequencies[1:])
    spectrum[0] = 0.0
    noise = np.fft.irfft(spectrum, samples)
    return noise / np.max(np.abs(noise))


def vendor_test_signal(reference, rate):
    """A hot master to run through the chain: a track, or pink noise."""
    if reference:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(reference), "-t", str(VENDOR_REFERENCE_SECONDS),
             "-ac", "2", "-ar", str(rate), "-f", "f32le", "-"],
            capture_output=True, check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            raise SystemExit(f"ffmpeg could not read {reference}: {proc.stderr.decode('utf-8', 'ignore')[:200]}")
        signal = np.frombuffer(proc.stdout, dtype=np.float32).astype(float)
        signal = signal[: signal.size - signal.size % 2].reshape(-1, 2)
        return signal, f"{Path(reference).name}, first {VENDOR_REFERENCE_SECONDS:.0f} s"
    left = _pink_noise(VENDOR_SIMULATION_SECONDS, rate, 1)
    right = _pink_noise(VENDOR_SIMULATION_SECONDS, rate, 2)
    peak = 10.0 ** (-0.1 / 20.0)
    return np.stack([left, right], axis=1) * peak, f"pink noise, {VENDOR_SIMULATION_SECONDS:.0f} s, peaks at -0.1 dBFS"


def ebur128_lra(signal, rate):
    """Loudness range in LU as ffmpeg's ebur128 reports it, or None."""
    if not shutil.which("ffmpeg"):
        return None
    pcm = np.clip(signal, -1.0, 1.0).astype(np.float32).tobytes()
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "info", "-f", "f32le", "-ar", str(rate), "-ac", "2", "-i", "-",
         "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
        input=pcm, capture_output=True, check=False,
    )
    match = re.search(r"LRA:\s*(-?\d+(?:\.\d+)?)\s*LU", proc.stderr.decode("utf-8", "ignore"))
    return float(match.group(1)) if match else None


def vendor_metrics(sections, input_gain, rate, reference=None, harmonics=None):
    """The four figures Omarchy asks a tuning to report, less the fit's own."""
    load_dsp()
    from calibration_optimizer import group_delay_swing_ms, chain_sos
    import scipy.signal
    swing = group_delay_swing_ms(sections, rate)
    signal, label = vendor_test_signal(reference, rate)
    if harmonics:
        # The same recipe the chain carries, sample for sample.
        h = harmonics
        band = chain_sos([("highpass", h["floor_hz"], 0.707, 0.0), ("lowpass", h["ceil_hz"], 0.707, 0.0)], rate)
        after = chain_sos([("highpass", h["final_hp_hz"], 0.707, 0.0), ("lowpass", 3.0 * h["ceil_hz"], 0.707, 0.0)], rate)
        clipped = np.clip(signal, -10.0, 10.0)
        added = np.stack([
            scipy.signal.sosfilt(after, h["scale"] * h["amount"] * np.tanh(
                h["drive"] * scipy.signal.sosfilt(band, clipped[:, channel])))
            for channel in range(2)], axis=1)
        signal = clipped + added
    sos = chain_sos(sections, rate)
    if sos.size:
        processed = np.stack([scipy.signal.sosfilt(sos, signal[:, channel]) for channel in range(2)], axis=1)
    else:
        processed = signal.copy()
    processed = processed * float(input_gain)
    peak_dbfs = 20.0 * math.log10(max(float(np.max(np.abs(processed))), 1e-9))
    before, after = ebur128_lra(signal, rate), ebur128_lra(processed, rate)
    return {
        "bass_group_delay_swing_ms": round(swing, 1),
        "limiter_headroom_db": round(VENDOR_LIMITER_THRESHOLD_DB - peak_dbfs, 1),
        "peak_dbfs": round(peak_dbfs, 2),
        "dynamic_range_delta_lu": round(after - before, 1) if before is not None and after is not None else None,
        "signal": label,
    }


def render_vendor_tuning(reference=None):
    """The playing calibration as an Omarchy vendor tuning, as texts."""
    profile = load_profile(PROFILE)
    if profile is None:
        raise SystemExit("No calibration is installed, so there is nothing to render.")
    fit = profile["fit"]
    hardware = hardware_id()
    slug = vendor_slug(hardware)
    sections = vendor_sections(fit)
    trim = fit.get("channel_trim") or {}
    trim_db = {"l": float(trim.get("left_db", 0.0)), "r": float(trim.get("right_db", 0.0))}
    input_gain = float(fit.get("input_gain_linear", 1.0))
    harmonics = vendor_harmonics(profile)
    metrics = vendor_metrics(sections, input_gain, VENDOR_RATE_HZ, reference, harmonics)
    mic = profile.get("microphone") or {}
    kind = "the built-in microphones" if mic.get("internal") else "an external measuring microphone at the listening position"
    when = str(profile.get("created_at", ""))[:10]
    today = dt.date.today().isoformat()
    label = hardware["label"]
    sku = hardware.get("product_sku") or ""
    sink_name = (profile.get("speaker") or {}).get("name") or ""
    voicing = VOICING_LABELS.get(profile.get("voicing"), "flat")
    header = f'''# {label} speaker tuning.
#
# Fitted by the Omarchy Speaker Calibrator {plugin_version()} from a swept-sine
# measurement of the internal speakers with {kind}, on {when}: {len(sections)}
# sections and a lookahead limiter. Cuts are preferred; boosts are allowed
# only where a held-out repeat confirmed them and are paid for by the input
# gain, so peaks never exceed what the limiter is told to expect. The
# high-pass sits at the measured knee below which these drivers make no
# usable output. Target voicing: {voicing}.
#
# Measures {fit.get("weighted_rmse_after_db", 0.0):.2f} dB RMS against the calibrator's target
# (perceptually weighted over its own error metric). See tuning.conf.
#
# Channels are wired explicitly because the limiter is a stereo plugin; a mono
# graph is duplicated per channel and would limit each side independently,
# shifting the stereo image on bass transients.'''
    if harmonics:
        header += f'''
#
# The hb_* nodes ahead of the EQ are deep bass: what lies below the knee
# ({harmonics["ceil_hz"]:.0f} Hz), which these drivers cannot play, is saturated
# ({harmonics["scale"]:.3f} * {harmonics["amount"]} * tanh({harmonics["drive"]} * x), the tanh written as
# 2/(1 + e^-2u) with exp and log, less a constant the high-pass removes) and
# its harmonics between the knee and three times the knee are added back, so
# the ear hears the note the speaker never made. It is the bankstown add-on's
# own recipe, in built-in nodes, verified against it to 0.1 dB.'''
    chain = vendor_chain(sections, trim_db, input_gain, header, harmonics)
    match_line = (f'match_sku=("{sku}")' if sku
                  else f'match_dmi=("{hardware.get("product_name", "")}")   ## no DMI SKU on this machine; substring of the product name')
    lra = metrics["dynamic_range_delta_lu"]
    tuning = f'''## {label} internal speakers.
##
## Fitted by the Omarchy Speaker Calibrator from a swept-sine measurement of
## the speakers with {kind}. The sections and the limiter are in
## filter-chain.conf; the bass add-on, loudness compensation and volume
## following the plugin can add are deliberately not part of this tuning.

description="{label} speakers"
{'## Deep bass is included: harmonics of the bass below the knee, made from built-in nodes.' if harmonics else '## No deep bass: the calibration was exported with that switch off.'}
## Matched on the DMI product SKU, compared as a whole value.
{match_line}
## The internal speaker sink, as PipeWire names it on this machine.  Plain
## dots on purpose: Omarchy hands this to awk -v, which eats backslashes.
sink_pattern='^{sink_name}$'

## Provenance.
derived_from="Omarchy Speaker Calibrator {plugin_version()}: sweep measurement with {kind}, {voicing} target, measured {when}"
validated_by=""   ## your name, once you have listened on the hardware named below
validated_on="{today}"
validated_hardware="{label}{f' ({sku})' if sku else ''}"

## Measurements.
magnitude_rms_db="{fit.get("weighted_rmse_after_db", 0.0):.2f}"   ## against the calibrator's target, perceptually weighted
bass_group_delay_swing_ms="{metrics["bass_group_delay_swing_ms"]}"   ## from the biquad coefficients, 30-300 Hz
limiter_headroom_db="{metrics["limiter_headroom_db"]}"   ## threshold (-1 dBFS) minus the peak of {metrics["signal"]} after the chain and input gain
dynamic_range_delta_lu="{lra if lra is not None else ''}"   ## LRA after minus before on the same signal, ffmpeg ebur128{'' if lra is not None else ' (ffmpeg was not available)'}
'''
    readme = f'''This is a speaker tuning for Omarchy, rendered by the Omarchy Speaker Calibrator.

To offer it to Omarchy: copy this directory into a checkout of
https://github.com/omacom/omarchy as default/audio/tunings/{slug}/, listen to it
on the hardware, fill in validated_by in tuning.conf, and open a pull request.
Omarchy's docs/audio-tuning.md describes what a tuning must report.

Files:
  tuning.conf        description, hardware match, provenance, measurements
  filter-chain.conf  the graph, @SPEAKER_SINK@ substituted by Omarchy on install
'''
    return {"slug": slug, "label": label, "tuning": tuning, "chain": chain, "readme": readme,
            "metrics": metrics, "sections": len(sections)}


def write_vendor_export(rendered):
    """The rendered tuning as files in Downloads, ready to hand over."""
    subdirectory = f"omarchy-tuning-{rendered['slug']}"
    written = [write_shared("tuning.conf", rendered["tuning"], subdirectory=subdirectory),
               write_shared("filter-chain.conf", rendered["chain"], subdirectory=subdirectory),
               write_shared("README.txt", rendered["readme"], subdirectory=subdirectory)]
    metrics = rendered["metrics"]
    return {
        "directory": str(written[0].parent), "files": [path.name for path in written],
        "slug": rendered["slug"], "sections": rendered["sections"], "metrics": metrics,
        "message": f"Written to {written[0].parent}: tuning.conf, filter-chain.conf and a README. "
                   f"{rendered['sections']} sections, group delay swing {metrics['bass_group_delay_swing_ms']} ms, "
                   f"limiter headroom {metrics['limiter_headroom_db']} dB. Listen, fill in validated_by, "
                   f"and offer it as default/audio/tunings/{rendered['slug']}/ in a pull request to Omarchy.",
    }


def vendor_tuning(reference=None):
    """Write the playing calibration as an Omarchy vendor tuning."""
    return write_vendor_export(render_vendor_tuning(reference))


# ---- hearing the vendor tuning the way Omarchy installs it -------------------
# Omarchy's own installer, omarchy-audio-tuning, reads its tunings from
# $OMARCHY_PATH/default/audio/tunings and writes the same three files this
# plugin writes, under the same names.  Pointing it at a private copy of the
# Omarchy tree with the rendered tuning added runs the real thing, with its
# own matching and verification, and without root.  The calibration is
# stopped for the duration and put back afterwards.
VENDOR_TRIAL = DATA / "vendor-trial.json"
VENDOR_OVERLAY = DATA / "omarchy-path"
OMARCHY_SHARE = Path(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy")
VENDOR_HEADER = "Fitted by the Omarchy Speaker Calibrator"
CALIBRATOR_HEADER = "# Generated by Omarchy Speaker Calibrator"
# The gapless way to hear the rendered tuning: a second host beside the
# calibration, with its own sink, and the playing streams moved across.  A
# PipeWire stream moves between sinks without a break, so nothing stops and
# nothing restarts; the calibration keeps running for anything new.
TRIAL_SINK = "omarchy_speaker_trial"
TRIAL_SERVICE = "omarchy-speaker-trial.service"
TRIAL_HOST = CONFIG / "pipewire/omarchy-speaker-trial.conf"
TRIAL_FRAGMENT = CONFIG / "pipewire/omarchy-speaker-trial.conf.d/90-trial.conf"
TRIAL_UNIT = CONFIG / "systemd/user" / TRIAL_SERVICE
TRIAL_UNIT_TEXT = (UNIT_TEXT
                   .replace("omarchy-speaker-tuning.conf", "omarchy-speaker-trial.conf")
                   .replace("Omarchy speaker tuning filter-chain", "Omarchy speaker calibrator: exported tuning on trial"))


def trial_graph(chain, speaker):
    """The rendered chain as a second sink: its own names, the real target."""
    return (chain.replace("@SPEAKER_SINK@", speaker)
            .replace(f'"{VIRTUAL_SINK}_output"', f'"{TRIAL_SINK}_output"')
            .replace(f'"{VIRTUAL_SINK}"', f'"{TRIAL_SINK}"')
            .replace('"Laptop Speakers"', '"Exported tuning (trial)"'))


def trial_sink_present():
    return any(item.get("name") == TRIAL_SINK for item in pactl_json("sinks"))


def trial_service_active():
    return run(["systemctl", "--user", "is-active", TRIAL_SERVICE], check=False, capture=True).stdout.strip() == "active"


def calibration_sink_present():
    return any(item.get("name") == VIRTUAL_SINK for item in pactl_json("sinks"))


def stop_trial_host():
    run(["systemctl", "--user", "stop", TRIAL_SERVICE], check=False, capture=True)
    for path in (TRIAL_FRAGMENT, TRIAL_HOST, TRIAL_UNIT):
        try:
            path.unlink()
        except OSError:
            pass
    run(["systemctl", "--user", "daemon-reload"], check=False, capture=True)


def graph_kind():
    """Whose graph the tuning host is running from: ours, a trial, another, none."""
    try:
        # The whole file, within the usual bound: the reader refuses anything
        # over its limit rather than handing back a prefix.
        text = read_text_bounded(FRAGMENT, errors="ignore")
    except (OSError, UnsafeFile):
        return "none"
    if not text:
        return "none"
    if text.startswith(CALIBRATOR_HEADER):
        return "calibrator"
    if VENDOR_HEADER in text[:600]:
        return "vendor-trial"
    return "other"


def vendor_export_status():
    """Where the last rendered tuning is, when there is one."""
    slug = vendor_slug(hardware_id())
    directory = share_directory() / f"omarchy-tuning-{slug}"
    if (directory / "tuning.conf").is_file() and (directory / "filter-chain.conf").is_file():
        return {"slug": slug, "directory": str(directory)}
    return None


def vendor_trial_active():
    try:
        marker = json.loads(read_text_bounded(VENDOR_TRIAL) or "")
    except (OSError, UnsafeFile, ValueError):
        return False
    return bool(marker) and (graph_kind() == "vendor-trial" or trial_sink_present())


def build_vendor_overlay(rendered):
    """A private Omarchy tree: links to the real one, plus the rendered tuning.

    Only the tunings directory is real, and it holds nothing but a copy of
    what was just rendered: Omarchy's script sources tuning.conf as shell, so
    what it reads is written here from memory, never taken from a folder
    another program can write to.
    """
    if VENDOR_OVERLAY.is_symlink():
        raise UnsafeFile(f"refusing to use {VENDOR_OVERLAY}: it is a symlink")
    if VENDOR_OVERLAY.exists():
        shutil.rmtree(VENDOR_OVERLAY)
    secure_directory(DATA)
    VENDOR_OVERLAY.mkdir(0o700)
    for entry in OMARCHY_SHARE.iterdir():
        if entry.name != "default":
            os.symlink(entry, VENDOR_OVERLAY / entry.name)
    default = VENDOR_OVERLAY / "default"
    default.mkdir(0o700)
    for entry in (OMARCHY_SHARE / "default").iterdir():
        if entry.name != "audio":
            os.symlink(entry, default / entry.name)
    audio = default / "audio"
    audio.mkdir(0o700)
    for entry in (OMARCHY_SHARE / "default" / "audio").iterdir():
        if entry.name != "tunings":
            os.symlink(entry, audio / entry.name)
    tuning_dir = audio / "tunings" / rendered["slug"]
    tuning_dir.mkdir(0o700, parents=True)
    write_atomic(tuning_dir / "tuning.conf", rendered["tuning"])
    write_atomic(tuning_dir / "filter-chain.conf", rendered["chain"])
    return VENDOR_OVERLAY


def omarchy_audio_tuning(action, overlay=None):
    environment = dict(os.environ)
    if overlay is not None:
        environment["OMARCHY_PATH"] = str(overlay)
    try:
        proc = subprocess.run(
            ["omarchy-audio-tuning", action] + (["--force"] if action == "on" else []),
            capture_output=True, text=True, check=False, timeout=120, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, "", str(error)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def reinstall_profile(profile):
    """Put the plugin's own files back and play the profile: what install does,
    without a new previous-profile slot, because nothing new was measured."""
    graph = filter_config(
        profile["speaker"]["name"], profile["fit"],
        deep_bass=profile.get("deep_bass") == "on",
        loudness_compensation=profile.get("loudness_compensation") == "on",
        sink_volume_db=sink_volume_db(listening_sink(profile)),
    )
    for path in (HOST, FRAGMENT, UNIT):
        secure_directory(path.parent)
    write_atomic(HOST, HOST_TEXT)
    write_atomic(FRAGMENT, graph)
    write_atomic(UNIT, UNIT_TEXT)
    method = activate_profile(profile)
    if profile.get("loudness_compensation") == "on":
        start_loudness_tracker()
    return method


def vendor_try(reference=None, installer=False):
    """Hear the rendered tuning: beside the calibration, or through Omarchy's installer.

    The default starts a second tuning host with the rendered chain and moves
    the playing streams onto its sink, which a PipeWire stream survives
    without a break.  ``installer`` instead runs Omarchy's own installer
    against a private copy of its tree, which is the faithful check of the
    files but restarts the one tuning host and so interrupts playback.
    """
    rendered = render_vendor_tuning(reference)
    export = write_vendor_export(rendered)
    if not installer:
        return vendor_try_beside(rendered, export)
    overlay = build_vendor_overlay(rendered)
    forget_loudness_tracker()
    run(["systemctl", "--user", "disable", "--now", SERVICE], check=False, capture=True)
    write_atomic(VENDOR_TRIAL, json.dumps({
        "slug": rendered["slug"], "directory": export["directory"],
        "since": dt.datetime.now(dt.timezone.utc).isoformat(),
    }) + "\n")
    code, out, err = omarchy_audio_tuning("on", overlay)
    if code != 0 or graph_kind() != "vendor-trial":
        try:
            VENDOR_TRIAL.unlink()
        except OSError:
            pass
        profile = load_profile(PROFILE)
        if profile:
            reinstall_profile(profile)
        raise SystemExit(
            "Omarchy's installer did not take the tuning, so the calibration is back. "
            + short_label((err or out or "no output").splitlines()[-1], SHARE_TEXT_LIMIT)
        )
    return {
        "trial": True, "export": export, "installer": out.splitlines()[-1] if out else "",
        "message": "Omarchy's own installer is playing the exported tuning: the plain chain, "
                   "no deep bass, no compensation, no volume following, exactly what a user "
                   "of the tuning gets. The calibration is stopped; press Back to the "
                   "calibration to return.",
    }


def vendor_try_beside(rendered, export):
    """A second sink with the rendered chain, and the music moved onto it."""
    speaker = local_speaker()["name"]
    graph = trial_graph(rendered["chain"], speaker)
    if trial_service_active():
        # Re-rendering while a trial plays: bring the streams home first, so
        # the trial host's restart is not heard as a gap on them.
        move_apps(VIRTUAL_SINK if calibration_sink_present() else speaker)
        stop_trial_host()
    for path in (TRIAL_HOST, TRIAL_FRAGMENT, TRIAL_UNIT):
        secure_directory(path.parent)
    write_atomic(TRIAL_HOST, HOST_TEXT)
    write_atomic(TRIAL_FRAGMENT, graph)
    write_atomic(TRIAL_UNIT, TRIAL_UNIT_TEXT)
    run(["systemctl", "--user", "daemon-reload"], check=False, capture=True)
    run(["systemctl", "--user", "reset-failed", TRIAL_SERVICE], check=False, capture=True)
    run(["systemctl", "--user", "start", TRIAL_SERVICE], check=False, capture=True)
    for _ in range(40):
        if trial_sink_present():
            break
        time.sleep(0.25)
    else:
        stop_trial_host()
        raise SystemExit(
            "The exported tuning did not come up as a sink, so nothing was moved. "
            f"See: journalctl --user -u {TRIAL_SERVICE}"
        )
    time.sleep(0.3)
    move_apps(TRIAL_SINK)
    write_atomic(VENDOR_TRIAL, json.dumps({
        "mode": "beside", "slug": rendered["slug"], "directory": export["directory"],
        "since": dt.datetime.now(dt.timezone.utc).isoformat(),
    }) + "\n")
    return {
        "trial": True, "mode": "beside", "export": export,
        "message": "Your music now plays through the exported tuning, beside the calibration: "
                   "the plain chain with its built-in deep bass, no compensation, no volume "
                   "following. Nothing was restarted. Back to the calibration moves it back.",
    }


def vendor_restore():
    """Bring the music back to the calibration, whichever trial was playing."""
    if graph_kind() != "vendor-trial":
        # A trial beside the calibration: move the streams home and drop the
        # second host; the calibration never stopped.
        target = VIRTUAL_SINK if calibration_sink_present() else local_speaker()["name"]
        move_apps(target)
        stop_trial_host()
        try:
            VENDOR_TRIAL.unlink()
        except OSError:
            pass
        return {"trial": False, "message": "Back to the calibration; the exported tuning's sink is gone."}
    code, out, err = omarchy_audio_tuning("off")
    try:
        VENDOR_TRIAL.unlink()
    except OSError:
        pass
    profile = load_profile(PROFILE)
    if profile is None:
        return {"trial": False, "message": "Omarchy's tuning is off; there is no calibration to bring back."}
    method = reinstall_profile(profile)
    return {"trial": False, "method": method,
            "message": "Back to the calibration" + (" (tuning restarted)." if method == "restart" else ".")}


# ---- a new calibration waits for a decision ---------------------------------
# A measurement used to replace the calibration the moment it passed.  Now,
# when one already plays, the new one is installed for listening while the
# old one is held aside, and the panel asks: apply it, or keep the previous?
# Either answer is one live update.  The first calibration has nothing to
# compare against and installs as before.
HELD_PROFILE = DATA / "held-profile.json"
HELD_PREVIOUS = DATA / "held-previous.json"


def previewing():
    return HELD_PROFILE.exists()


def _forget_held():
    for path in (HELD_PROFILE, HELD_PREVIOUS):
        try:
            path.unlink()
        except OSError:
            pass


def preview_install(profile):
    """Play a new calibration without letting go of the one before it."""
    current = load_profile(PROFILE)
    if current is None:
        return install_now(profile)
    if not previewing():
        # A second measurement while one already waits keeps the original held
        # copies: the decision stays between the newest and what played before.
        write_atomic(HELD_PROFILE, json.dumps(current, indent=2) + "\n")
        previous = load_profile(PREVIOUS_PROFILE)
        if previous is not None:
            write_atomic(HELD_PREVIOUS, json.dumps(previous, indent=2) + "\n")
        else:
            try:
                HELD_PREVIOUS.unlink()
            except OSError:
                pass
    result = install_now(profile)
    result["previewing"] = True
    return result


def preview_if_accepted(profile):
    if profile.get("quality", {}).get("accepted") and profile.get("fit"):
        return preview_install(profile)
    return profile


def preview_apply():
    """Keep the new calibration; the previous stays under Switch profile."""
    if not previewing():
        raise SystemExit("No new calibration is waiting for a decision.")
    if compare_state()["active"] == "previous":
        compare_toggle()
    _forget_held()
    return {"previewing": False, "profile": load_profile(PROFILE),
            "message": "The new calibration is applied; the one before it stays available under Switch profile."}


def preview_discard():
    """Put the previous calibration back; the new one stays as the last measurement."""
    held = load_profile(HELD_PROFILE)
    if held is None:
        raise SystemExit("No new calibration is waiting for a decision.")
    held_previous = load_profile(HELD_PREVIOUS)
    write_atomic(PROFILE, json.dumps(held, indent=2) + "\n")
    if held_previous is not None:
        write_atomic(PREVIOUS_PROFILE, json.dumps(held_previous, indent=2) + "\n")
    else:
        try:
            PREVIOUS_PROFILE.unlink()
        except OSError:
            pass
    write_compare_state({"active": "current", "bypass": False})
    method = reinstall_profile(held)
    _forget_held()
    return {"previewing": False, "profile": held, "method": method,
            "message": "Kept the previous calibration. The new measurement stays as the last "
                       "measurement; Install last measurement applies it later if you change your mind."}


def cached_status():
    """The last status this plugin wrote, for drawing the panel immediately.

    Read here rather than in the panel: the panel is the shell, and a file at
    a predictable name under the data directory is something any process
    running as this user can replace with a symlink or a pipe.  Anything
    unreadable, oversized or not the shape written is simply absent, because
    a stale panel is better than a wrong one.
    """
    try:
        payload = json.loads(read_text_bounded(STATUS_CACHE) or "")
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) and payload.get("service") else {}


def status_payload():
    profile = load_profile(PROFILE)
    proposal = load_profile(PROPOSAL)
    active = run(["systemctl", "--user", "is-active", SERVICE], check=False, capture=True).stdout.strip()
    default = run(["pactl", "get-default-sink"], check=False, capture=True).stdout.strip()
    compare = compare_payload()
    payload = {"service": active or "inactive", "defaultSink": default or "unknown",
            # The fallback is a node name, which the device also chooses.
            "defaultSinkDescription": (short_label(sink_description(default))
                                       or short_label(default)
                                       or "the calibrated output"),
            "calibratedSink": VIRTUAL_SINK,
            "profile": profile, "proposal": proposal,
            "enabled": active == "active" and default == VIRTUAL_SINK and graph_kind() == "calibrator",
            # Whose graph the host is running, and whether a rendered tuning
            # is being heard through Omarchy's installer.
            "graph": graph_kind(),
            "vendorTrial": vendor_trial_active(),
            "vendorExport": vendor_export_status(),
            "bypass": compare["bypass"],
            "compare": compare,
            "verification": load_verification(),
            "bassEnhancer": harmonic_bass_status(),
            "deepBass": (profile or {}).get("deep_bass", "off"),
            "previewing": previewing(),
            "loudnessCompensation": (profile or {}).get("loudness_compensation", "off"),
            "loudnessTracker": "running" if loudness_running() else "stopped",
            "microphones": archived_microphones(),
            "hardware": hardware_id(),
            "sharedProfiles": shared_profiles(),
            "unusableMicrophones": [short_label(name) for name in unusable_microphones()],
            "measurementSupport": measurement_support()}
    try:
        write_atomic(STATUS_CACHE, json.dumps(payload) + "\n")
    except OSError:
        pass
    return payload


def choose_mic():
    all_mics = microphones()
    print("\nMicrophone type:\n  1. Built-in microphone\n  2. External/USB calibration microphone\n  3. Show all microphones")
    kind = input("Select 1-3: ").strip()
    if kind == "1":
        predicate = lambda item: item["name"].startswith("alsa_input.pci-")
    elif kind == "2":
        predicate = lambda item: item["name"].startswith("alsa_input.usb-")
    else:
        predicate = None
    return select(all_mics, "Recording microphone", predicate)


def wizard():
    print("Omarchy Speaker Calibrator\n===========================")
    print("This prioritizes cuts. Small boosts require a broad, reliable deficit and matching input headroom.")
    sink = select(physical_sinks(), "Physical speaker output")
    mic = choose_mic()
    channels = channel_count(mic)
    channel = 0
    if channels > 1:
        if mic["name"].startswith("alsa_input.pci-"):
            answer = input(
                f"Use [a]ll {channels} built-in microphones (recommended), "
                f"or choose 1-{channels}? [a]: "
            ).strip().lower()
            if answer.isdigit() and 1 <= int(answer) <= channels:
                channel = int(answer) - 1
            else:
                channel = "all"
        else:
            answer = input(f"Microphone channel 1-{channels} [1]: ").strip()
            if answer.isdigit() and 1 <= int(answer) <= channels:
                channel = int(answer) - 1
    print("\nVoicing:\n  Flat: balanced with more clarity; may sound a little brighter."
          "\n  Warm: softer and less sharp; comfortable for long listening.")
    answer = input("Choose [f]lat (recommended) or [w]arm? [f]: ").strip().lower()
    voicing = "warm" if answer.startswith("w") else "neutral"
    print("\nBass:\n  Normal: the measured correction only."
          "\n  Full: a +3 dB low shelf at the speaker's knee, paid for by input trim.")
    answer = input("Choose [n]ormal (recommended) or [f]ull? [n]: ").strip().lower()
    bass = "full" if answer.startswith("f") else "normal"
    print("\nLoudness:\n  Protected: cleanest; the cuts make the speaker a little quieter."
          "\n  Balanced: half of the lost loudness is added back."
          "\n  Matched: as loud as before; the limiter works harder at high volume.")
    answer = input("Choose [p]rotected (recommended), [b]alanced, or [m]atched? [p]: ").strip().lower()
    loudness = "matched" if answer.startswith("m") else (
        "balanced" if answer.startswith("b") else "protected"
    )
    mic_cal_file = None
    if not mic["name"].startswith("alsa_input.pci-"):
        mic_cal_file = input("Microphone calibration file (optional): ").strip() or None
    print("\nPlacement:")
    if mic["name"].startswith("alsa_input.pci-"):
        print("  Leave the laptop open on a hard surface and do not move it.")
    else:
        print("  Place the mic on-axis at normal listening distance, centered between speakers.")
    print("  Pause music/video, keep the room quiet, and use 50-70% hardware volume.")
    print("  A short level check runs first and sets the sweep level automatically.")
    input("Press Enter when ready. The level check and repeated left/right sweeps take about 30 seconds...")
    try:
        profile = build_profile(sink, mic, channel, voicing, mic_cal_file, loudness, bass)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    quality = profile["quality"]
    metrics = quality["metrics"]
    print(f"\nMeasurement quality: {quality['verdict'].upper()}")
    level_search = profile["measurement"].get("level_search")
    if level_search:
        print(f"  Sweep level: {level_search['selected_level_dbfs']:+.1f} dBFS "
              f"after {len(level_search['attempts'])} probe(s), {level_search['status']}")
    print(f"  Broadband prominence: {metrics['minimum_broadband_prominence_db']:.1f} dB")
    print(f"  Repeatability: {metrics['worst_repeatability_db']:.1f} dB")
    print(f"  Stable broad response: {metrics['minimum_stable_band_percent']:.0f}%")
    print(f"  Clock drift: {metrics['clock_drift_ppm']:+.0f} ppm")
    for issue in quality["failures"] + quality["warnings"]:
        print(f"  - {issue}")
    if not quality["accepted"]:
        for advice in quality["guidance"]:
            print(f"  Retry: {advice}")
        print("\nNo installable profile was generated.")
        return
    fit_payload = profile["fit"]
    gains = fit_payload["gains_db"]
    print("\nProposed protected filters:")
    for item in fit_payload["filters"]:
        print(f"  {item['type']:9s} {item['frequency_hz']:7.1f} Hz  Q {item['q']:4.2f}  "
              f"{item['gain_db']:6.2f} dB")
    highpass = fit_payload.get("highpass") or {}
    if highpass:
        knee = highpass.get("knee_hz")
        print(f"  High-pass: {highpass['frequency_hz']:.0f} Hz, "
              f"{'2nd' if highpass['stages'] == 1 else '4th'} order"
              + (f" (measured knee {knee:.0f} Hz)" if knee else " (no knee found)"))
    if fit_payload.get("bass_shelf"):
        shelf = fit_payload["bass_shelf"]
        print(f"  Bass shelf: {shelf['gain_db']:+.1f} dB below {shelf['frequency_hz']:.0f} Hz")
    print(f"  Input headroom: {fit_payload['headroom_db']:.2f} dB")
    print(f"  Loudness lost to cuts: {fit_payload['loudness_loss_db']:.1f} dB; "
          f"make-up {fit_payload['makeup_db']:+.1f} dB ({fit_payload['loudness_mode']})")
    print(f"  Target-fit error: {fit_payload['weighted_rmse_before_db']:.2f} → "
          f"{fit_payload['weighted_rmse_after_db']:.2f} dB")
    print(f"  Background level: {metrics['background_dbfs']:.1f} dBFS")
    if input("Install and enable this profile? [Y/n]: ").strip().lower() not in ("n", "no"):
        install_profile(profile, filter_config(sink["name"], fit_payload))
        print("\nProfile installed. The calibrated sink is now the default output.")
    else:
        print("Profile was measured but not installed.")


def status():
    payload = status_payload()
    profile = payload["profile"]
    print(f"Service: {payload['service']}\nDefault output: {payload['defaultSink']}")
    check = payload.get("verification")
    if check:
        errors = check["target_error_db"]
        print(f"Last check: {check['verdict'].upper()}"
              + (" (stale, the profile changed since)" if check.get("stale") else "")
              + f" · off plan by {check['model_error_db']['rms']:.1f} dB"
              + f" · target error {errors['before']:.1f} → {errors['measured']:.1f} dB")
    compare = payload["compare"]
    if payload.get("bypass"):
        print("Calibration is switched off (bypassed); run 'bypass-toggle' to switch it on.")
    if compare["available"]:
        playing = compare[compare["active"]]
        print(f"Playing: {compare['active']} profile"
              + (f" ({playing['label']})" if playing else "")
              + " · run 'compare-toggle' to hear the other one")
    if profile:
        print(f"Speaker: {profile['speaker']['description']}\nMicrophone: {profile['microphone']['description']}")
        gains = profile.get("fit", {}).get("gains_db") if profile.get("fit") else None
        voicing = VOICING_LABELS.get(profile.get("voicing"), "neutral")
        loudness = LOUDNESS_LABELS.get(profile.get("loudness"), "protected")
        bass = BASS_LABELS.get(profile.get("bass"), "normal bass")
        print(f"Voicing: {voicing}\nBass: {bass}\nLoudness: {loudness}\n"
              f"Gains: {gains or 'not installable'}")
    else:
        print("No saved calibration profile.")


def disable():
    profile = load_profile(PROFILE)
    target = profile["speaker"]["name"] if profile else None
    if target:
        move_apps(target)
    forget_loudness_tracker()
    run(["systemctl", "--user", "disable", "--now", SERVICE], check=False)
    print("Speaker calibration disabled." + (f" Output restored to {target}." if target else ""))


def exit_on_terminate(signum, frame):
    """Turn the panel's SIGTERM into a normal exit, so every finally block runs.

    Python's default is to die at once, which left the recorder and the playback
    running with the microphone still held (issue #1).
    """
    raise SystemExit(128 + signum)


def main():
    signal.signal(signal.SIGTERM, exit_on_terminate)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    for name in ("wizard", "status", "status-json", "status-cache-json",
                 "microphone-comparison-json", "use-calibrated-output",
                 "devices-json", "install-proposal",
                 "disable", "compare-toggle", "bypass-toggle"):
        sub.add_parser(name)
    for name in ("deep-bass-toggle",
                 "install-measurement-support", "loudness-toggle"):
        sub.add_parser(name)
    sub.add_parser("verify-json")
    sub.add_parser("export-json")
    sub.add_parser("preview-apply-json")
    sub.add_parser("preview-discard-json")
    vendor = sub.add_parser("vendor-tuning-json")
    vendor.add_argument("--reference", help="a track to run through the chain for the headroom and LRA figures")
    trial = sub.add_parser("vendor-try-json")
    trial.add_argument("--reference")
    trial.add_argument("--installer", action="store_true",
                       help="through Omarchy's own installer instead of beside the calibration (restarts the tuning host)")
    sub.add_parser("vendor-restore-json")
    relevel_parser = sub.add_parser("relevel-json")
    relevel_parser.add_argument("--bass", choices=("normal", "full"))
    relevel_parser.add_argument("--loudness", choices=("protected", "balanced", "matched"))
    imported = sub.add_parser("import-json")
    imported.add_argument("--file", help="a shared file in the Downloads folder, by name")
    imported.add_argument("--path", help="a shared file anywhere, for use from a terminal")
    refine = sub.add_parser("refine-json")
    refine.add_argument("--install", action="store_true",
                        help="install and play the improved profile")
    calibrate = sub.add_parser("calibrate-json")
    calibrate.add_argument("--sink", required=True)
    calibrate.add_argument("--mic", required=True)
    calibrate.add_argument("--channel", default="0")
    calibrate.add_argument("--voicing", choices=("warm", "neutral"), default="neutral")
    calibrate.add_argument("--loudness", choices=("protected", "balanced", "matched"),
                           default="protected")
    calibrate.add_argument("--bass", choices=("normal", "full"), default="normal")
    calibrate.add_argument("--channel-trim", choices=("off", "auto"), default="off")
    calibrate.add_argument("--mic-cal-file")
    calibrate.add_argument("--preview", action="store_true",
                           help="play the result and wait for a decision when a calibration already exists")
    calibrate.add_argument("--install", action="store_true",
                           help="install and play the result when it passes")
    reanalyze = sub.add_parser("reanalyze-saved-json")
    reanalyze.add_argument("--voicing", choices=("warm", "neutral"))
    reanalyze.add_argument("--loudness", choices=("protected", "balanced", "matched"))
    reanalyze.add_argument("--bass", choices=("normal", "full"))
    reanalyze.add_argument("--channel-trim", choices=("off", "auto"))
    reanalyze.add_argument("--channel")
    reanalyze.add_argument("--install", action="store_true",
                           help="install and play the result when it passes")
    args = parser.parse_args()
    command = args.command or "wizard"
    if command == "devices-json":
        print(json.dumps(devices_payload()))
    elif command == "status-json":
        print(json.dumps(status_payload()))
    elif command == "status-cache-json":
        print(json.dumps(cached_status()))
    elif command == "use-calibrated-output":
        print(json.dumps(use_calibrated_output()))
    elif command == "microphone-comparison-json":
        print(json.dumps(microphone_comparison()))
    elif command == "calibrate-json":
        profile = calibrate_noninteractive(
            args.sink, args.mic, args.channel, args.voicing, args.mic_cal_file,
            args.loudness, args.bass, args.channel_trim,
        )
        if args.install:
            profile = install_if_accepted(profile)
        elif args.preview:
            profile = preview_if_accepted(profile)
        print(json.dumps(profile))
    elif command == "reanalyze-saved-json":
        profile = reanalyze_saved_capture(
            args.voicing, args.channel, args.loudness, args.bass, args.channel_trim
        )
        print(json.dumps(install_if_accepted(profile) if args.install else profile))
    elif command == "install-proposal":
        print(json.dumps(install_proposal()))
    elif command == "compare-toggle":
        print(json.dumps(compare_toggle()))
    elif command == "bypass-toggle":
        print(json.dumps(bypass_toggle()))
    elif command == "verify-json":
        print(json.dumps(verify_calibration()))
    elif command == "relevel-json":
        print(json.dumps(relevel(bass=args.bass, loudness=args.loudness)))
    elif command == "vendor-try-json":
        print(json.dumps(vendor_try(reference=args.reference, installer=args.installer)))
    elif command == "vendor-restore-json":
        print(json.dumps(vendor_restore()))
    elif command == "vendor-tuning-json":
        print(json.dumps(vendor_tuning(reference=args.reference)))
    elif command == "preview-apply-json":
        print(json.dumps(preview_apply()))
    elif command == "preview-discard-json":
        print(json.dumps(preview_discard()))
    elif command == "export-json":
        print(json.dumps(export_profile()))
    elif command == "import-json":
        print(json.dumps(import_profile(name=args.file, path=args.path)))
    elif command == "deep-bass-toggle":
        print(json.dumps(deep_bass_toggle()))
    elif command == "loudness-toggle":
        print(json.dumps(loudness_toggle()))
    elif command == "install-measurement-support":
        print(json.dumps(install_measurement_support()))
    elif command == "refine-json":
        profile = refine_from_check()
        print(json.dumps(install_if_accepted(profile) if args.install else profile))
    else:
        {"wizard": wizard, "status": status, "disable": disable}[command]()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130)
