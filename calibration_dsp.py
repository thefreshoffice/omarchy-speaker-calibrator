#!/usr/bin/python3
"""Measurement DSP for Omarchy Speaker Calibrator.

This module deliberately contains no PipeWire or UI code.  It can therefore be
tested with synthetic captures before the plugin plays anything through a real
speaker.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import numpy as np
    from scipy import signal
    from scipy.ndimage import gaussian_filter1d
except ImportError as error:  # pragma: no cover - exercised by helper preflight
    raise SystemExit(
        f"DSP import failed in {__import__('sys').executable}: {error}. "
        "The panel expects Arch's python-numpy and python-scipy packages."
    ) from error


@dataclass(frozen=True)
class SweepSpec:
    rate: int = 48_000
    start_hz: float = 70.0
    end_hz: float = 18_000.0
    seconds: float = 2.8
    level_dbfs: float = -27.0
    repeats: int = 3
    pre_silence: float = 0.5
    block_gap: float = 0.65
    response_tail: float = 0.35

    @property
    def frames(self) -> int:
        return int(round(self.seconds * self.rate))


def dbfs(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-12))


def rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    values = np.asarray(samples, dtype=np.float64)
    return float(np.sqrt(np.mean(values * values)))


def make_sweep(spec: SweepSpec) -> np.ndarray:
    """Generate a constant-amplitude exponential sine sweep with soft ends."""
    t = np.arange(spec.frames, dtype=np.float64) / spec.rate
    log_ratio = math.log(spec.end_hz / spec.start_hz)
    phase = (2.0 * math.pi * spec.start_hz * spec.seconds / log_ratio) * (
        np.exp(t * log_ratio / spec.seconds) - 1.0
    )
    sweep = np.sin(phase)
    fade_frames = max(1, min(spec.frames // 8, int(round(0.04 * spec.rate))))
    fade = np.sin(np.linspace(0.0, math.pi / 2.0, fade_frames)) ** 2
    sweep[:fade_frames] *= fade
    sweep[-fade_frames:] *= fade[::-1]
    sweep *= 10.0 ** (spec.level_dbfs / 20.0)
    return sweep.astype(np.float64)


def build_measurement_signal(spec: SweepSpec) -> tuple[np.ndarray, list[dict]]:
    """Build a stereo test that measures left and right independently."""
    sweep = make_sweep(spec)
    pre = np.zeros(int(round(spec.pre_silence * spec.rate)), dtype=np.float64)
    gap = np.zeros(int(round(spec.block_gap * spec.rate)), dtype=np.float64)
    tail = np.zeros(int(round(spec.response_tail * spec.rate)), dtype=np.float64)
    chunks: list[np.ndarray] = [np.column_stack((pre, pre))]
    schedule: list[dict] = []
    cursor = pre.size
    # Alternating channels makes slow microphone gain drift visible instead of
    # systematically assigning it to one side.
    for repeat in range(spec.repeats):
        for output_channel in (0, 1):
            active = np.zeros((sweep.size, 2), dtype=np.float64)
            active[:, output_channel] = sweep
            schedule.append({
                "output_channel": output_channel,
                "repeat": repeat,
                "start_frame": cursor,
            })
            chunks.extend((active, np.column_stack((tail, tail)),
                           np.column_stack((gap, gap))))
            cursor += sweep.size + tail.size + gap.size
    return np.vstack(chunks), schedule


def write_pcm16_wave(path: Path, stereo: np.ndarray, rate: int) -> None:
    import wave

    clipped = np.clip(stereo, -0.999969, 0.999969)
    pcm = np.rint(clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())


def read_pcm16_wave_channels(path: Path, expected_rate: int) -> np.ndarray:
    """Read every channel without mixing microphone waveforms together."""
    import wave

    with wave.open(str(path), "rb") as wav:
        if wav.getframerate() != expected_rate or wav.getsampwidth() != 2:
            raise ValueError(
                f"Unexpected recording format: expected 16-bit/{expected_rate} Hz."
            )
        channels = wav.getnchannels()
        raw = wav.readframes(wav.getnframes())
    values = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    return values.reshape((-1, channels)).copy()


def read_pcm16_wave(path: Path, channel: int, expected_rate: int) -> tuple[np.ndarray, int]:
    captures = read_pcm16_wave_channels(path, expected_rate)
    channels = captures.shape[1]
    if channel < 0 or channel >= channels:
        raise ValueError(
            f"Microphone has {channels} channel(s), not channel {channel + 1}."
        )
    return captures[:, channel].copy(), channels


def parse_mic_calibration(path: str | Path | None) -> dict | None:
    """Parse common frequency/correction-dB microphone calibration text files."""
    if not path:
        return None
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Microphone calibration file not found: {source}")
    frequencies: list[float] = []
    corrections: list[float] = []
    sensitivity_dbfs = None
    for line in source.read_text(errors="replace").splitlines():
        sensitivity = re.search(
            r"sens(?:itivity)?[^-+0-9]*([-+]?\d+(?:\.\d+)?)\s*dBFS",
            line, re.IGNORECASE,
        )
        if sensitivity:
            sensitivity_dbfs = float(sensitivity.group(1))
        if not line.strip() or line.lstrip().startswith(("#", ";", "*")):
            continue
        numbers = re.findall(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", line)
        if len(numbers) < 2:
            continue
        frequency, correction = float(numbers[0]), float(numbers[1])
        if 5.0 <= frequency <= 100_000.0 and -60.0 <= correction <= 60.0:
            frequencies.append(frequency)
            corrections.append(correction)
    if len(frequencies) < 3:
        raise ValueError(
            "Calibration file needs at least three rows containing frequency and correction dB."
        )
    order = np.argsort(frequencies)
    return {
        "path": str(source),
        "frequency_hz": np.asarray(frequencies, dtype=float)[order].tolist(),
        "correction_db": np.asarray(corrections, dtype=float)[order].tolist(),
        "sensitivity_dbfs": sensitivity_dbfs,
    }


def _calibration_curve(calibration: dict | None, frequencies: np.ndarray) -> np.ndarray:
    if not calibration:
        return np.zeros_like(frequencies)
    source_f = np.asarray(calibration["frequency_hz"], dtype=float)
    source_db = np.asarray(calibration["correction_db"], dtype=float)
    return np.interp(
        np.log(frequencies), np.log(source_f), source_db,
        left=source_db[0], right=source_db[-1],
    )


def _normalized_correlation(segment: np.ndarray, sweep: np.ndarray) -> float:
    denominator = np.linalg.norm(segment) * np.linalg.norm(sweep)
    if denominator <= 1e-15:
        return 0.0
    return float(abs(np.dot(segment, sweep)) / denominator)


def locate_sweeps(
    capture: np.ndarray,
    sweep: np.ndarray,
    schedule: Iterable[dict],
    rate: int,
    record_lead_seconds: float,
) -> tuple[list[int], list[float], float]:
    """Locate each sweep and estimate playback/recording clock-rate mismatch."""
    expected = np.asarray([
        record_lead_seconds * rate + event["start_frame"] for event in schedule
    ], dtype=float)
    starts: list[int] = []
    correlations: list[float] = []
    base_offset = 0.0
    for index, target in enumerate(expected):
        predicted = target + base_offset
        radius = int((0.8 if index == 0 else 0.32) * rate)
        left = max(0, int(round(predicted)) - radius)
        right = min(capture.size, int(round(predicted)) + radius + sweep.size)
        window = capture[left:right]
        if window.size < sweep.size:
            raise ValueError("Recording ended before all calibration sweeps were captured.")
        correlation = signal.correlate(window, sweep, mode="valid", method="fft")
        start = left + int(np.argmax(np.abs(correlation)))
        segment = capture[start:start + sweep.size]
        starts.append(start)
        correlations.append(_normalized_correlation(segment, sweep))
        if index == 0:
            base_offset = start - target
    if len(starts) >= 2:
        slope, _ = np.polyfit(expected, np.asarray(starts, dtype=float), 1)
    else:
        slope = 1.0
    return starts, correlations, float(slope)


def _drift_corrected_segment(
    capture: np.ndarray, start: int, frames: int, clock_ratio: float
) -> np.ndarray:
    positions = start + np.arange(frames, dtype=np.float64) * clock_ratio
    source = np.arange(capture.size, dtype=np.float64)
    return np.interp(positions, source, capture, left=0.0, right=0.0)


def _log_grid(start_hz: float, end_hz: float, points_per_octave: int = 24) -> np.ndarray:
    count = int(math.floor(math.log2(end_hz / start_hz) * points_per_octave)) + 1
    return start_hz * 2.0 ** (np.arange(count, dtype=float) / points_per_octave)


def regularized_response(
    response: np.ndarray,
    sweep: np.ndarray,
    spec: SweepSpec,
    frequencies: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Return a regularized deconvolved impulse response and sampled magnitude."""
    fft_size = 1 << (response.size + sweep.size - 1).bit_length()
    excitation = np.fft.rfft(sweep, fft_size)
    observed = np.fft.rfft(response, fft_size)
    power = np.abs(excitation) ** 2
    regularizer = max(float(np.max(power)) * 1e-9, 1e-18)
    transfer = observed * np.conj(excitation) / (power + regularizer)
    impulse = np.fft.irfft(transfer, fft_size)

    bins = np.fft.rfftfreq(fft_size, 1.0 / spec.rate)
    magnitude_db = 20.0 * np.log10(np.maximum(np.abs(transfer), 1e-12))
    sampled = np.interp(frequencies, bins, magnitude_db)
    return impulse, sampled


