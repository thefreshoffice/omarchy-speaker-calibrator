#!/usr/bin/python3
"""Guided, measurement-gated PipeWire speaker calibration for Omarchy."""

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from calibration_dsp import (
    LEVEL_SEARCH_ABORT_STATUSES,
    SweepSpec,
    analyse_capture,
    analyse_level_probe,
    build_measurement_signal,
    combine_microphone_measurements,
    level_after_clipping,
    level_search_advice,
    parse_mic_calibration,
    read_pcm16_wave_channels,
    search_measurement_level,
    write_pcm16_wave,
)
from calibration_optimizer import (
    apply_refinement,
    optimize_peq,
    refinement_residual,
    verification_report,
)

SWEEP_SPEC = SweepSpec()
RATE = SWEEP_SPEC.rate
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
VERIFICATION_SWEEPS = DATA / "verification-sweeps.wav"
VERIFICATION_RECORDING = DATA / "verification.wav"
SERVICE = "omarchy-speaker-tuning.service"
VIRTUAL_SINK = "omarchy_speaker_tuning"

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
        manifest = json.loads((Path(__file__).resolve().parent / "manifest.json").read_text())
        return str(manifest.get("version", "unknown"))
    except (OSError, ValueError):
        return "unknown"


def run(args, *, check=True, capture=False):
    return subprocess.run(args, check=check, text=True,
                          capture_output=capture)


def pactl_json(kind):
    proc = run(["pactl", "-f", "json", "list", kind], capture=True)
    return json.loads(proc.stdout)


def bass_enhancer_status():
    """Whether the psychoacoustic bass add-on is installed and usable."""
    for base in BASS_ENHANCER_SEARCH_PATHS:
        directory = Path(base)
        if not directory.is_dir():
            continue
        for bundle in sorted(directory.iterdir()):
            if not bundle.is_dir():
                continue
            text = ""
            for turtle in sorted(bundle.glob("*.ttl")):
                try:
                    text += turtle.read_text(errors="ignore")
                except OSError:
                    continue
            if BASS_ENHANCER_URI not in text:
                continue
            missing = [
                port for port in BASS_ENHANCER_PORTS
                if f'lv2:symbol "{port}"' not in text
            ]
            return {
                "available": not missing,
                "installed": True,
                "usable": not missing,
                "package": BASS_ENHANCER_PACKAGE,
                "path": str(bundle),
                "missing_ports": missing,
            }
    return {
        "available": False,
        "installed": False,
        "usable": False,
        "package": BASS_ENHANCER_PACKAGE,
        "path": None,
        "missing_ports": [],
    }


