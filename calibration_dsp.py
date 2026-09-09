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

from calibration_io import MAX_CALIBRATION_BYTES, read_text_bounded
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
    # The user picked this path, so it is input like any other: read it once
    # through a descriptor that refuses a symlink and caps the size, rather
    # than testing the name and opening it again afterwards.
    source = Path(path).expanduser()
    frequencies: list[float] = []
    corrections: list[float] = []
    sensitivity_dbfs = None
    # A calibration file can just as well have come from a package as from the
    # user's own directory, so root owning it is not a reason to refuse it.
    body = read_text_bounded(
        source, MAX_CALIBRATION_BYTES, errors="replace", allow_root=True)
    if body is None:
        raise ValueError(f"Microphone calibration file not found: {source}")
    for line in body.splitlines():
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


# Recording kept in front of every sweep so the direct sound never sits at the
# edge of the deconvolved buffer, and so the noise-floor buffer can be gated at
# the same nominal position.
GATE_PRE_ARRIVAL_SECONDS = 0.05
# Length of the frequency-dependent gate in cycles of each analysis frequency:
# 150 ms at 100 Hz, 15 ms at 1 kHz, 1.5 ms at 10 kHz.  Long enough to keep the
# desk reflection that a listener at the laptop also hears, short enough above
# a few hundred hertz to drop wall and ceiling reflections and the distortion
# products that a sine sweep folds into negative time.
DEFAULT_GATE_CYCLES = 15.0


def _band_limit_weights(bins: np.ndarray, spec: SweepSpec) -> np.ndarray:
    """Raised-cosine edges just outside the sweep band.

    Dividing by the sweep spectrum outside the band divides by almost nothing
    and turns recording noise into a huge out-of-band impulse-response
    artefact.  Rolling those bins off keeps the time domain honest.
    """
    low_start, low_end = 0.71 * spec.start_hz, spec.start_hz
    high_start = spec.end_hz
    high_end = min(1.12 * spec.end_hz, 0.5 * spec.rate)
    weights = np.ones_like(bins)
    below = bins < low_end
    weights[below] = 0.5 - 0.5 * np.cos(
        np.pi * np.clip((bins[below] - low_start) / (low_end - low_start), 0.0, 1.0)
    )
    above = bins > high_start
    weights[above] = 0.5 + 0.5 * np.cos(
        np.pi * np.clip((bins[above] - high_start) / max(high_end - high_start, 1e-9), 0.0, 1.0)
    )
    return weights