def estimate_harmonic_residual_db(
    response: np.ndarray, spec: SweepSpec, noise_rms: float
) -> float:
    """Estimate 2nd/3rd-harmonic energy by coherent chirp demodulation."""
    frame = 4096
    hop = 4096
    if response.size < frame:
        return -120.0
    log_ratio = math.log(spec.end_hz / spec.start_hz)
    window = np.hanning(frame)
    ratios: list[float] = []
    for start in range(0, min(spec.frames, response.size) - frame + 1, hop):
        indices = start + np.arange(frame)
        t = indices / spec.rate
        center_frequency = spec.start_hz * math.exp(
            ((start + frame / 2.0) / spec.rate) * log_ratio / spec.seconds
        )
        if center_frequency < 120.0 or center_frequency * 3.0 > spec.end_hz:
            continue
        phase = (2.0 * math.pi * spec.start_hz * spec.seconds / log_ratio) * (
            np.exp(t * log_ratio / spec.seconds) - 1.0
        )
        values = response[start:start + frame] * window
        amplitudes = [
            abs(np.vdot(np.exp(1j * harmonic * phase), values))
            for harmonic in (1, 2, 3)
        ]
        fundamental = amplitudes[0]
        noise_amplitude = noise_rms * math.sqrt(float(np.sum(window * window)))
        if fundamental <= 8.0 * noise_amplitude:
            continue
        ratios.append(math.hypot(amplitudes[1], amplitudes[2]) / fundamental)
    return dbfs(float(np.median(ratios))) if ratios else -120.0