def package_repository(package):
    """The configured repository holding this package, or None for AUR-only."""
    result = run(["pacman", "-Si", package], check=False, capture=True)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.lower().startswith("repository"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def bass_enhancer_install_command():
    """How to install the add-on: a plain package when a repository has it.

    Omarchy's own repository may carry it one day, and a signed repository
    package is preferable to a source build, so the repository is asked first
    and the answer decides the command.  Nothing here needs changing if it
    later appears there.
    """
    repository = package_repository(BASS_ENHANCER_PACKAGE)
    if repository:
        return f"omarchy pkg add {BASS_ENHANCER_PACKAGE}", repository
    return f"omarchy pkg aur add {BASS_ENHANCER_PACKAGE}", None


def install_bass_enhancer():
    """Start the add-on's installation in a terminal the user can watch."""
    status = bass_enhancer_status()
    if status["installed"]:
        return {**status, "started": False,
                "message": "The bass add-on is already installed."}
    command, repository = bass_enhancer_install_command()
    started = run(
        ["omarchy", "launch", "floating", "terminal", "with", "presentation", command],
        check=False,
    ).returncode == 0
    if not started:
        raise SystemExit(
            "Could not open a terminal for the installation. Run this yourself:\n"
            f"  {command}"
        )
    return {
        **status,
        "started": True,
        "command": command,
        "source": repository or "AUR",
        "message": (
            f"Installing from the {repository} repository in a terminal window. "
            "When it finishes, switch Deep bass on again."
            if repository else
            "Installing from the AUR, which is not curated by Omarchy and builds from "
            "source, in a terminal window. Read what it does before agreeing. When it "
            "finishes, switch Deep bass on again."
        ),
    }


def is_physical_sink(name):
    """True for a real output device, never the calibrated sink in front of one."""
    return str(name).startswith("alsa_output.") and str(name) != VIRTUAL_SINK


def physical_sinks():
    """Only real outputs are offered, so a calibration cannot measure itself."""
    return [item for item in pactl_json("sinks") if is_physical_sink(item.get("name", ""))]


def microphones():
    return [item for item in pactl_json("sources")
            if not item.get("name", "").endswith(".monitor")]


def channel_count(item):
    spec = item.get("sample_specification") or item.get("sample_spec") or ""
    found = re.search(r"(\d+)ch", str(spec))
    return int(found.group(1)) if found else 1


def devices_payload():
    def public(item, kind):
        name = item["name"]
        return {
            "name": name,
            "description": label(item),
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
# the ear supplies the fundamental it never heard.  It is a separate package,
# so the plugin works without it and only ever asks.
BASS_ENHANCER_URI = "https://chadmed.au/bankstown"
BASS_ENHANCER_PACKAGE = "bankstown"
BASS_ENHANCER_SEARCH_PATHS = (
    "/usr/lib/lv2", "/usr/local/lib/lv2", str(Path.home() / ".lv2"),
)
# Every port the generated graph refers to.  If the installed build does not
# have all of them it is not the plugin this was written against, and it is
# left out rather than risking a filter chain that will not load.
BASS_ENHANCER_PORTS = (
    "in_l", "in_r", "out_l", "out_r",
    "bypass", "amt", "floor", "ceil", "final_hp", "sat_second", "sat_third", "blend",
)
# Settings from the Asahi Linux MacBook tunings, which use the same plugin on
# speakers of much the same size.  The two frequency limits are not fixed:
# they follow the measured knee, so the harmonics land where this speaker can
# actually play them.  The plugin clamps them to 250 Hz.
BASS_ENHANCER_AMOUNT = 1.45
BASS_ENHANCER_SECOND = 1.3
BASS_ENHANCER_THIRD = 1.75
BASS_ENHANCER_BLEND = 1.0
BASS_ENHANCER_FLOOR_HZ = 20.0
BASS_ENHANCER_MAX_HZ = 250.0

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
}


def graph_sections():
    """Section names per channel, in signal order.

    ``ls`` and ``hs`` are the optimizer's shelves; ``bs`` is the full-bass
    option's shelf, kept separate so both can be present at once.
    """
    return (
        ["hp1", "hp2", "ls", "bs"]
        + [f"p{slot}" for slot in range(1, PEAKING_SLOTS + 1)]
        + ["hs"]
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


def bass_enhancer_controls(corner_hz, deep_bass):
    """Controls for the bass add-on, tuned to where this speaker gives up."""
    limit = float(min(BASS_ENHANCER_MAX_HZ, max(10.0, corner_hz)))
    return {
        "bass:bypass": 0.0 if deep_bass else 1.0,
        "bass:amt": BASS_ENHANCER_AMOUNT if deep_bass else 0.0,
        "bass:floor": BASS_ENHANCER_FLOOR_HZ,
        # Harmonics are made from what lies below the knee and kept above it,
        # which is the only place the speaker can reproduce them.
        "bass:ceil": limit,
        "bass:final_hp": limit,
        "bass:sat_second": BASS_ENHANCER_SECOND,
        "bass:sat_third": BASS_ENHANCER_THIRD,
        "bass:blend": BASS_ENHANCER_BLEND,
    }


def graph_controls(fit_payload, *, bass_enhancer=None, deep_bass=False):
    """Every control of the fixed-shape graph, for both channels, in order."""
    if bass_enhancer is None:
        bass_enhancer = bass_enhancer_status()["usable"]
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
    if bass_enhancer:
        controls.update(bass_enhancer_controls(corner, deep_bass))
    controls["limiter:g_in"] = float(fit_payload["input_gain_linear"])
    return controls


def _number(value):
    return f"{float(value):.4f}".rstrip("0").rstrip(".") or "0"


def filter_config(sink, fit_payload, *, bass_enhancer=None, deep_bass=False):
    if bass_enhancer is None:
        bass_enhancer = bass_enhancer_status()["usable"]
    controls = graph_controls(
        fit_payload, bass_enhancer=bass_enhancer, deep_bass=deep_bass
    )
    nodes, links, inputs, outputs = [], [], [], []
    for side, port in (("l", "l"), ("r", "r")):
        chain = []
        for section in graph_sections():
            name = f"{section}_{side}"
            kind = section.rstrip("0123456789")
            label = SECTION_LABELS[kind]
            settings = f'"Freq" = {_number(controls[f"{name}:Freq"])} "Q" = {_number(controls[f"{name}:Q"])}'
            if kind != "hp":
                settings += f' "Gain" = {_number(controls[f"{name}:Gain"])}'
            nodes.append(
                f'{{ type = builtin name = {name} label = {label} control = {{ {settings} }} }}'
            )
            chain.append(name)
        if bass_enhancer:
            # The add-on has to see the low notes before the high-pass takes
            # them away, so it comes first and feeds each channel's chain.
            links.append(f'{{ output = "bass:out_{port}" input = "{chain[0]}:In" }}')
            inputs.append(f'"bass:in_{port}"')
        else:
            inputs.append(f'"{chain[0]}:In"')
        for before, after in zip(chain, chain[1:]):
            links.append(f'{{ output = "{before}:Out" input = "{after}:In" }}')
        links.append(f'{{ output = "{chain[-1]}:Out" input = "limiter:in_{port}" }}')
        outputs.append(f'"limiter:out_{port}"')
    if bass_enhancer:
        settings = " ".join(
            f'"{name.split(":", 1)[1]}" = {_number(value)}'
            for name, value in bass_enhancer_controls(
                controls["bass:ceil"], controls["bass:bypass"] < 0.5
            ).items()
        )
        nodes.insert(0, f'''{{ type = lv2 name = bass
      plugin = "{BASS_ENHANCER_URI}"
      control = {{ {settings} }}
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
    node.description = "Calibrated Speakers — Protected"
    media.name = "Calibrated Speakers — Protected"
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
    capture.props = {{ node.name = "{VIRTUAL_SINK}" media.class = Audio/Sink }}
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
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def move_apps(target):
    run(["pactl", "set-default-sink", target])
    for stream in pactl_json("sink-inputs"):
        props = stream.get("properties", {})
        if props.get("application.name") and props.get("application.name") != "EasyEffects":
            run(["pactl", "move-sink-input", str(stream["index"]), target], check=False)


def compare_state():
    try:
        state = json.loads(COMPARE_STATE.read_text())
    except (OSError, ValueError):
        state = {}
    if state.get("active") not in ("current", "previous"):
        state["active"] = "current"
    state["bypass"] = bool(state.get("bypass", False))
    return state


def write_compare_state(state):
    DATA.mkdir(parents=True, exist_ok=True)
    COMPARE_STATE.write_text(json.dumps(state) + "\n")


def transparent_controls(bass_enhancer=None):
    """Controls that make the running graph pass audio through unchanged.

    The high-pass sections drop to 10 Hz, every gain goes to 0 dB, the bass
    add-on is bypassed, and the limiter input gain returns to unity; only the
    -1 dBFS ceiling remains.
    """
    controls = graph_controls(
        {"filters": [], "input_gain_linear": 1.0},
        bass_enhancer=bass_enhancer, deep_bass=False,
    )
    for name in list(controls):
        if name.startswith("hp") and name.endswith(":Freq"):
            controls[name] = 10.0
    return controls


def profile_summary(path):
    """A short label for a saved profile, or None when there is none."""
    try:
        profile = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    fit = profile.get("fit") or {}
    created = str(profile.get("created_at", ""))[:16].replace("T", " ")
    return {
        "created_at": profile.get("created_at"),
        "label": f"{created} · {fit.get('filter_count', 0)} filters · "
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


def apply_controls_live(controls):
    """Update the running filter in place; False when it must be restarted."""
    node_id = tuning_node_id()
    if node_id is None:
        return False
    current = live_controls(node_id)
    if any(name not in current for name in controls):
        # The running graph has a different shape (an older profile); only a
        # restart can load the new one.
        return False
    payload = " ".join(f'"{name}" {float(value):.6f}' for name, value in controls.items())
    result = run(
        ["pw-cli", "set-param", str(node_id), "Props", f"{{ params = [ {payload} ] }}"],
        check=False, capture=True,
    )
    if result.returncode != 0:
        return False
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
    enhancer = bass_enhancer_status()["usable"]
    deep_bass = profile.get("deep_bass") == "on" and enhancer
    controls = graph_controls(fit, bass_enhancer=enhancer, deep_bass=deep_bass)
    FRAGMENT.parent.mkdir(parents=True, exist_ok=True)
    FRAGMENT.write_text(filter_config(
        profile["speaker"]["name"], fit, bass_enhancer=enhancer, deep_bass=deep_bass
    ))
    state = compare_state()
    if state["bypass"]:
        state["bypass"] = False
        write_compare_state(state)
    if service_active() and apply_controls_live(controls):
        run(["systemctl", "--user", "daemon-reload"], check=False)
        return "live"
    restart_tuning()
    return "restart"


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


def bypass_toggle():
    """Switch the running graph between unity and the playing profile, live."""
    state = compare_state()
    profile = playing_profile()
    if profile is None:
        raise SystemExit("No calibration is installed, so there is nothing to switch off.")
    if not state["bypass"]:
        method = "live"
        if not (service_active() and apply_controls_live(transparent_controls())):
            # The running graph has an older shape, so its controls cannot be
            # zeroed by name.  Activating the profile regenerates the graph in
            # the current shape (restarting once); then unity can be applied.
            method = activate_profile(profile)
            if not apply_controls_live(transparent_controls()):
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
    if not Path("/usr/lib/lv2/lsp-plugins.lv2/limiter_stereo.ttl").exists():
        raise SystemExit("Missing lsp-plugins-lv2. Install it with: omarchy pkg add lsp-plugins-lv2")
    DATA.mkdir(parents=True, exist_ok=True)
    keep_previous_profile()
    for path in (HOST, FRAGMENT, UNIT):
        backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
    HOST.write_text(HOST_TEXT)
    FRAGMENT.write_text(graph)
    UNIT.write_text(UNIT_TEXT)
    PROFILE.write_text(json.dumps(profile, indent=2) + "\n")
    return activate_profile(profile)


def load_profile(path):
    try:
        profile = json.loads(path.read_text())
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
    return SWEEP_SPEC.level_dbfs


def record_while_playing(
    sink_name, mic_name, channels, program, recording, lead_seconds, tail_seconds
):
    """Record the microphone while a program plays on the selected sink."""
    recorder = subprocess.Popen([
        "pw-record", f"--target={mic_name}", f"--rate={RATE}",
        f"--channels={channels}", "--format=s16", str(recording)])
    try:
        time.sleep(lead_seconds)
        run(["pw-play", f"--target={sink_name}", str(program)])
        time.sleep(tail_seconds)
    finally:
        recorder.send_signal(signal.SIGINT)
        recorder.wait(timeout=5)


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
    """Find a safe level, play the Phase 1 program, capture it, and analyze it.

    ``level_sink`` names the output the level default should be taken from,
    which differs from the played sink when measuring through the calibrated
    sink that fronts it.
    """
    channels = next(
        (channel_count(item) for item in microphones() if item["name"] == mic_name), 1
    )
    DATA.mkdir(parents=True, exist_ok=True)
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


def profile_from_measurement(
    sink, mic, channel, voicing, measurement, loudness="protected", bass="normal"
):
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
        # Carried across refits so switching voicing does not lose the add-on.
        "deep_bass": (load_profile(PROFILE) or {}).get("deep_bass", "off"),
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
    PROPOSAL.write_text(json.dumps(profile, indent=2) + "\n")
    return profile


def build_profile(
    sink, mic, channel, voicing, mic_cal_file=None, loudness="protected", bass="normal"
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
        sink, mic, channel, voicing, measurement, loudness, bass
    )


def reanalyze_saved_capture(voicing=None, channel_override=None, loudness=None, bass=None):
    """Re-run current analysis and optimization on the last capture, without sound."""
    recording = DATA / "measurement.wav"
    if not PROPOSAL.exists() or not recording.exists():
        raise SystemExit("No saved capture and proposal are available to reanalyze.")
    previous = json.loads(PROPOSAL.read_text())
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
    bass="normal",
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
        return build_profile(sink, mic, channel, voicing, mic_cal_file, loudness, bass)
    except ValueError as error:
        raise SystemExit(str(error)) from error


def install_proposal():
    if not PROPOSAL.exists():
        raise SystemExit("No measured proposal is available.")
    profile = json.loads(PROPOSAL.read_text())
    return install_now(profile)


def install_now(profile):
    if not profile.get("quality", {}).get("accepted") or not profile.get("fit"):
        raise SystemExit("This measurement failed its quality checks and cannot be installed.")
    enhancer = bass_enhancer_status()["usable"]
    profile["activation"] = install_profile(
        profile,
        filter_config(
            profile["speaker"]["name"], profile["fit"], bass_enhancer=enhancer,
            deep_bass=profile.get("deep_bass") == "on" and enhancer,
        ),
    )
    profile["installed"] = True
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
        report = json.loads(VERIFICATION.read_text())
    except (OSError, ValueError):
        return None
    profile = load_profile(PROFILE)
    created = profile.get("created_at") if profile else None
    report["stale"] = report.get("profile_created_at") != created
    return report


def deep_bass_toggle():
    """Switch the bass add-on on or off, live, without refitting."""
    status = bass_enhancer_status()
    if not status["usable"]:
        if status["installed"]:
            raise SystemExit(
                "The installed bass add-on is missing controls this expects "
                f"({', '.join(status['missing_ports'])}), so it was left out."
            )
        return install_bass_enhancer()
    profile = load_profile(PROFILE)
    if profile is None:
        raise SystemExit("Calibrate the speakers first; there is nothing to add bass to.")
    wanted = "off" if profile.get("deep_bass") == "on" else "on"
    profile["deep_bass"] = wanted
    PROFILE.write_text(json.dumps(profile, indent=2) + "\n")
    method = activate_profile(profile)
    return {**status, "started": False, "deep_bass": wanted, "method": method,
            "message": ("Deep bass on" if wanted == "on" else "Deep bass off")}


def refine_from_check():
    """Fold what the check measured back into the raw estimate and fit again."""
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
    )


def verify_calibration(channel_override=None):
    """Measure through the corrected output and compare it with the fit."""
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
            f"The microphone used for this calibration ({label(mic)}) is not connected."
        )
    channel = parse_channel_selection(
        channel_override if channel_override is not None else mic.get("channel", 0)
    )
    calibration_file = mic.get("calibration_file")
    # The one measurement that is deliberately made through the correction.
    measurement = capture_measurement(
        VIRTUAL_SINK, mic["name"], channel, calibration_file,
        # The level default belongs to the real speakers behind the filter.
        level_sink=profile["speaker"]["name"],
        sweeps=VERIFICATION_SWEEPS, recording=VERIFICATION_RECORDING,
    )
    measurement["measured_through"] = {"sink": VIRTUAL_SINK, "corrected": True}
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
    DATA.mkdir(parents=True, exist_ok=True)
    VERIFICATION.write_text(json.dumps(report, indent=2) + "\n")
    return report


def status_payload():
    profile = json.loads(PROFILE.read_text()) if PROFILE.exists() else None
    proposal = json.loads(PROPOSAL.read_text()) if PROPOSAL.exists() else None
    active = run(["systemctl", "--user", "is-active", SERVICE], check=False, capture=True).stdout.strip()
    default = run(["pactl", "get-default-sink"], check=False, capture=True).stdout.strip()
    compare = compare_payload()
    return {"service": active or "inactive", "defaultSink": default or "unknown",
            "profile": profile, "proposal": proposal,
            "enabled": active == "active" and default == VIRTUAL_SINK,
            "bypass": compare["bypass"],
            "compare": compare,
            "verification": load_verification(),
            "bassEnhancer": {**bass_enhancer_status(),
                             "source": package_repository(BASS_ENHANCER_PACKAGE) or "AUR"},
            "deepBass": (profile or {}).get("deep_bass", "off")}


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
    profile = json.loads(PROFILE.read_text()) if PROFILE.exists() else None
    target = profile["speaker"]["name"] if profile else None
    if target:
        move_apps(target)
    run(["systemctl", "--user", "disable", "--now", SERVICE], check=False)
    print("Speaker calibration disabled." + (f" Output restored to {target}." if target else ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    for name in ("wizard", "status", "status-json", "devices-json", "install-proposal",
                 "disable", "compare-toggle", "bypass-toggle"):
        sub.add_parser(name)
    for name in ("deep-bass-toggle", "install-bass-enhancer"):
        sub.add_parser(name)
    verify = sub.add_parser("verify-json")
    verify.add_argument("--channel")
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
    calibrate.add_argument("--mic-cal-file")
    calibrate.add_argument("--install", action="store_true",
                           help="install and play the result when it passes")
    reanalyze = sub.add_parser("reanalyze-saved-json")
    reanalyze.add_argument("--voicing", choices=("warm", "neutral"))
    reanalyze.add_argument("--loudness", choices=("protected", "balanced", "matched"))
    reanalyze.add_argument("--bass", choices=("normal", "full"))
    reanalyze.add_argument("--channel")
    reanalyze.add_argument("--install", action="store_true",
                           help="install and play the result when it passes")
    args = parser.parse_args()
    command = args.command or "wizard"
    if command == "devices-json":
        print(json.dumps(devices_payload()))
    elif command == "status-json":
        print(json.dumps(status_payload()))
    elif command == "calibrate-json":
        profile = calibrate_noninteractive(
            args.sink, args.mic, args.channel, args.voicing, args.mic_cal_file,
            args.loudness, args.bass,
        )
        print(json.dumps(install_if_accepted(profile) if args.install else profile))
    elif command == "reanalyze-saved-json":
        profile = reanalyze_saved_capture(
            args.voicing, args.channel, args.loudness, args.bass
        )
        print(json.dumps(install_if_accepted(profile) if args.install else profile))
    elif command == "install-proposal":
        print(json.dumps(install_proposal()))
    elif command == "compare-toggle":
        print(json.dumps(compare_toggle()))
    elif command == "bypass-toggle":
        print(json.dumps(bypass_toggle()))
    elif command == "verify-json":
        print(json.dumps(verify_calibration(args.channel)))
    elif command == "deep-bass-toggle":
        print(json.dumps(deep_bass_toggle()))
    elif command == "install-bass-enhancer":
        print(json.dumps(install_bass_enhancer()))
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