def deconvolve(
    response: np.ndarray, sweep: np.ndarray, spec: SweepSpec
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return the band-limited impulse response, transfer function, and bins."""
    fft_size = 1 << (response.size + sweep.size - 1).bit_length()
    excitation = np.fft.rfft(sweep, fft_size)
    observed = np.fft.rfft(response, fft_size)
    power = np.abs(excitation) ** 2
    regularizer = max(float(np.max(power)) * 1e-9, 1e-18)
    transfer = observed * np.conj(excitation) / (power + regularizer)
    bins = np.fft.rfftfreq(fft_size, 1.0 / spec.rate)
    transfer *= _band_limit_weights(bins, spec)
    impulse = np.fft.irfft(transfer, fft_size)
    return impulse, transfer, bins


def locate_direct_sound(
    impulse: np.ndarray, rate: int, nominal_index: int,
    *, before_seconds: float = 0.005, after_seconds: float = 0.03,
) -> int:
    """Index of the direct-sound peak near where the sweep alignment put it."""
    low = max(0, nominal_index - int(round(before_seconds * rate)))
    high = min(impulse.size, nominal_index + int(round(after_seconds * rate)))
    if high <= low:
        return int(np.clip(nominal_index, 0, impulse.size - 1))
    return low + int(np.argmax(np.abs(impulse[low:high])))


def gated_magnitude_db(
    impulse: np.ndarray,
    rate: int,
    frequencies: np.ndarray,
    peak_index: int,
    cycles: float,
    *,
    minimum_pre_seconds: float = 0.0005,
) -> np.ndarray:
    """Magnitude at each frequency through a window of ``cycles`` periods.

    For every analysis frequency the impulse response is windowed with a short
    rising half-Hann before the direct-sound peak and a falling half-Hann of
    ``cycles / f`` after it, and the response at that one frequency is read
    directly from the windowed samples.  This is the frequency-dependent
    window that room-measurement tools apply before equalisation, evaluated
    only on the analysis grid, which keeps it cheap and exact.
    """
    size = impulse.size
    out = np.empty(frequencies.size, dtype=float)
    minimum_pre = max(1, int(round(minimum_pre_seconds * rate)))
    for position, frequency in enumerate(frequencies):
        length = max(8, int(round(cycles / float(frequency) * rate)))
        pre = max(minimum_pre, length // 8)
        offsets = np.arange(-pre, length)
        window = np.empty(offsets.size)
        window[:pre] = 0.5 - 0.5 * np.cos(np.pi * np.arange(pre) / pre)
        window[pre:] = 0.5 + 0.5 * np.cos(np.pi * np.arange(length) / length)
        samples = impulse[(peak_index + offsets) % size] * window
        phasor = np.exp(-2j * np.pi * float(frequency) * offsets / rate)
        out[position] = 20.0 * np.log10(max(abs(np.dot(samples, phasor)), 1e-12))
    return out


def regularized_response(
    response: np.ndarray,
    sweep: np.ndarray,
    spec: SweepSpec,
    frequencies: np.ndarray,
    *,
    gate_cycles: float | None = DEFAULT_GATE_CYCLES,
    nominal_peak_index: int = 0,
    locate_peak: bool = True,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Return the impulse response, its (gated) magnitude, and the peak index.

    ``gate_cycles=None`` returns the whole-buffer magnitude, which includes
    every room reflection, the noise, and the folded distortion products.
    """
    impulse, transfer, bins = deconvolve(response, sweep, spec)
    peak_index = (
        locate_direct_sound(impulse, spec.rate, nominal_peak_index)
        if locate_peak else int(nominal_peak_index)
    )
    if gate_cycles is None:
        magnitude_db = 20.0 * np.log10(np.maximum(np.abs(transfer), 1e-12))
        return impulse, np.interp(frequencies, bins, magnitude_db), peak_index
    return impulse, gated_magnitude_db(
        impulse, spec.rate, frequencies, peak_index, gate_cycles
    ), peak_index


def silence_buffer(
    capture: np.ndarray,
    starts: list[int],
    spec: SweepSpec,
    record_lead_seconds: float,
    length: int,
) -> np.ndarray:
    """Recorded room sound from between the sweeps, tiled to ``length``.

    Running this through the same deconvolution and gate as a sweep measures
    the noise floor in exactly the units of the response, at every frequency,
    without any assumption about how the inverse filter spreads noise in time.
    """
    rate = spec.rate
    margin = int(round(min(0.1, spec.block_gap / 4.0) * rate))
    tail = int(round(spec.response_tail * rate))
    pieces = []
    first_end = starts[0] - margin
    first_start = max(0, first_end - int(round(
        (0.75 * record_lead_seconds + spec.pre_silence) * rate
    )))
    if first_end > first_start:
        pieces.append(capture[first_start:first_end])
    for previous, following in zip(starts, starts[1:]):
        begin = previous + spec.frames + tail + margin
        end = following - margin
        if end > begin:
            pieces.append(capture[begin:end])
    silence = np.concatenate(pieces) if pieces else np.zeros(0)
    if silence.size == 0:
        return np.zeros(length)
    return np.resize(silence, length)


# REW calls the same idea a cubic mean.  Averaging cubed amplitudes rather
# than decibels lets a peak dominate its neighbourhood while a narrow notch
# barely counts, which matches what is audible: a resonance is heard, a
# cancellation of the same depth and width mostly is not.
PEAK_WEIGHT_EXPONENT = 3.0


def erb_octaves(frequencies: np.ndarray) -> np.ndarray:
    """Width of the ear's critical band at each frequency, in octaves.

    Glasberg and Moore's equivalent rectangular bandwidth.  It is roughly a
    fixed number of hertz through the bass, which is a large fraction of an
    octave down there and a small one up high: about 0.9 octaves at 50 Hz,
    a third of an octave at 200 Hz, and a sixth from 1 kHz upward.  Smoothing
    a response by it shows what the ear can actually resolve, instead of
    detail no listener could hear and no filter should chase.
    """
    frequencies = np.asarray(frequencies, dtype=float)
    bandwidth = 24.7 * (4.37 * frequencies / 1000.0 + 1.0)
    half = bandwidth / 2.0
    return np.log2((frequencies + half) / np.maximum(frequencies - half, 1e-6))


def perceptual_smooth(
    frequencies: np.ndarray,
    curve_db: np.ndarray,
    *,
    peak_weighted: bool = True,
    width_scale: float = 1.0,
) -> np.ndarray:
    """Smooth a magnitude response the way the ear resolves it.

    The window widens with the critical band, so the unreliable bass is
    averaged over a broad span while the midrange keeps its detail.  With
    ``peak_weighted`` the average is taken over cubed amplitudes, so peaks
    survive and narrow dips are largely filled in; that is the asymmetry the
    loudspeaker literature asks for, since filling a cancellation with gain
    achieves nothing but a resonance is worth removing.  Pass
    ``peak_weighted=False`` for a curve that is a difference rather than a
    response, where a plain mean is the honest one.
    """
    frequencies = np.asarray(frequencies, dtype=float)
    curve = np.asarray(curve_db, dtype=float)
    sigma = np.maximum(erb_octaves(frequencies) * float(width_scale), 1e-3)
    distance = np.log2(frequencies[np.newaxis, :] / frequencies[:, np.newaxis])
    weights = np.exp(-0.5 * (distance / sigma[:, np.newaxis]) ** 2)
    weights /= np.sum(weights, axis=1, keepdims=True)
    if not peak_weighted:
        return weights @ curve
    amplitude = 10.0 ** (curve / 20.0)
    averaged = weights @ (amplitude ** PEAK_WEIGHT_EXPONENT)
    return 20.0 * np.log10(
        np.maximum(averaged, 1e-30) ** (1.0 / PEAK_WEIGHT_EXPONENT)
    )


def snr_uncertainty_db(snr_db: np.ndarray) -> np.ndarray:
    """Largest magnitude error additive noise at this SNR can cause."""
    return 20.0 * np.log10(1.0 + 10.0 ** (-np.asarray(snr_db, dtype=float) / 20.0))


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
    excluded_sweeps: int, snr_bands_db: dict | None = None,
) -> dict:
    failures: list[str] = []
    warnings: list[str] = []
    guidance: list[str] = []
    snr_bands_db = snr_bands_db or {}

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
    mid_snr = snr_bands_db.get("snr_mid_db")
    if mid_snr is not None and mid_snr < 20.0:
        warnings.append(
            f"Mid-band signal-to-noise ratio is low ({mid_snr:.1f} dB); "
            "corrections shrink where the noise floor is close."
        )
        guidance.append("Reduce background noise or raise the level, then measure again.")
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
            **{key: round(float(value), 2) for key, value in snr_bands_db.items()},
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