def _repeatability_details(curves: list[np.ndarray], frequencies: np.ndarray) -> dict:
    if len(curves) < 2:
        return {
            "repeatability_db": 0.0,
            "stable_band_percent": 100.0,
            "accepted_indices": list(range(len(curves))),
            "excluded_indices": [],
        }
    stack = np.vstack(curves)
    # Quality is assessed on broad behavior because the generated correction is
    # broad PEQ.  Raw narrow-bin variance in a room is not evidence that the
    # broad response is unusable.
    smoothed = gaussian_filter1d(stack, sigma=4.0, axis=1, mode="nearest")
    band = (frequencies >= 160.0) & (frequencies <= 10_000.0)
    normalized = smoothed[:, band] - np.median(smoothed[:, band], axis=1, keepdims=True)
    center = np.median(normalized, axis=0)
    errors = np.sqrt(np.mean((normalized - center) ** 2, axis=1))
    # Keep all repetitions when all agree.  Otherwise select the closest pair;
    # this lets three repeats tolerate one cough, key press, or notification.
    accepted = list(range(len(curves)))
    repeatability = float(np.max(errors))
    stable = float(np.mean(np.max(normalized, axis=0) - np.min(normalized, axis=0) <= 3.0) * 100.0)
    if len(curves) >= 3 and (repeatability > 2.5 or stable < 85.0):
        pairs = []
        for left in range(len(curves)):
            for right in range(left + 1, len(curves)):
                difference = np.abs(normalized[left] - normalized[right])
                pairs.append((
                    float(np.sqrt(np.mean(difference * difference))),
                    float(np.mean(difference <= 3.0) * 100.0),
                    [left, right],
                ))
        repeatability, stable, accepted = min(pairs, key=lambda item: item[0])
    excluded = [index for index in range(len(curves)) if index not in accepted]
    return {
        "repeatability_db": repeatability,
        "stable_band_percent": stable,
        "accepted_indices": accepted,
        "excluded_indices": excluded,
    }


