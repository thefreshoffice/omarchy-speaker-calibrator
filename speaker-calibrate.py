#!/usr/bin/python3
"""Guided, measurement-gated PipeWire speaker calibration for Omarchy."""

import argparse
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

from calibration_dsp import (
    LEVEL_SEARCH_ABORT_STATUSES,
    SweepSpec,
    analyse_capture,
    analyse_level_probe,
    build_measurement_signal,
    combine_microphone_measurements,
    level_search_advice,
    parse_mic_calibration,
    read_pcm16_wave_channels,
    search_measurement_level,
    write_pcm16_wave,
)
from calibration_optimizer import optimize_peq

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
LEVEL_SEARCH_ATTEMPTS = 3
LEVEL_SEARCH_START_OFFSET_DB = -12.0
LEVEL_SEARCH_BOUNDS_DB = (-24.0, 6.0)

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "omarchy-speaker-calibrator"
HOST = CONFIG / "pipewire/omarchy-speaker-tuning.conf"
FRAGMENT = CONFIG / "pipewire/omarchy-speaker-tuning.conf.d/90-tuning.conf"
UNIT = CONFIG / "systemd/user/omarchy-speaker-tuning.service"
PROFILE = DATA / "active-profile.json"
PROPOSAL = DATA / "proposed-profile.json"
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


def run(args, *, check=True, capture=False):
    return subprocess.run(args, check=check, text=True,
                          capture_output=capture)


def pactl_json(kind):
    proc = run(["pactl", "-f", "json", "list", kind], capture=True)
    return json.loads(proc.stdout)


def physical_sinks():
    return [item for item in pactl_json("sinks")
            if item.get("name", "").startswith("alsa_output.")]


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


def filter_config(sink, fit_payload):
    centers = fit_payload["centers_hz"]
    q_values = fit_payload["q"]
    gains = fit_payload["gains_db"]
    input_gain = float(fit_payload["input_gain_linear"])
    nodes, links, inputs, outputs = [], [], [], []
    for side, port in (("l", "l"), ("r", "r")):
        chain = []
        for index in (1, 2):
            name = f"hp{index}_{side}"
            nodes.append(f'{{ type = builtin name = {name} label = bq_highpass control = {{ "Freq" = 55.0 "Q" = 0.707 }} }}')
            chain.append(name)
        for filter_index, (center, q, gain) in enumerate(
            zip(centers, q_values, gains), 1
        ):
            name = f"p{filter_index}_{side}"
            frequency_text = f"{float(center):.3f}".rstrip("0").rstrip(".")
            q_text = f"{float(q):.4f}".rstrip("0").rstrip(".")
            gain_text = f"{float(gain):.3f}".rstrip("0").rstrip(".")
            nodes.append(
                f'{{ type = builtin name = {name} label = bq_peaking control = '
                f'{{ "Freq" = {frequency_text} "Q" = {q_text} "Gain" = {gain_text} }} }}'
            )
            chain.append(name)
        inputs.append(f'"{chain[0]}:In"')
        for before, after in zip(chain, chain[1:]):
            links.append(f'{{ output = "{before}:Out" input = "{after}:In" }}')
        links.append(f'{{ output = "{chain[-1]}:Out" input = "limiter:in_{port}" }}')
        outputs.append(f'"limiter:out_{port}"')
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


def install_profile(profile, graph):
    if not Path("/usr/lib/lv2/lsp-plugins.lv2/limiter_stereo.ttl").exists():
        raise SystemExit("Missing lsp-plugins-lv2. Install it with: omarchy pkg add lsp-plugins-lv2")
    for path in (HOST, FRAGMENT, UNIT):
        backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
    HOST.write_text(HOST_TEXT)
    FRAGMENT.write_text(graph)
    UNIT.write_text(UNIT_TEXT)
    DATA.mkdir(parents=True, exist_ok=True)
    PROFILE.write_text(json.dumps(profile, indent=2) + "\n")
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", SERVICE])
    run(["systemctl", "--user", "restart", SERVICE])
    for _ in range(30):
        if any(item.get("name") == VIRTUAL_SINK for item in pactl_json("sinks")):
            move_apps(VIRTUAL_SINK)
            return
        time.sleep(0.25)
    raise SystemExit("The tuning sink did not appear; inspect the user service status.")


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


def find_measurement_level(sink_name, mic_name, channel, channels):
    """Probe the speaker/microphone pair and choose the sweep level."""
    default_level = default_sweep_level(sink_name)
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


def capture_measurement(sink_name, mic_name, channel, mic_cal_file=None):
    """Find a safe level, play the Phase 1 program, capture it, and analyze it."""
    channels = next(
        (channel_count(item) for item in microphones() if item["name"] == mic_name), 1
    )
    DATA.mkdir(parents=True, exist_ok=True)
    level_search = find_measurement_level(sink_name, mic_name, channel, channels)
    sweeps = DATA / "calibration-sweeps.wav"
    recording = DATA / "measurement.wav"
    measurement_spec = SweepSpec(level_dbfs=level_search["selected_level_dbfs"])
    program, schedule = build_measurement_signal(measurement_spec)
    write_pcm16_wave(sweeps, program, RATE)
    record_while_playing(
        sink_name, mic_name, channels, sweeps, recording, RECORD_LEAD_SECONDS, 0.75
    )
    calibration = parse_mic_calibration(mic_cal_file)
    measurement = analyze_recording(
        recording,
        channel,
        schedule,
        measurement_spec,
        internal_mic=mic_name.startswith("alsa_input.pci-"),
        calibration=calibration,
    )
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