SNR_BANDS_HZ = {
    "snr_low_db": (80.0, 250.0),
    "snr_mid_db": (250.0, 4_000.0),
    "snr_high_db": (4_000.0, 16_000.0),
}


def _snr_bands(frequencies: np.ndarray, snr_curves: list[np.ndarray]) -> dict:
    """Worst channel's median signal-to-noise ratio in three bands."""
    if not snr_curves:
        return {}
    stack = np.vstack(snr_curves)
    bands = {}
    for key, (low, high) in SNR_BANDS_HZ.items():
        band = (frequencies >= low) & (frequencies < high)
        if np.any(band):
            bands[key] = float(np.min(np.median(stack[:, band], axis=1)))
    return bands


def analyse_capture(
    capture: np.ndarray,
    schedule: list[dict],
    spec: SweepSpec,
    *,
    record_lead_seconds: float,
    internal_mic: bool,
    calibration: dict | None = None,
    gate_cycles: float | None = DEFAULT_GATE_CYCLES,
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
    pre_frames = int(round(GATE_PRE_ARRIVAL_SECONDS * spec.rate))
    segment_frames = pre_frames + sweep.size + tail_frames

    # The noise floor is the room sound between sweeps, put through the same
    # deconvolution and gate as the sweeps and read at the same position.
    _, noise_floor, _ = regularized_response(
        silence_buffer(capture, starts, spec, record_lead_seconds, segment_frames),
        sweep, spec, frequencies,
        gate_cycles=gate_cycles, nominal_peak_index=pre_frames, locate_peak=False,
    )
    noise_floor = noise_floor + calibration_curve

    per_channel: dict[int, list[dict]] = {0: [], 1: []}
    for event, start, correlation in zip(schedule, starts, correlations):
        segment = _drift_corrected_segment(
            capture, start - pre_frames, segment_frames, clock_ratio
        )
        sweep_part = segment[pre_frames:pre_frames + sweep.size]
        impulse, curve, peak_index = regularized_response(
            segment, sweep, spec, frequencies,
            gate_cycles=gate_cycles, nominal_peak_index=pre_frames,
        )
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
            "snr_db": curve - noise_floor,
            "impulse_peak": round(float(np.max(np.abs(impulse))), 7),
            "direct_sound_ms": round((peak_index - pre_frames) / spec.rate * 1000.0, 3),
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
    channel_snrs = []
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
        snr = np.median(np.vstack([
            item["snr_db"] for item in accepted_items
        ]), axis=0)
        # Repeat spread catches anything that changed between sweeps; the
        # noise floor catches what is wrong in every sweep the same way.
        uncertainty = np.hypot(
            (np.max(accepted_stack, axis=0) - np.min(accepted_stack, axis=0)) / 2.0,
            snr_uncertainty_db(snr),
        )
        channel_curves.append(aggregate)
        channel_snrs.append(snr)
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
            public = {
                key: value for key, value in item.items()
                if key not in ("response_db", "snr_db")
            }
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
            "snr_db": np.round(snr, 3).tolist(),
            "sweeps": public_sweeps,
        })

    combined = np.mean(np.vstack(channel_curves), axis=0)
    snr_bands = _snr_bands(frequencies, channel_snrs)
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
        snr_bands_db=snr_bands,
    )
    _add_measurement_level_guidance(quality, max(accepted_peaks))
    return {
        "method": "repeated-exponential-sine-sweep",
        "rate_hz": spec.rate,
        "gate": {
            "method": "frequency-dependent window" if gate_cycles else "none",
            "cycles": gate_cycles,
            "pre_arrival_seconds": GATE_PRE_ARRIVAL_SECONDS,
        },
        "noise_floor_db": np.round(noise_floor, 3).tolist(),
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
            "snr_db": np.round(np.max(np.vstack([
                np.asarray(source.get("snr_db", np.zeros(frequencies.size)), dtype=float)
                for source in sources
            ]), axis=0), 3).tolist(),
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
    # The best microphone sets the usable signal-to-noise ratio, because the
    # combination is a median across microphones, not a sum of their noise.
    snr_bands = {
        key: max(quality["metrics"][key] for quality in used_qualities)
        for key in SNR_BANDS_HZ
        if all(key in quality["metrics"] for quality in used_qualities)
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
        snr_bands_db=snr_bands,
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
        "gate": measurements[0].get("gate"),
        # The quietest microphone's floor, level-aligned like its response.
        "noise_floor_db": np.round(np.min(np.vstack([
            np.asarray(measurements[index].get("noise_floor_db", np.zeros(frequencies.size)), dtype=float)
            + offsets[position]
            for position, index in enumerate(selected)
        ]), axis=0), 3).tolist(),
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


@dataclass(frozen=True)
class LevelSearchPolicy:
    """How the pre-measurement level search steers the sweep level."""

    # Where the microphone peak should land.  -6 dBFS keeps 5 dB of margin
    # below the clipping gate while sitting inside the window that
    # _add_measurement_level_guidance reports as a good level.
    target_peak_dbfs: float = -6.0
    # A probe that is not at least this far above the room noise did not
    # measure anything usable, so its peak cannot be used for planning.
    minimum_prominence_db: float = 6.0
    # Within this distance of the target the level is accepted as final.
    convergence_db: float = 1.5
    # Backing off after clipping is deliberately coarse: once samples have
    # been flattened the true peak is unknown.
    clip_step_db: float = 12.0
    # A probe that clipped proves its own peak reached full scale, so any
    # level within this much of it must overshoot the target as well.
    clip_ceiling_margin_db: float = 6.0
    # A probe that clipped proves its own peak reached full scale, so any
    # level within this much of it must overshoot the target as well.
    clip_ceiling_margin_db: float = 6.0
    # Stepping up when nothing was heard is equally coarse.
    blind_step_db: float = 12.0
    # Room sound recorded before the probe starts.  Music, a call, or typing
    # next to a built-in microphone sits far above this; a quiet room with a
    # fan sits well below it.
    maximum_background_dbfs: float = -30.0
    # A linear speaker/microphone path moves the peak one-for-one with the
    # level.  When a level change of at least this size moves the peak by
    # less than half as much, something else sets the peak: automatic gain
    # control, an overloaded microphone, or other audio.
    level_follow_step_db: float = 3.0


def analyse_level_probe(
    captures: np.ndarray,
    rate: int,
    *,
    background_seconds: float = 0.3,
    block_seconds: float = 0.05,
) -> dict:
    """Summarize a short level-probe recording without relying on timing.

    The recorder starts ``background_seconds`` or more before the probe plays,
    so the opening of the recording is room sound alone and is reported as
    background.  Everything after it is the probe region.  Noise and
    prominence come from short RMS blocks, so a late recorder start or an
    early stop cannot masquerade as a quiet speaker.

    The steering peak is the second-highest block peak of the probe region.
    Using the highest sample would let one keyboard click drive the whole
    measurement down; using only the tonal blocks, as an earlier version did,
    ignored the sweep's own start and stop transients, which sit well above
    the tone and are what actually clips.  The second-highest block keeps
    anything that recurs, including those transients, and discards a lone
    click.  The tonal and transient peaks are reported separately for
    diagnosis.  Peak and clipping span every supplied channel, because one
    clipped microphone channel spoils the measurement, while noise and
    prominence use the best channel, because one working microphone is enough
    to plan the level.
    """
    values = np.asarray(captures, dtype=np.float64)
    if values.ndim == 1:
        values = values[:, np.newaxis]
    frames, channels = values.shape
    block = max(1, int(round(block_seconds * rate)))
    usable = (frames // block) * block
    if usable < 6 * block:
        raise ValueError("The level probe recording is too short to analyse.")
    blocks = values[:usable].reshape((usable // block, block, channels))
    block_rms = np.sqrt(np.mean(blocks * blocks, axis=1))
    block_peak = np.max(np.abs(blocks), axis=1)
    count = blocks.shape[0]
    background_blocks = int(np.clip(round(background_seconds * rate / block), 1, count // 2))
    background_rms = np.sqrt(np.mean(block_rms[:background_blocks] ** 2, axis=0))
    background_peak = np.max(block_peak[:background_blocks], axis=0)

    region_rms = block_rms[background_blocks:]
    region_peak = block_peak[background_blocks:]
    noise = np.percentile(block_rms, 10, axis=0)
    loud = np.percentile(region_rms, 95, axis=0)
    prominences = [dbfs(loud[index]) - dbfs(noise[index]) for index in range(channels)]
    best = int(np.argmax(prominences))

    crest_db = 20.0 * np.log10(region_peak / np.maximum(region_rms, 1e-12))
    tonal = (crest_db <= 12.0) & (region_rms >= 2.0 * noise[np.newaxis, :])
    tonal_peak = float(np.max(region_peak[tonal])) if np.any(tonal) else 0.0
    transient_peak = float(np.max(region_peak[~tonal])) if np.any(~tonal) else 0.0
    loudest_blocks = np.sort(np.max(region_peak, axis=1))[::-1]
    peak = float(loudest_blocks[1] if loudest_blocks.size >= 4 else loudest_blocks[0])
    region = values[background_blocks * block:usable]
    return {
        "peak_dbfs": round(dbfs(peak), 3),
        "tonal_peak_dbfs": round(dbfs(tonal_peak), 3),
        "noise_dbfs": round(dbfs(float(noise[best])), 3),
        "prominence_db": round(float(prominences[best]), 3),
        "clipped_samples": int(np.count_nonzero(np.abs(region) >= 0.999)),
        "background_rms_dbfs": round(dbfs(float(np.max(background_rms))), 3),
        "background_peak_dbfs": round(dbfs(float(np.max(background_peak))), 3),
        "transient_peak_dbfs": round(dbfs(transient_peak), 3),
        "tonal_blocks": int(np.count_nonzero(np.any(tonal, axis=1))),
    }


def plan_probe_level(
    level_dbfs: float,
    probe: dict,
    bounds: tuple[float, float],
    policy: LevelSearchPolicy = LevelSearchPolicy(),
) -> dict:
    """Decide the next sweep level from one probe result."""
    low, high = float(bounds[0]), float(bounds[1])
    at_low = level_dbfs <= low + 1e-9
    at_high = level_dbfs >= high - 1e-9
    if float(probe.get("background_rms_dbfs", -120.0)) > policy.maximum_background_dbfs:
        return {"level_dbfs": level_dbfs, "done": True, "status": "background-too-loud"}
    if probe["clipped_samples"] > 0:
        if at_low:
            return {"level_dbfs": low, "done": True, "status": "clipping-at-minimum-level"}
        return {
            "level_dbfs": max(low, level_dbfs - policy.clip_step_db),
            "done": False,
            "status": "clipped",
        }
    if probe["prominence_db"] < policy.minimum_prominence_db:
        if at_high:
            return {"level_dbfs": high, "done": True, "status": "no-signal"}
        return {
            "level_dbfs": min(high, level_dbfs + policy.blind_step_db),
            "done": False,
            "status": "not-heard",
        }
    # A sine sweep's microphone peak scales one-for-one with its level, so a
    # single proportional step lands near the target; the next probe confirms.
    error = policy.target_peak_dbfs - float(probe["peak_dbfs"])
    proposed = float(np.clip(level_dbfs + error, low, high))
    if abs(proposed - level_dbfs) <= policy.convergence_db:
        if error > policy.convergence_db and proposed >= high - 1e-9:
            status = "limited-by-maximum-level"
        elif error < -policy.convergence_db and proposed <= low + 1e-9:
            status = "limited-by-minimum-level"
        else:
            status = "converged"
        return {"level_dbfs": proposed, "done": True, "status": status}
    return {"level_dbfs": proposed, "done": False, "status": "adjusting"}


def _peak_follows_level(previous: dict, current: dict, policy: LevelSearchPolicy) -> bool:
    """True unless two clean probes show the peak ignoring a level change."""
    for probe in (previous, current):
        if probe["clipped_samples"] > 0 or probe["prominence_db"] < policy.minimum_prominence_db:
            return True
    level_change = current["level_dbfs"] - previous["level_dbfs"]
    if abs(level_change) < policy.level_follow_step_db:
        return True
    peak_change = current["peak_dbfs"] - previous["peak_dbfs"]
    return abs(peak_change) >= 0.5 * abs(level_change)


def search_measurement_level(
    run_probe,
    *,
    start_level_dbfs: float,
    bounds: tuple[float, float],
    policy: LevelSearchPolicy = LevelSearchPolicy(),
    attempts: int = 3,
) -> dict:
    """Find a sweep level that lands the microphone peak near the target.

    ``run_probe(level_dbfs)`` plays a short probe at that level and returns
    the dict produced by :func:`analyse_level_probe`.  The search starts
    quietly and moves in proportional steps, so a hot microphone is caught
    before anything clips and a quiet one is raised before the long sweeps.
    It stops early when the room is too loud before the probe starts, or when
    the peak stops following the level, because no level can rescue either.
    """
    low, high = float(bounds[0]), float(bounds[1])
    ceiling = high
    level = float(np.clip(start_level_dbfs, low, high))
    trace: list[dict] = []
    plan = {"level_dbfs": level, "done": False, "status": "not-run"}
    for _ in range(max(1, int(attempts))):
        probe = run_probe(level)
        plan = plan_probe_level(level, probe, (low, ceiling), policy)
        entry = {"level_dbfs": round(level, 2), **probe, "status": plan["status"]}
        if trace and not plan["done"] and not _peak_follows_level(trace[-1], entry, policy):
            plan = {
                "level_dbfs": min(level, float(trace[-1]["level_dbfs"])),
                "done": True,
                "status": "level-independent",
            }
            entry["status"] = plan["status"]
        trace.append(entry)
        if probe["clipped_samples"] > 0:
            # This level reached full scale, so the target cannot be met at it
            # or anywhere near it.  Nothing later may climb back up here.
            ceiling = max(low, min(ceiling, level - policy.clip_ceiling_margin_db))
        level = float(np.clip(plan["level_dbfs"], low, ceiling))
        if plan["done"]:
            break
    status = plan["status"]
    if not plan["done"]:
        # The search ran out of probes.  Its last proposal was never played,
        # and proposing is exactly what went wrong, so fall back to the best
        # level that was actually measured.
        clean = [
            item for item in trace
            if item["clipped_samples"] == 0
            and item["prominence_db"] >= policy.minimum_prominence_db
        ]
        if clean:
            best = min(clean, key=lambda item: abs(item["peak_dbfs"] - policy.target_peak_dbfs))
            level = float(best["level_dbfs"])
            status = "best-tested"
        else:
            level = low
            status = "unsettled"
    return {
        "target_peak_dbfs": policy.target_peak_dbfs,
        "level_bounds_dbfs": [round(low, 2), round(high, 2)],
        "level_ceiling_dbfs": round(ceiling, 2),
        "selected_level_dbfs": round(level, 2),
        "status": status,
        "confirmed": status == "converged" or status.startswith("limited-by"),
        "attempts": trace,
    }


# A search that ends in one of these states cannot produce a usable
# measurement at any level, so the caller should stop and show the advice.
LEVEL_SEARCH_ABORT_STATUSES = ("no-signal", "background-too-loud", "level-independent")


def level_after_clipping(
    level_dbfs: float,
    peak_dbfs: float,
    low_bound_dbfs: float,
    policy: LevelSearchPolicy = LevelSearchPolicy(),
) -> float:
    """A level to retry a capture at after the microphone clipped.

    A clipped capture only proves the peak reached full scale, never how far
    past it went, so the retry drops by the shortfall to the target plus the
    same margin a clipped probe imposes.
    """
    overshoot = max(0.0, float(peak_dbfs) - policy.target_peak_dbfs)
    return float(max(low_bound_dbfs, level_dbfs - max(policy.clip_ceiling_margin_db, overshoot)))


def level_search_advice(search: dict) -> tuple[list[str], list[str]]:
    """Return (warnings, guidance) for a level search that could not settle."""
    status = search.get("status", "")
    level = float(search.get("selected_level_dbfs", 0.0))
    attempts = search.get("attempts") or [{}]
    last = attempts[-1]
    if status == "background-too-loud":
        return (
            [f"Background sound at the microphone is too loud for a measurement "
             f"({float(last.get('background_rms_dbfs', 0.0)):.1f} dBFS RMS before the probe started)."],
            ["Pause other audio and calls, stop typing, and keep the room quiet, then measure again."],
        )
    if status == "level-independent":
        return (
            ["The microphone level did not follow the sweep level between probes."],
            ["Disable microphone automatic gain control, pause other audio, and lower the "
             "microphone gain if it is overloaded, then measure again."],
        )
    if status == "no-signal":
        return (
            ["The microphone did not pick up the level probe even at the loudest allowed sweep level."],
            ["Check that the selected speaker and microphone are unmuted and raise the "
             "hardware volume, then measure again."],
        )
    if status == "limited-by-maximum-level":
        return (
            [f"The level search stopped at the loudest allowed sweep level ({level:+.1f} dBFS) "
             "with a low microphone signal."],
            ["Raise the speaker hardware volume or the microphone input gain, then measure again."],
        )
    if status == "retried-after-clipping":
        return (
            [f"The first capture clipped, so it was measured again at {level:+.1f} dBFS."],
            [],
        )
    if status in ("limited-by-minimum-level", "clipping-at-minimum-level"):
        return (
            [f"The level search stopped at the quietest allowed sweep level ({level:+.1f} dBFS) "
             "with a hot microphone signal."],
            ["Lower the microphone input gain or the speaker hardware volume, then measure again."],
        )
    if status == "best-tested":
        return (
            [f"The level search did not settle, so the best level it actually "
             f"measured ({level:+.1f} dBFS) was used."],
            ["Keep the room quiet during the level check for a closer level."],
        )
    if status in ("adjusting", "clipped", "not-heard", "unsettled"):
        return (
            [f"The level search did not settle; the sweeps used {level:+.1f} dBFS unconfirmed."],
            ["Disable microphone automatic gain control and keep the room quiet, then measure again."],
        )
    return [], []