def _quality_summary(
    *, internal_mic: bool, calibration: dict | None, background_dbfs: float,
    min_broadband_prominence_db: float, min_correlation: float,
    clock_drift_ppm: float, worst_repeatability_db: float,
    minimum_stable_band_percent: float, worst_gain_stability_db: float,
    worst_harmonic_residual_db: float, clipped_samples: int,
    excluded_sweeps: int,
) -> dict:
    failures: list[str] = []
    warnings: list[str] = []
    guidance: list[str] = []

    if clipped_samples:
        failures.append(f"The microphone clipped {clipped_samples} sample(s).")
        guidance.append("Lower speaker volume or microphone input gain, then measure again.")
    # Broadband sweep RMS versus broadband room noise is only a prominence
    # diagnostic; it is not a valid swept-sine SNR and must not veto an
    # otherwise repeatable deconvolution.
    if min_broadband_prominence_db < 0.0:
        warnings.append(
            f"Broadband sweep prominence is low ({min_broadband_prominence_db:.1f} dB); "
            "repeat agreement was used for confidence."
        )
    if min_correlation < 0.08:
        failures.append("One or more sweeps could not be aligned reliably.")
        guidance.append("Pause all other audio, keep the microphone still, and retry.")
    elif min_correlation < 0.15:
        warnings.append("Sweep alignment is usable but weak.")
    if abs(clock_drift_ppm) > 5_000.0:
        failures.append(f"Playback/recording clock drift is excessive ({clock_drift_ppm:+.0f} ppm).")
        guidance.append("Reconnect the USB microphone or use another input device.")
    elif abs(clock_drift_ppm) > 500.0:
        warnings.append(f"Clock drift was corrected ({clock_drift_ppm:+.0f} ppm).")
    if worst_repeatability_db > 3.0 or minimum_stable_band_percent < 70.0:
        failures.append(f"Repeated sweeps differ by {worst_repeatability_db:.1f} dB.")
        guidance.append("Keep the microphone and laptop still and reduce background noise.")
    elif worst_repeatability_db > 1.5:
        warnings.append(f"Sweep repeatability is limited ({worst_repeatability_db:.1f} dB).")
    if minimum_stable_band_percent < 90.0 and minimum_stable_band_percent >= 70.0:
        warnings.append(
            f"Only {minimum_stable_band_percent:.0f}% of the broad response was stable within 3 dB."
        )
    if worst_gain_stability_db > 1.5:
        if internal_mic and worst_gain_stability_db <= 6.0:
            warnings.append(
                f"Built-in microphone level changed by {worst_gain_stability_db:.1f} dB; "
                "repeat shape, rather than absolute level, was used for confidence."
            )
        else:
            failures.append(
                f"Microphone gain changed by {worst_gain_stability_db:.1f} dB; AGC may be active."
            )
        guidance.append("Disable automatic gain control, echo cancellation, and noise suppression.")
    elif worst_gain_stability_db > 0.7:
        warnings.append(
            f"Microphone gain varied by {worst_gain_stability_db:.1f} dB."
        )
    if worst_harmonic_residual_db > -12.0:
        failures.append(
            f"Estimated harmonic residual is high ({worst_harmonic_residual_db:.1f} dB)."
        )
        guidance.append("Lower playback level and check for rattling or microphone overload.")
    elif worst_harmonic_residual_db > -25.0:
        warnings.append(
            f"Estimated harmonic residual is elevated ({worst_harmonic_residual_db:.1f} dB)."
        )
    if excluded_sweeps:
        warnings.append(
            f"Discarded {excluded_sweeps} contaminated sweep(s); two repeatable captures per speaker remained."
        )
    if internal_mic:
        warnings.append(
            "Built-in microphone mode is a relative estimate; chassis coupling and unknown mic response remain."
        )
    elif calibration is None:
        warnings.append("No microphone calibration file was supplied.")
        guidance.append("For final tuning, load the serial-number calibration file for this microphone.")
    if background_dbfs > -35.0:
        warnings.append(f"Recorded background level is high ({background_dbfs:.1f} dBFS).")

    # De-duplicate advice created by multiple quality checks.
    guidance = list(dict.fromkeys(guidance))
    accepted = not failures
    verdict = "fail" if failures else ("warning" if warnings else "pass")
    return {
        "accepted": accepted,
        "verdict": verdict,
        "failures": failures,
        "warnings": warnings,
        "guidance": guidance,
        "metrics": {
            "background_dbfs": round(background_dbfs, 2),
            "minimum_broadband_prominence_db": round(min_broadband_prominence_db, 2),
            "minimum_alignment_correlation": round(min_correlation, 4),
            "clock_drift_ppm": round(clock_drift_ppm, 1),
            "worst_repeatability_db": round(worst_repeatability_db, 2),
            "minimum_stable_band_percent": round(minimum_stable_band_percent, 1),
            "worst_gain_stability_db": round(worst_gain_stability_db, 2),
            "worst_harmonic_residual_db": round(worst_harmonic_residual_db, 2),
            "clipped_samples": int(clipped_samples),
            "excluded_sweeps": int(excluded_sweeps),
        },
    }


def _add_measurement_level_guidance(quality: dict, maximum_peak_dbfs: float) -> None:
    """Explain whether accepted sweeps used a useful microphone level."""
    quality["metrics"]["maximum_accepted_peak_dbfs"] = round(maximum_peak_dbfs, 2)
    if maximum_peak_dbfs < -9.0:
        quality["warnings"].append(
            f"The accepted test signal peaked at only {maximum_peak_dbfs:.1f} dBFS."
        )
        quality["guidance"].append(
            "For a more repeatable result, raise speaker volume slightly and retry."
        )
        quality["metrics"]["measurement_level"] = "low"
    elif maximum_peak_dbfs > -1.0:
        quality["warnings"].append(
            f"The accepted test signal came close to clipping ({maximum_peak_dbfs:.1f} dBFS)."
        )
        quality["guidance"].append(
            "Lower speaker volume slightly before the next measurement."
        )
        quality["metrics"]["measurement_level"] = "high"
    else:
        quality["metrics"]["measurement_level"] = "good"
    quality["warnings"] = list(dict.fromkeys(quality["warnings"]))
    quality["guidance"] = list(dict.fromkeys(quality["guidance"]))
    if quality["accepted"]:
        quality["verdict"] = "warning" if quality["warnings"] else "pass"