def profile_from_measurement(sink, mic, channel, voicing, measurement):
    quality = measurement["quality"]
    internal_mic = mic["name"].startswith("alsa_input.pci-")
    fit_payload = None
    if quality["accepted"]:
        fit_payload = optimize_peq(
            measurement,
            voicing,
            internal_mic=internal_mic,
        )
    profile = {
        "schema_version": 5,
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
        "safety": {
            "eq_max_db": fit_payload["maximum_allowed_boost_db"] if fit_payload else 0,
            "eq_min_db": -6,
            "highpass_hz": 55,
            "limiter_ceiling_dbfs": -1,
            "input_trim_db": -fit_payload["headroom_db"] if fit_payload else -1,
            "makeup_gain": False,
            "boost_policy": "Cuts are preferred; any boost must be broad, reliable, and improve a held-out repeat.",
        },
        "measurement": measurement,
        "quality": quality,
        "fit": fit_payload,
    }
    PROPOSAL.write_text(json.dumps(profile, indent=2) + "\n")
    return profile


def build_profile(sink, mic, channel, voicing, mic_cal_file=None):
    measurement = capture_measurement(
        sink["name"], mic["name"], channel, mic_cal_file
    )
    return profile_from_measurement(sink, mic, channel, voicing, measurement)


def reanalyze_saved_capture(voicing=None, channel_override=None):
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
    return profile_from_measurement(
        sink, mic, channel, voicing or previous.get("voicing", "warm"), measurement
    )


def parse_channel_selection(value):
    if isinstance(value, str) and value.lower() == "all":
        return "all"
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise SystemExit("Microphone channel must be a zero-based number or 'all'.") from error


def calibrate_noninteractive(sink_name, mic_name, channel, voicing, mic_cal_file=None):
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
        return build_profile(sink, mic, channel, voicing, mic_cal_file)
    except ValueError as error:
        raise SystemExit(str(error)) from error


def install_proposal():
    if not PROPOSAL.exists():
        raise SystemExit("No measured proposal is available.")
    profile = json.loads(PROPOSAL.read_text())
    if not profile.get("quality", {}).get("accepted") or not profile.get("fit"):
        raise SystemExit("This measurement failed its quality checks and cannot be installed.")
    install_profile(profile, filter_config(profile["speaker"]["name"], profile["fit"]))
    return profile


def status_payload():
    profile = json.loads(PROFILE.read_text()) if PROFILE.exists() else None
    proposal = json.loads(PROPOSAL.read_text()) if PROPOSAL.exists() else None
    active = run(["systemctl", "--user", "is-active", SERVICE], check=False, capture=True).stdout.strip()
    default = run(["pactl", "get-default-sink"], check=False, capture=True).stdout.strip()
    return {"service": active or "inactive", "defaultSink": default or "unknown",
            "profile": profile, "proposal": proposal,
            "enabled": active == "active" and default == VIRTUAL_SINK}


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
    print("\nVoicing:\n  Warm: softer and less sharp; comfortable for long listening."
          "\n  Flat: balanced with more clarity; may sound a little brighter.")
    answer = input("Choose [w]arm (recommended) or [f]lat? [w]: ").strip().lower()
    voicing = "neutral" if answer.startswith(("f", "n")) else "warm"
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
        profile = build_profile(sink, mic, channel, voicing, mic_cal_file)
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
    for center, q, gain in zip(
        fit_payload["centers_hz"], fit_payload["q"], gains
    ):
        print(f"  {center:7.1f} Hz  Q {q:4.2f}  {gain:5.2f} dB")
    print(f"  Input headroom: {fit_payload['headroom_db']:.2f} dB")
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
    if profile:
        print(f"Speaker: {profile['speaker']['description']}\nMicrophone: {profile['microphone']['description']}")
        gains = profile.get("fit", {}).get("gains_db") if profile.get("fit") else None
        voicing = "flat" if profile["voicing"] == "neutral" else "warm"
        print(f"Voicing: {voicing}\nGains: {gains or 'not installable'}")
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
    for name in ("wizard", "status", "status-json", "devices-json", "install-proposal", "disable"):
        sub.add_parser(name)
    calibrate = sub.add_parser("calibrate-json")
    calibrate.add_argument("--sink", required=True)
    calibrate.add_argument("--mic", required=True)
    calibrate.add_argument("--channel", default="0")
    calibrate.add_argument("--voicing", choices=("warm", "neutral"), default="warm")
    calibrate.add_argument("--mic-cal-file")
    reanalyze = sub.add_parser("reanalyze-saved-json")
    reanalyze.add_argument("--voicing", choices=("warm", "neutral"))
    reanalyze.add_argument("--channel")
    args = parser.parse_args()
    command = args.command or "wizard"
    if command == "devices-json":
        print(json.dumps(devices_payload()))
    elif command == "status-json":
        print(json.dumps(status_payload()))
    elif command == "calibrate-json":
        print(json.dumps(calibrate_noninteractive(
            args.sink, args.mic, args.channel, args.voicing, args.mic_cal_file
        )))
    elif command == "reanalyze-saved-json":
        print(json.dumps(reanalyze_saved_capture(args.voicing, args.channel)))
    elif command == "install-proposal":
        print(json.dumps(install_proposal()))
    else:
        {"wizard": wizard, "status": status, "disable": disable}[command]()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130)