def analyse_capture(
    capture: np.ndarray,
    schedule: list[dict],
    spec: SweepSpec,
    *,
    record_lead_seconds: float,
    internal_mic: bool,
    calibration: dict | None = None,
) -> dict:
    """Analyze a mono capture of the generated stereo measurement program."""
    sweep = make_sweep(spec)
    starts, correlations, clock_ratio = locate_sweeps(
        capture, sweep, schedule, spec.rate, record_lead_seconds
    )
    drift_ppm = (clock_ratio - 1.0) * 1_000_000.0
    noise_frames = max(1, int(max(0.15, record_lead_seconds * 0.7) * spec.rate))
    noise = capture[:noise_frames]
    noise_level = rms(noise)
    noise_dbfs = dbfs(noise_level)
    frequencies = _log_grid(max(80.0, spec.start_hz), min(16_000.0, spec.end_hz))
    calibration_curve = _calibration_curve(calibration, frequencies)
    tail_frames = int(round(spec.response_tail * spec.rate))

    per_channel: dict[int, list[dict]] = {0: [], 1: []}
    for event, start, correlation in zip(schedule, starts, correlations):
        segment = _drift_corrected_segment(
            capture, start, sweep.size + tail_frames, clock_ratio
        )
        sweep_part = segment[:sweep.size]
        impulse, curve = regularized_response(segment, sweep, spec, frequencies)
        curve += calibration_curve
        segment_rms = rms(sweep_part)
        item = {
            "repeat": event["repeat"],
            "start_frame": start,
            "alignment_correlation": round(correlation, 5),
            "rms_dbfs": round(dbfs(segment_rms), 3),
            "peak_dbfs": round(dbfs(float(np.max(np.abs(sweep_part)))), 3),
            "clipped_samples": int(np.count_nonzero(np.abs(sweep_part) >= 0.999)),
            "broadband_prominence_db": round(dbfs(segment_rms) - noise_dbfs, 3),
            "harmonic_residual_db": round(
                estimate_harmonic_residual_db(sweep_part, spec, noise_level), 3
            ),
            "response_db": curve,
            "impulse_peak": round(float(np.max(np.abs(impulse))), 7),
        }
        per_channel[event["output_channel"]].append(item)

    channel_results = []
    all_repeatabilities = []
    all_stable_band_percentages = []
    all_gain_stabilities = []
    all_harmonics = []
    accepted_correlations = []
    accepted_prominences = []
    accepted_peaks = []
    excluded_sweeps = 0
    accepted_clipped_samples = 0
    channel_curves = []
    validation_curves = []
    for output_channel in (0, 1):
        items = per_channel[output_channel]
        curves = [item["response_db"] for item in items]
        clean_indices = [
            index for index, item in enumerate(items) if item["clipped_samples"] == 0
        ]
        candidate_indices = clean_indices if len(clean_indices) >= 2 else list(range(len(items)))
        repeatability = _repeatability_details(
            [curves[index] for index in candidate_indices], frequencies
        )
        accepted_indices = [
            candidate_indices[index] for index in repeatability["accepted_indices"]
        ]
        excluded_indices = [
            index for index in range(len(items)) if index not in accepted_indices
        ]
        accepted_items = [items[index] for index in accepted_indices]
        accepted_curves = [curves[index] for index in accepted_indices]
        levels = [item["rms_dbfs"] for item in accepted_items]
        gain_stability = max(levels) - min(levels) if levels else 0.0
        accepted_stack = np.vstack(accepted_curves)
        aggregate = np.median(accepted_stack, axis=0)
        uncertainty = (
            np.max(accepted_stack, axis=0) - np.min(accepted_stack, axis=0)
        ) / 2.0
        channel_curves.append(aggregate)
        harmonics = [item["harmonic_residual_db"] for item in accepted_items]
        worst_harmonic = max(harmonics) if harmonics else -120.0
        all_repeatabilities.append(repeatability["repeatability_db"])
        all_stable_band_percentages.append(repeatability["stable_band_percent"])
        all_gain_stabilities.append(gain_stability)
        all_harmonics.append(worst_harmonic)
        accepted_correlations.extend(item["alignment_correlation"] for item in accepted_items)
        accepted_prominences.extend(item["broadband_prominence_db"] for item in accepted_items)
        accepted_peaks.extend(item["peak_dbfs"] for item in accepted_items)
        accepted_clipped_samples += sum(item["clipped_samples"] for item in accepted_items)
        excluded_sweeps += len(excluded_indices)
        public_sweeps = []
        for index, item in enumerate(items):
            public = {key: value for key, value in item.items() if key != "response_db"}
            public["accepted"] = index in accepted_indices
            public_sweeps.append(public)
            if index in accepted_indices:
                validation_curves.append({
                    "output_channel": "left" if output_channel == 0 else "right",
                    "repeat": item["repeat"],
                    "response_db": np.round(item["response_db"], 3).tolist(),
                })
        channel_results.append({
            "output_channel": "left" if output_channel == 0 else "right",
            "repeatability_db": round(repeatability["repeatability_db"], 3),
            "stable_band_percent": round(repeatability["stable_band_percent"], 1),
            "accepted_repeats": [items[index]["repeat"] for index in accepted_indices],
            "excluded_repeats": [items[index]["repeat"]
                                  for index in excluded_indices],
            "gain_stability_db": round(gain_stability, 3),
            "harmonic_residual_db": round(worst_harmonic, 3),
            "response_db": np.round(aggregate, 3).tolist(),
            "uncertainty_db": np.round(uncertainty, 3).tolist(),
            "sweeps": public_sweeps,
        })

    combined = np.mean(np.vstack(channel_curves), axis=0)
    quality = _quality_summary(
        internal_mic=internal_mic,
        calibration=calibration,
        background_dbfs=noise_dbfs,
        min_broadband_prominence_db=min(accepted_prominences),
        min_correlation=min(accepted_correlations),
        clock_drift_ppm=drift_ppm,
        worst_repeatability_db=max(all_repeatabilities),
        minimum_stable_band_percent=min(all_stable_band_percentages),
        worst_gain_stability_db=max(all_gain_stabilities),
        worst_harmonic_residual_db=max(all_harmonics),
        clipped_samples=accepted_clipped_samples,
        excluded_sweeps=excluded_sweeps,
    )
    _add_measurement_level_guidance(quality, max(accepted_peaks))
    return {
        "method": "repeated-exponential-sine-sweep",
        "rate_hz": spec.rate,
        "sweep": {
            "start_hz": spec.start_hz,
            "end_hz": spec.end_hz,
            "seconds": spec.seconds,
            "level_dbfs": spec.level_dbfs,
            "repeats_per_speaker": spec.repeats,
        },
        "microphone_calibration": calibration,
        "clock_ratio": round(clock_ratio, 9),
        "frequency_hz": np.round(frequencies, 3).tolist(),
        "level_dbfs": np.round(combined, 3).tolist(),
        "channels": channel_results,
        # Accepted repeat curves are retained so Phase 2 can choose filter
        # count and bandwidth on one repeat and verify them on another.  They
        # are frequency responses, not raw microphone audio.
        "validation_curves": validation_curves,
        "quality": quality,
    }


def combine_microphone_measurements(
    measurements: list[dict], input_channels: list[int]
) -> dict:
    """Combine independently analyzed microphone channels in the dB domain.

    Raw microphone signals must not be averaged: small spacing and timing
    differences would create artificial comb filtering.  Instead, channel gain
    is aligned in the broad midband, magnitude responses are combined with a
    median, and microphone disagreement is added to the uncertainty used by the
    PEQ optimizer.
    """
    if not measurements or len(measurements) != len(input_channels):
        raise ValueError("At least one analyzed microphone channel is required.")

    frequencies = np.asarray(measurements[0]["frequency_hz"], dtype=float)
    for measurement in measurements[1:]:
        if not np.allclose(
            frequencies, np.asarray(measurement["frequency_hz"], dtype=float)
        ):
            raise ValueError("Microphone channels use incompatible frequency grids.")

    accepted = [
        index for index, measurement in enumerate(measurements)
        if measurement["quality"]["accepted"]
    ]
    rejected: list[dict] = []
    for index, (measurement, input_channel) in enumerate(zip(measurements, input_channels)):
        if index not in accepted:
            rejected.append({
                "input_channel": input_channel,
                "reason": "; ".join(measurement["quality"]["failures"])
                or "failed measurement quality checks",
            })

    if accepted:
        selected = accepted.copy()
    else:
        # Keep the least-bad response for diagnosis, but preserve a failed
        # quality verdict so it can never be installed.
        selected = [min(
            range(len(measurements)),
            key=lambda index: (
                len(measurements[index]["quality"]["failures"]),
                measurements[index]["quality"]["metrics"]["worst_repeatability_db"],
                -measurements[index]["quality"]["metrics"]["minimum_alignment_correlation"],
            ),
        )]

    reference_band = (frequencies >= 250.0) & (frequencies <= 2000.0)
    references = np.asarray([
        np.median(np.asarray(measurements[index]["level_dbfs"], dtype=float)[reference_band])
        for index in selected
    ])
    common_reference = float(np.median(references))
    offsets = common_reference - references

    # With three or more usable microphones, reject only a clear broad-response
    # outlier. With two microphones there is no principled way to decide which
    # one is wrong, so both remain and their disagreement becomes uncertainty.
    response_outliers: list[int] = []
    if len(selected) >= 3:
        normalized = []
        for position, index in enumerate(selected):
            curve = np.asarray(measurements[index]["level_dbfs"], dtype=float) + offsets[position]
            normalized.append(gaussian_filter1d(curve, sigma=4.0, mode="nearest"))
        stack = np.vstack(normalized)
        center = np.median(stack, axis=0)
        errors = np.sqrt(np.mean((stack[:, reference_band] - center[reference_band]) ** 2, axis=1))
        error_center = float(np.median(errors))
        error_mad = float(np.median(np.abs(errors - error_center)))
        threshold = max(3.0, error_center + 3.0 * max(error_mad, 0.25))
        keep_positions = [position for position, error in enumerate(errors) if error <= threshold]
        if len(keep_positions) >= 2:
            response_outliers = [
                selected[position] for position in range(len(selected))
                if position not in keep_positions
            ]
            for index in response_outliers:
                rejected.append({
                    "input_channel": input_channels[index],
                    "reason": "broad response disagreed with the other microphones",
                })
            selected = [selected[position] for position in keep_positions]
            references = references[keep_positions]
            common_reference = float(np.median(references))
            offsets = common_reference - references

    level_stack = np.vstack([
        np.asarray(measurements[index]["level_dbfs"], dtype=float) + offsets[position]
        for position, index in enumerate(selected)
    ])
    combined_level = np.median(level_stack, axis=0)
    combined_channels = []
    validation_curves = []
    spread_errors = []
    analysis_band = (frequencies >= 160.0) & (frequencies <= 10_000.0)

    for output_index in range(len(measurements[0]["channels"])):
        sources = [measurements[index]["channels"][output_index] for index in selected]
        response_stack = np.vstack([
            np.asarray(source["response_db"], dtype=float) + offsets[position]
            for position, source in enumerate(sources)
        ])
        aggregate = np.median(response_stack, axis=0)
        within_uncertainty = np.median(np.vstack([
            np.asarray(source.get("uncertainty_db", np.zeros(frequencies.size)), dtype=float)
            for source in sources
        ]), axis=0)
        if len(sources) == 1:
            between_uncertainty = np.zeros_like(aggregate)
        elif len(sources) == 2:
            between_uncertainty = (
                np.max(response_stack, axis=0) - np.min(response_stack, axis=0)
            ) / 2.0
        else:
            between_uncertainty = 1.4826 * np.median(
                np.abs(response_stack - aggregate), axis=0
            )
        uncertainty = np.hypot(within_uncertainty, between_uncertainty)

        smoothed_stack = gaussian_filter1d(response_stack, sigma=4.0, axis=1, mode="nearest")
        smoothed_center = np.median(smoothed_stack, axis=0)
        spread_errors.extend(
            float(np.sqrt(np.mean((curve[analysis_band] - smoothed_center[analysis_band]) ** 2)))
            for curve in smoothed_stack
        )
        accepted_repeat_sets = [set(source["accepted_repeats"]) for source in sources]
        common_repeats = set.intersection(*accepted_repeat_sets)
        accepted_repeats = sorted(common_repeats or set.union(*accepted_repeat_sets))
        excluded_repeats = sorted(set().union(*(
            set(source["excluded_repeats"]) for source in sources
        )))
        combined_channels.append({
            "output_channel": sources[0]["output_channel"],
            "repeatability_db": round(max(source["repeatability_db"] for source in sources), 3),
            "stable_band_percent": round(min(source["stable_band_percent"] for source in sources), 1),
            "accepted_repeats": accepted_repeats,
            "excluded_repeats": excluded_repeats,
            "gain_stability_db": round(max(source["gain_stability_db"] for source in sources), 3),
            "harmonic_residual_db": round(max(source["harmonic_residual_db"] for source in sources), 3),
            "response_db": np.round(aggregate, 3).tolist(),
            "uncertainty_db": np.round(uncertainty, 3).tolist(),
            "sweeps": [],
        })

    for position, index in enumerate(selected):
        for curve in measurements[index].get("validation_curves", []):
            validation_curves.append({
                "input_channel": input_channels[index],
                "output_channel": curve["output_channel"],
                "repeat": curve["repeat"],
                "response_db": np.round(
                    np.asarray(curve["response_db"], dtype=float) + offsets[position], 3
                ).tolist(),
            })

    used_measurements = [measurements[index] for index in selected]
    used_qualities = [measurement["quality"] for measurement in used_measurements]
    metrics = {
        "background_dbfs": round(max(
            quality["metrics"]["background_dbfs"] for quality in used_qualities
        ), 2),
        "minimum_broadband_prominence_db": round(min(
            quality["metrics"]["minimum_broadband_prominence_db"] for quality in used_qualities
        ), 2),
        "minimum_alignment_correlation": round(min(
            quality["metrics"]["minimum_alignment_correlation"] for quality in used_qualities
        ), 4),
        "clock_drift_ppm": round(float(np.median([
            quality["metrics"]["clock_drift_ppm"] for quality in used_qualities
        ])), 1),
        "worst_repeatability_db": round(max(
            quality["metrics"]["worst_repeatability_db"] for quality in used_qualities
        ), 2),
        "minimum_stable_band_percent": round(min(
            quality["metrics"]["minimum_stable_band_percent"] for quality in used_qualities
        ), 1),
        "worst_gain_stability_db": round(max(
            quality["metrics"]["worst_gain_stability_db"] for quality in used_qualities
        ), 2),
        "worst_harmonic_residual_db": round(max(
            quality["metrics"]["worst_harmonic_residual_db"] for quality in used_qualities
        ), 2),
        "clipped_samples": int(max(
            quality["metrics"]["clipped_samples"] for quality in used_qualities
        )),
        "excluded_sweeps": int(max(
            quality["metrics"]["excluded_sweeps"] for quality in used_qualities
        )),
        "maximum_accepted_peak_dbfs": round(max(
            quality["metrics"]["maximum_accepted_peak_dbfs"] for quality in used_qualities
        ), 2),
        "microphone_channels_requested": len(input_channels),
        "microphone_channels_used": len(selected),
        "microphone_channels_rejected": len(input_channels) - len(selected),
        "inter_microphone_spread_db": round(max(spread_errors, default=0.0), 2),
    }
    quality = _quality_summary(
        internal_mic=True,
        calibration=measurements[0]["microphone_calibration"],
        background_dbfs=metrics["background_dbfs"],
        min_broadband_prominence_db=metrics["minimum_broadband_prominence_db"],
        min_correlation=metrics["minimum_alignment_correlation"],
        clock_drift_ppm=metrics["clock_drift_ppm"],
        worst_repeatability_db=metrics["worst_repeatability_db"],
        minimum_stable_band_percent=metrics["minimum_stable_band_percent"],
        worst_gain_stability_db=metrics["worst_gain_stability_db"],
        worst_harmonic_residual_db=metrics["worst_harmonic_residual_db"],
        clipped_samples=metrics["clipped_samples"],
        excluded_sweeps=metrics["excluded_sweeps"],
    )
    _add_measurement_level_guidance(
        quality, metrics["maximum_accepted_peak_dbfs"]
    )
    quality["metrics"].update({
        key: value for key, value in metrics.items()
        if key.startswith("microphone_") or key == "inter_microphone_spread_db"
    })
    if not accepted:
        quality["accepted"] = False
        quality["verdict"] = "fail"
        quality["failures"] = list(dict.fromkeys([
            "No built-in microphone channel passed all measurement quality checks.",
            *(failure for item in used_qualities for failure in item["failures"]),
            *quality["failures"],
        ]))
    if rejected and accepted:
        quality["warnings"].append(
            f"Used {len(selected)} of {len(input_channels)} built-in microphone channels; "
            "unreliable or disagreeing channels were ignored."
        )
    if metrics["inter_microphone_spread_db"] > 2.5 and len(selected) > 1:
        quality["warnings"].append(
            f"Built-in microphones differ by {metrics['inter_microphone_spread_db']:.1f} dB "
            "across the broad response; disagreement reduces correction confidence."
        )
    quality["warnings"] = list(dict.fromkeys(quality["warnings"]))
    if quality["accepted"]:
        quality["verdict"] = "warning" if quality["warnings"] else "pass"
    return {
        "method": "repeated-exponential-sine-sweep-multi-microphone",
        "rate_hz": measurements[0]["rate_hz"],
        "sweep": measurements[0]["sweep"],
        "microphone_calibration": measurements[0]["microphone_calibration"],
        "clock_ratio": round(float(np.median([
            measurement["clock_ratio"] for measurement in used_measurements
        ])), 9),
        "frequency_hz": measurements[0]["frequency_hz"],
        "level_dbfs": np.round(combined_level, 3).tolist(),
        "channels": combined_channels,
        "validation_curves": validation_curves,
        "microphone_array": {
            "combination": "level-aligned median of independently analyzed magnitude responses",
            "raw_waveforms_mixed": False,
            "requested_channels": input_channels,
            "used_channels": [input_channels[index] for index in selected],
            "rejected_channels": rejected,
            "inter_microphone_spread_db": metrics["inter_microphone_spread_db"],
            "channels": [
                {
                    "input_channel": input_channel,
                    "accepted": measurement["quality"]["accepted"],
                    "verdict": measurement["quality"]["verdict"],
                    "failures": measurement["quality"]["failures"],
                }
                for input_channel, measurement in zip(input_channels, measurements)
            ],
        },
        "quality": quality,
    }
