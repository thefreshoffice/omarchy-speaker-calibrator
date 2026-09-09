#!/usr/bin/python3
"""Adaptive, subtractive-first parametric EQ for speaker calibration."""

from __future__ import annotations

import math

try:
    import numpy as np
    from scipy.ndimage import gaussian_filter1d
    from scipy.optimize import minimize
except ImportError as error:  # pragma: no cover
    raise SystemExit(
        f"Optimizer import failed: {error}. The panel expects Arch's "
        "python-numpy and python-scipy packages."
    ) from error

# The one thing the optimizer borrows from the measurement side: how much a
# given signal-to-noise ratio can move a magnitude reading.  Shared rather
# than restated so the two cannot drift apart.
from calibration_dsp import (  # noqa: E402
    PEAK_WEIGHT_EXPONENT,
    erb_octaves,
    perceptual_smooth,
    snr_uncertainty_db,
)


DIAGNOSTIC_CENTERS = np.asarray(
    [160.0, 250.0, 400.0, 630.0, 1000.0, 1600.0,
     2500.0, 4000.0, 6300.0, 10000.0]
)
# Depth limits apply to the whole correction at a frequency, not to each
# filter.  Stacked shallow filters used to add up to the same depth anyway;
# one deep filter does the same job with less interaction between sections,
# so a single section may go as deep as the total limit allows.
CUT_LIMIT_DB = -15.0
PER_FILTER_CUT_LIMIT_DB = CUT_LIMIT_DB
INTERNAL_CUT_ANCHORS_HZ = np.asarray(
    [160.0, 250.0, 400.0, 630.0, 1000.0, 1600.0,
     2500.0, 4000.0, 6300.0, 10000.0]
)
# A built-in microphone's own response is unknown at the band edges, so the
# whole correction stays shallower there.
INTERNAL_CUT_LIMITS_DB = np.asarray(
    [-6.0, -8.0, -10.0, -13.0, -15.0, -15.0, -15.0, -15.0, -13.0, -8.0]
)
# Shelves handle a residual that stays high all the way to a band edge, so a
# tilt costs one section instead of several overlapping peaking filters.
# They are cut-only: a shelf boost would extend below the knee or above the
# measured band, exactly where boosting is forbidden.
SHELF_Q_BOUNDS = (0.5, 1.0)
LOW_SHELF_CORNERS_HZ = (200.0, 300.0, 450.0)
HIGH_SHELF_CORNERS_HZ = (2500.0, 4000.0, 6300.0)


def _log_interpolate(frequencies: np.ndarray, anchors_hz, anchors_db) -> np.ndarray:
    return np.interp(
        np.log(frequencies), np.log(np.asarray(anchors_hz, dtype=float)),
        np.asarray(anchors_db, dtype=float),
    )


VOICINGS = ("neutral", "warm")
# Bass rise of the target: where it starts, how steep it is, and its cap.
BASS_RISE = {
    "neutral": (250.0, 2.0, 3.5),
    "warm": (250.0, 2.0, 3.5),
}

# How much of the loudness lost to the cuts is added back as input gain.
LOUDNESS_MODES = {"protected": 0.0, "balanced": 0.5, "matched": 1.0}
# Beyond this the limiter would be working on most peaks of loud music.  With
# the 1 dB limiter margin this also bounds the electrical drive in any band
# to 5 dB above the uncorrected speaker, whatever else is selected.
MAKEUP_CAP_DB = 6.0

# Protective high-pass.  Below the point where a speaker stops keeping up, the
# cone still travels as far as ever while producing almost nothing, so that
# content costs excursion, distortion, and headroom for no sound.  The corner
# is put where this measurement says that happens instead of at a fixed
# frequency that may sit two octaves below the real limit.
HIGHPASS_Q = 0.707
HIGHPASS_BOUNDS_HZ = (50.0, 200.0)
HIGHPASS_SEARCH_CEILING_HZ = 400.0
# How far short of the target the speaker must fall to count as finished.
KNEE_SHORTFALL_DB = 15.0
# A second section doubles the slope.  That is free where nothing is audible
# and heavy-handed where something still is, so it is only used down low.
HIGHPASS_TWO_STAGE_BELOW_HZ = 100.0

# "Full" bass is a low shelf whose corner sits at the measured knee, where
# the speaker stops keeping up with its midband, so the lift lands where the
# driver still turns voltage into sound.  It is paid for by input trim like
# any other positive correction.
BASS_MODES = ("normal", "full")
BASS_SHELF_DB = 3.0
BASS_SHELF_Q = 0.707
BASS_SHELF_CORNER_HZ = (150.0, 600.0)
# A low shelf reaches its full lift below its corner, so a corner sitting on
# the high-pass would put the whole boost in the band the high-pass has just
# removed and the option would do nothing.  Keep it clear of it.
BASS_SHELF_ABOVE_HIGHPASS = 2.5


def pleasant_in_room_target(frequencies: np.ndarray, voicing: str) -> np.ndarray:
    """Return the broad in-room loudspeaker target relative to the midband."""
    frequencies = np.asarray(frequencies, dtype=float)
    target = np.zeros_like(frequencies)
    knee, slope, cap = BASS_RISE.get(voicing, BASS_RISE["neutral"])
    low = frequencies < knee
    high = frequencies > 2000.0
    target[low] = np.minimum(cap, slope * np.log2(knee / frequencies[low]))
    target[high] = np.maximum(-4.5, -1.5 * np.log2(frequencies[high] / 2000.0))
    if voicing == "warm":
        target += _log_interpolate(
            frequencies,
            [80, 400, 800, 1600, 3150, 6300, 10000, 16000],
            [0.0, 0.0, -0.25, -0.7, -1.2, -1.35, -1.1, -0.7],
        )
    return target


def a_weighting_db(frequencies: np.ndarray) -> np.ndarray:
    """IEC 61672 A-weighting, in dB."""
    f2 = np.asarray(frequencies, dtype=float) ** 2
    ra = (12194.0 ** 2 * f2 ** 2) / (
        (f2 + 20.6 ** 2)
        * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
        * (f2 + 12194.0 ** 2)
    )
    return 20.0 * np.log10(np.maximum(ra, 1e-12)) + 2.0


def pink_loudness_db(
    frequencies: np.ndarray, response_db: np.ndarray,
    *, low_hz: float = 100.0, high_hz: float = 10_000.0,
) -> float:
    """A-weighted level of pink noise played through this response.

    The analysis grid is log-spaced, so an unweighted mean over its bins is
    already pink (equal energy per octave).  Only the difference between two
    responses is used, so the absolute scale does not matter.
    """
    frequencies = np.asarray(frequencies, dtype=float)
    band = (frequencies >= low_hz) & (frequencies <= high_hz)
    weighted = np.asarray(response_db, dtype=float)[band] + a_weighting_db(frequencies[band])
    return 10.0 * math.log10(float(np.mean(10.0 ** (weighted / 10.0))))


def loudness_makeup_db(loudness_loss_db: float, loudness: str) -> float:
    """Input gain that pays back part of the loudness the cuts removed.

    The share applies to the capped loss, so "balanced" is always a real
    step between "protected" and "matched", even when the loss is large.
    """
    fraction = LOUDNESS_MODES.get(loudness, 0.0)
    return round(min(MAKEUP_CAP_DB, max(0.0, loudness_loss_db)) * fraction, 2)


def _weighted_quantile(values: np.ndarray, weights: np.ndarray, quantile: float) -> float:
    order = np.argsort(values)
    ordered_values = values[order]
    ordered_weights = weights[order]
    cumulative = np.cumsum(ordered_weights)
    if cumulative[-1] <= 0:
        return float(np.median(values))
    location = quantile * cumulative[-1]
    return float(ordered_values[min(np.searchsorted(cumulative, location), values.size - 1)])


def _peaking_response_db(
    frequencies: np.ndarray, center: float, q: float, gain_db: float, rate: int
) -> np.ndarray:
    """Exact RBJ peaking-biquad magnitude response."""
    amplitude = 10.0 ** (gain_db / 40.0)
    omega0 = 2.0 * math.pi * center / rate
    alpha = math.sin(omega0) / (2.0 * q)
    cos0 = math.cos(omega0)
    b0 = 1.0 + alpha * amplitude
    b1 = -2.0 * cos0
    b2 = 1.0 - alpha * amplitude
    a0 = 1.0 + alpha / amplitude
    a1 = -2.0 * cos0
    a2 = 1.0 - alpha / amplitude
    omega = 2.0 * math.pi * frequencies / rate
    z1 = np.exp(-1j * omega)
    z2 = z1 * z1
    numerator = b0 + b1 * z1 + b2 * z2
    denominator = a0 + a1 * z1 + a2 * z2
    return 20.0 * np.log10(np.maximum(np.abs(numerator / denominator), 1e-12))


def _lowshelf_response_db(
    frequencies: np.ndarray, corner: float, q: float, gain_db: float, rate: int
) -> np.ndarray:
    """Exact RBJ low-shelf magnitude response."""
    amplitude = 10.0 ** (gain_db / 40.0)
    omega0 = 2.0 * math.pi * corner / rate
    alpha = math.sin(omega0) / (2.0 * q)
    cos0 = math.cos(omega0)
    root_term = 2.0 * math.sqrt(amplitude) * alpha
    b0 = amplitude * ((amplitude + 1.0) - (amplitude - 1.0) * cos0 + root_term)
    b1 = 2.0 * amplitude * ((amplitude - 1.0) - (amplitude + 1.0) * cos0)
    b2 = amplitude * ((amplitude + 1.0) - (amplitude - 1.0) * cos0 - root_term)
    a0 = (amplitude + 1.0) + (amplitude - 1.0) * cos0 + root_term
    a1 = -2.0 * ((amplitude - 1.0) + (amplitude + 1.0) * cos0)
    a2 = (amplitude + 1.0) + (amplitude - 1.0) * cos0 - root_term
    omega = 2.0 * math.pi * frequencies / rate
    z1 = np.exp(-1j * omega)
    z2 = z1 * z1
    numerator = b0 + b1 * z1 + b2 * z2
    denominator = a0 + a1 * z1 + a2 * z2
    return 20.0 * np.log10(np.maximum(np.abs(numerator / denominator), 1e-12))


def _highshelf_response_db(
    frequencies: np.ndarray, corner: float, q: float, gain_db: float, rate: int
) -> np.ndarray:
    """Exact RBJ high-shelf magnitude response."""
    amplitude = 10.0 ** (gain_db / 40.0)
    omega0 = 2.0 * math.pi * corner / rate
    alpha = math.sin(omega0) / (2.0 * q)
    cos0 = math.cos(omega0)
    root_term = 2.0 * math.sqrt(amplitude) * alpha
    b0 = amplitude * ((amplitude + 1.0) + (amplitude - 1.0) * cos0 + root_term)
    b1 = -2.0 * amplitude * ((amplitude - 1.0) + (amplitude + 1.0) * cos0)
    b2 = amplitude * ((amplitude + 1.0) + (amplitude - 1.0) * cos0 - root_term)
    a0 = (amplitude + 1.0) - (amplitude - 1.0) * cos0 + root_term
    a1 = 2.0 * ((amplitude - 1.0) - (amplitude + 1.0) * cos0)
    a2 = (amplitude + 1.0) - (amplitude - 1.0) * cos0 - root_term
    omega = 2.0 * math.pi * frequencies / rate
    z1 = np.exp(-1j * omega)
    z2 = z1 * z1
    numerator = b0 + b1 * z1 + b2 * z2
    denominator = a0 + a1 * z1 + a2 * z2
    return 20.0 * np.log10(np.maximum(np.abs(numerator / denominator), 1e-12))


def _section_response_db(
    frequencies: np.ndarray, shape: str, center: float, q: float, gain_db: float, rate: int
) -> np.ndarray:
    if shape == "lowshelf":
        return _lowshelf_response_db(frequencies, center, q, gain_db, rate)
    if shape == "highshelf":
        return _highshelf_response_db(frequencies, center, q, gain_db, rate)
    return _peaking_response_db(frequencies, center, q, gain_db, rate)


# Bands the verification result is reported in.  Wide enough that one noisy
# analysis bin cannot swing a band, narrow enough to point at what is wrong.
VERIFICATION_BANDS_HZ = (
    ("bass", 80.0, 250.0),
    ("low mid", 250.0, 800.0),
    ("mid", 800.0, 2500.0),
    ("treble", 2500.0, 8000.0),
    ("air", 8000.0, 16000.0),
)
# A correction this far from its prediction means something other than the
# filters is shaping the sound: the wrong device, a moved microphone, or a
# level high enough to be working the limiter.
VERIFICATION_FAIL_DB = 4.0
VERIFICATION_WARN_DB = 2.0
# Below this the verification microphone is hearing room noise, not the sweep.
VERIFICATION_MIN_SNR_DB = 6.0
# Two checks of the same profile, taken minutes apart on a built-in
# microphone array, agreed to about this much.  Nothing smaller than it can be
# told apart from the check repeating itself.
CHECK_REPEATABILITY_DB = 1.0
# The most the raw estimate may be moved by one round of refinement.
REFINEMENT_LIMIT_DB = 8.0

# The two curves are measured and smoothed differently, so "no worse than the
# raw speaker" needs a margin; without one, a correction that changed nothing
# could be reported as a regression on rounding alone.
VERIFICATION_REGRESSION_DB = 0.5


def _aligned(curve: np.ndarray, frequencies: np.ndarray, usable: np.ndarray) -> np.ndarray:
    """Curve shifted so its trusted midband median sits at zero."""
    band = usable & (frequencies >= 250.0) & (frequencies <= 2000.0)
    if not np.any(band):
        band = usable if np.any(usable) else np.ones(frequencies.size, dtype=bool)
    return np.asarray(curve, dtype=float) - float(np.median(np.asarray(curve, dtype=float)[band]))


def _band_rms(frequencies: np.ndarray, error: np.ndarray, usable: np.ndarray) -> dict:
    bands = {}
    for name, low, high in VERIFICATION_BANDS_HZ:
        inside = usable & (frequencies >= low) & (frequencies < high)
        if np.count_nonzero(inside) >= 3:
            bands[name] = round(float(np.sqrt(np.mean(error[inside] ** 2))), 2)
    return bands


def verification_report(
    frequencies: np.ndarray,
    verified_db: np.ndarray,
    snr_db: np.ndarray | None,
    fit: dict,
    fit_frequencies: np.ndarray,
) -> dict:
    """Compare a measurement taken through the corrected output with the fit.

    Two questions are answered separately.  Did the filters do what the fit
    said they would, which tests the model?  And is the result closer to the
    target than the raw speaker was, which tests whether any of it was worth
    doing?  Both curves are level-aligned first, because the sweep level and
    the input trim differ between the two measurements and only shape matters.
    """
    frequencies = np.asarray(frequencies, dtype=float)
    fit_frequencies = np.asarray(fit_frequencies, dtype=float)

    def onto_grid(values):
        return np.interp(
            np.log(frequencies), np.log(fit_frequencies), np.asarray(values, dtype=float)
        )

    predicted = onto_grid(fit["predicted_response_db"])
    original = onto_grid(fit["measured_smoothed_db"])
    target = onto_grid(fit["target"]["aligned_db"])
    # Smoothed exactly as the curve it is compared against was.
    verified = perceptual_smooth(frequencies, verified_db)

    # The high-passed region is deliberately empty, so it carries no
    # information about whether the filters landed.
    highpass = fit.get("highpass") or {}
    floor_hz = max(160.0, float(highpass.get("frequency_hz", 55.0)))
    usable = (frequencies >= floor_hz) & (frequencies <= 10_000.0)
    if snr_db is not None:
        usable = usable & (np.asarray(snr_db, dtype=float) >= VERIFICATION_MIN_SNR_DB)
    if np.count_nonzero(usable) < 8:
        usable = (frequencies >= floor_hz) & (frequencies <= 10_000.0)

    verified_aligned = _aligned(verified, frequencies, usable)
    predicted_aligned = _aligned(predicted, frequencies, usable)
    original_aligned = _aligned(original, frequencies, usable)
    target_aligned = _aligned(target, frequencies, usable)

    model_error = verified_aligned - predicted_aligned
    worst_index = int(np.argmax(np.abs(np.where(usable, model_error, 0.0))))
    model_rms = float(np.sqrt(np.mean(model_error[usable] ** 2)))

    def target_rms(curve):
        return round(float(np.sqrt(np.mean((curve[usable] - target_aligned[usable]) ** 2))), 2)

    before = target_rms(original_aligned)
    after = target_rms(verified_aligned)
    expected = target_rms(predicted_aligned)

    notes = []
    verdict = "pass"
    if model_rms > VERIFICATION_FAIL_DB:
        verdict = "fail"
        notes.append(
            f"The corrected output is {model_rms:.1f} dB away from what the filters "
            "should have produced."
        )
        notes.append(
            "Check that the calibrated output is the one being measured, that the "
            "microphone has not moved, and that nothing else is playing."
        )
    elif model_rms > VERIFICATION_WARN_DB:
        verdict = "warning"
        notes.append(
            f"The corrected output differs from the plan by {model_rms:.1f} dB, more "
            "than measurement noise alone explains."
        )
    if after > before + VERIFICATION_REGRESSION_DB:
        verdict = "fail"
        notes.append(
            f"Measured against the target the corrected speaker is worse than the raw "
            f"one ({before:.1f} dB before, {after:.1f} dB after)."
        )
    elif after > expected + VERIFICATION_WARN_DB:
        if verdict == "pass":
            verdict = "warning"
        notes.append(
            f"The correction improved less than expected ({expected:.1f} dB planned, "
            f"{after:.1f} dB measured)."
        )
    # What the check itself could not resolve, used to decide how much of the
    # difference is worth believing when it is fed back in.
    uncertainty = np.hypot(
        snr_uncertainty_db(snr_db) if snr_db is not None
        else np.zeros(frequencies.size),
        CHECK_REPEATABILITY_DB,
    )
    return {
        "verdict": verdict,
        "notes": notes,
        "analysis_band_hz": [round(floor_hz, 1), 10_000.0],
        "usable": usable.tolist(),
        "uncertainty_db": np.round(uncertainty, 3).tolist(),
        "analysed_points": int(np.count_nonzero(usable)),
        "model_error_db": {
            "rms": round(model_rms, 2),
            "worst": round(float(model_error[worst_index]), 2),
            "worst_hz": round(float(frequencies[worst_index]), 1),
            "bands": _band_rms(frequencies, model_error, usable),
        },
        "target_error_db": {"before": before, "planned": expected, "measured": after},
        "frequency_hz": np.round(frequencies, 3).tolist(),
        "verified_db": np.round(verified_aligned, 3).tolist(),
        "predicted_db": np.round(predicted_aligned, 3).tolist(),
        "target_db": np.round(target_aligned, 3).tolist(),
        "original_db": np.round(original_aligned, 3).tolist(),
    }


def refinement_residual(verification: dict, frequencies: np.ndarray) -> np.ndarray:
    """How wrong the raw measurement was, according to the check.

    If the speaker was measured through a correction whose response is known,
    then whatever the result differs from the prediction by is what the raw
    measurement got wrong, so adding it back gives a better estimate of the
    bare speaker.  The difference is shrunk toward zero where it is comparable
    with what the check itself could resolve, so that repeating the check
    cannot inject its own noise into the next fit.
    """
    frequencies = np.asarray(frequencies, dtype=float)
    check_grid = np.asarray(verification["frequency_hz"], dtype=float)
    difference = (
        np.asarray(verification["verified_db"], dtype=float)
        - np.asarray(verification["predicted_db"], dtype=float)
    )
    uncertainty = np.asarray(
        verification.get("uncertainty_db") or np.full(check_grid.size, CHECK_REPEATABILITY_DB),
        dtype=float,
    )
    usable = np.asarray(
        verification.get("usable") or np.ones(check_grid.size, dtype=bool), dtype=bool
    )
    shrink = difference ** 2 / (difference ** 2 + np.maximum(uncertainty, 1e-6) ** 2)
    applied = np.clip(difference * shrink, -REFINEMENT_LIMIT_DB, REFINEMENT_LIMIT_DB)
    applied = np.where(usable, applied, 0.0)
    # A difference, not a response: a plain mean is the honest one here.
    applied = perceptual_smooth(check_grid, applied, peak_weighted=False)
    # Outside the band the check could judge, the earlier measurement stands.
    edges = np.flatnonzero(usable)
    if edges.size:
        applied[check_grid < check_grid[edges[0]]] = 0.0
        applied[check_grid > check_grid[edges[-1]]] = 0.0
    else:
        applied[:] = 0.0
    return np.interp(np.log(frequencies), np.log(check_grid), applied)


def apply_refinement(measurement: dict, residual: np.ndarray) -> dict:
    """A measurement moved by ``residual``, as if the speaker had measured so.

    Every curve the optimizer reads moves together, including the per-repeat
    curves it cross-validates on, so the held-out test stays consistent.  The
    residual is level-neutral by construction, so refining never drifts the
    overall level.
    """
    residual = np.asarray(residual, dtype=float)
    refined = dict(measurement)
    refined["level_dbfs"] = np.round(
        np.asarray(measurement["level_dbfs"], dtype=float) + residual, 3
    ).tolist()
    channels = []
    for channel in measurement.get("channels", []):
        moved = dict(channel)
        moved["response_db"] = np.round(
            np.asarray(channel["response_db"], dtype=float) + residual, 3
        ).tolist()
        if "uncertainty_db" in channel:
            # The check's own spread is now part of this estimate.
            moved["uncertainty_db"] = np.round(np.hypot(
                np.asarray(channel["uncertainty_db"], dtype=float),
                CHECK_REPEATABILITY_DB,
            ), 3).tolist()
        channels.append(moved)
    refined["channels"] = channels
    refined["validation_curves"] = [
        dict(curve, response_db=np.round(
            np.asarray(curve["response_db"], dtype=float) + residual, 3
        ).tolist())
        for curve in measurement.get("validation_curves", [])
    ]
    return refined


def estimate_highpass(
    frequencies: np.ndarray, measured_db: np.ndarray, target_db: np.ndarray
) -> dict:
    """Corner and stage count for the protective high-pass.

    The knee is the highest frequency below the search ceiling where the
    speaker falls more than ``KNEE_SHORTFALL_DB`` short of the target it is
    being fitted to.  Measuring the shortfall against the target rather than
    against a passband average keeps one loud resonance from dragging the
    estimate upward, which matters on laptop speakers whose response is mostly
    one big peak.  Recording noise can only raise the measured level, so a
    noisy measurement understates the shortfall and lowers the corner, which is
    the safe direction to be wrong in.
    """
    frequencies = np.asarray(frequencies, dtype=float)
    shortfall = np.asarray(target_db, dtype=float) - np.asarray(measured_db, dtype=float)
    failing = (frequencies <= HIGHPASS_SEARCH_CEILING_HZ) & (shortfall >= KNEE_SHORTFALL_DB)
    knee = float(np.max(frequencies[failing])) if np.any(failing) else 0.0
    corner = float(np.clip(knee, HIGHPASS_BOUNDS_HZ[0], HIGHPASS_BOUNDS_HZ[1]))
    return {
        "frequency_hz": round(corner, 1),
        "q": HIGHPASS_Q,
        "stages": 2 if corner <= HIGHPASS_TWO_STAGE_BELOW_HZ else 1,
        "knee_hz": round(knee, 1) if knee > 0.0 else None,
        "shortfall_db": KNEE_SHORTFALL_DB,
        "bounds_hz": list(HIGHPASS_BOUNDS_HZ),
    }


def bass_shelf(knee_hz: float, bass: str, highpass_hz: float = 0.0) -> dict | None:
    """The full-bass shelf for this measurement, or None for normal bass."""
    if bass != "full":
        return None
    corner = max(float(knee_hz), BASS_SHELF_ABOVE_HIGHPASS * float(highpass_hz))
    corner = float(np.clip(corner, BASS_SHELF_CORNER_HZ[0], BASS_SHELF_CORNER_HZ[1]))
    return {"frequency_hz": round(corner, 1), "q": BASS_SHELF_Q, "gain_db": BASS_SHELF_DB}


def _highpass_response_db(
    frequencies: np.ndarray, cutoff: float, q: float, rate: int
) -> np.ndarray:
    """Exact RBJ high-pass magnitude response."""
    omega0 = 2.0 * math.pi * cutoff / rate
    alpha = math.sin(omega0) / (2.0 * q)
    cos0 = math.cos(omega0)
    b0 = (1.0 + cos0) / 2.0
    b1 = -(1.0 + cos0)
    b2 = b0
    a0 = 1.0 + alpha
    a1 = -2.0 * cos0
    a2 = 1.0 - alpha
    omega = 2.0 * math.pi * frequencies / rate
    z1 = np.exp(-1j * omega)
    z2 = z1 * z1
    numerator = b0 + b1 * z1 + b2 * z2
    denominator = a0 + a1 * z1 + a2 * z2
    return 20.0 * np.log10(np.maximum(np.abs(numerator / denominator), 1e-12))


def filter_response_db(
    frequencies: np.ndarray,
    gains_db: np.ndarray,
    rate: int = 48_000,
    *,
    centers_hz: np.ndarray | list[float] | None = None,
    q_values: np.ndarray | list[float] | None = None,
    shapes: list[str] | None = None,
) -> np.ndarray:
    """Return the summed response of any number of parametric sections."""
    frequencies = np.asarray(frequencies, dtype=float)
    gains = np.asarray(gains_db, dtype=float)
    centers = np.asarray(centers_hz if centers_hz is not None else [], dtype=float)
    q_array = np.asarray(q_values if q_values is not None else [], dtype=float)
    if not (centers.size == q_array.size == gains.size):
        raise ValueError("Parametric filter frequency, Q, and gain arrays must match.")
    shape_list = list(shapes) if shapes is not None else ["peaking"] * gains.size
    if len(shape_list) != gains.size:
        raise ValueError("Filter shapes must match the number of filters.")
    response = np.zeros_like(frequencies)
    for center, q, gain, shape in zip(centers, q_array, gains, shape_list):
        response += _section_response_db(frequencies, shape, center, q, float(gain), rate)
    return response


def _measurement_confidence(measurement: dict) -> tuple[np.ndarray, np.ndarray]:
    frequencies = np.asarray(measurement["frequency_hz"], dtype=float)
    channels = measurement.get("channels", [])
    uncertainty_curves = [
        np.asarray(channel.get("uncertainty_db", np.zeros(frequencies.size)), dtype=float)
        for channel in channels
    ]
    uncertainty = (
        np.max(np.vstack(uncertainty_curves), axis=0)
        if uncertainty_curves else np.full(frequencies.size, 3.0)
    )
    confidence = 1.0 / (1.0 + (uncertainty / 1.25) ** 2)
    if len(channels) >= 2:
        left = np.asarray(channels[0]["response_db"], dtype=float)
        right = np.asarray(channels[1]["response_db"], dtype=float)
        band = (frequencies >= 250.0) & (frequencies <= 2000.0)
        mismatch = (left - np.median(left[band])) - (right - np.median(right[band]))
        confidence *= 1.0 / (1.0 + (np.abs(mismatch) / 4.0) ** 2)
    return np.clip(confidence, 0.05, 1.0), uncertainty


def _safe_boost_floor(
    frequencies: np.ndarray, measured: np.ndarray, confidence: np.ndarray
) -> float:
    mid = (frequencies >= 300.0) & (frequencies <= 1600.0)
    mid_reference = float(np.percentile(measured[mid], 65))
    candidates = np.where(
        (frequencies >= 140.0) & (frequencies <= 1000.0)
        & (measured >= mid_reference - 10.0) & (confidence >= 0.55)
    )[0]
    return float(max(160.0, frequencies[candidates[0]])) if candidates.size else 1000.0


def _cut_limit_at(center: float, internal_mic: bool) -> float:
    """Deepest the whole correction may go at this frequency."""
    if not internal_mic:
        return CUT_LIMIT_DB
    return float(np.interp(
        math.log(center), np.log(INTERNAL_CUT_ANCHORS_HZ), INTERNAL_CUT_LIMITS_DB
    ))


def _total_cut_limit(frequencies: np.ndarray, internal_mic: bool) -> np.ndarray:
    if not internal_mic:
        return np.full(frequencies.size, CUT_LIMIT_DB)
    return np.interp(
        np.log(frequencies), np.log(INTERNAL_CUT_ANCHORS_HZ), INTERNAL_CUT_LIMITS_DB
    )


def _filter_cut_limit_at(center: float, internal_mic: bool) -> float:
    """Deepest one section may go: the per-filter limit, or the total where tighter."""
    return max(PER_FILTER_CUT_LIMIT_DB, _cut_limit_at(center, internal_mic))


def _window_cut_limit(low_hz: float, high_hz: float, internal_mic: bool) -> float:
    """The least-negative single-filter limit anywhere a section can reach."""
    samples = np.geomspace(max(low_hz, 1.0), max(high_hz, low_hz, 1.0), 9)
    return max(_filter_cut_limit_at(float(value), internal_mic) for value in samples)


def _boost_decision(
    center: float,
    frequencies: np.ndarray,
    desired: np.ndarray,
    confidence: np.ndarray,
    safe_boost_floor: float,
) -> dict:
    local = np.abs(np.log2(frequencies / center)) <= 0.42
    if not np.any(local):
        local_confidence = 0.0
        broad_deficit = 0.0
        deficit_fraction = 0.0
    else:
        local_confidence = float(np.average(confidence[local]))
        broad_deficit = float(np.average(desired[local], weights=confidence[local]))
        deficit_fraction = float(np.mean(desired[local] >= 1.0))
    permitted = (
        center >= safe_boost_floor and center <= 8000.0
        and local_confidence >= 0.72 and broad_deficit >= 1.50
        and deficit_fraction >= 0.60
    )
    return {
        "center_hz": round(float(center), 1),
        "permitted": bool(permitted),
        "broad_deficit_db": round(broad_deficit, 3),
        "confidence": round(local_confidence, 3),
        "deficit_fraction": round(deficit_fraction, 3),
    }


def _smooth_and_level_align(
    curve: np.ndarray, reference: np.ndarray, frequencies: np.ndarray
) -> np.ndarray:
    smoothed = perceptual_smooth(frequencies, curve)
    band = (frequencies >= 250.0) & (frequencies <= 2000.0)
    smoothed += float(np.median(reference[band] - smoothed[band]))
    return smoothed


def _validation_data(
    measurement: dict, measured_smooth: np.ndarray, frequencies: np.ndarray
) -> tuple[np.ndarray, np.ndarray, list[np.ndarray], dict]:
    """Build a training curve and a genuinely held-out repeat when available."""
    repeat_groups: dict[int, list[np.ndarray]] = {}
    for item in measurement.get("validation_curves", []):
        curve = np.asarray(item.get("response_db", []), dtype=float)
        if curve.size != frequencies.size:
            continue
        repeat = int(item.get("repeat", 0))
        repeat_groups.setdefault(repeat, []).append(
            _smooth_and_level_align(curve, measured_smooth, frequencies)
        )
    grouped = [
        np.median(np.vstack(repeat_groups[key]), axis=0)
        for key in sorted(repeat_groups)
        if repeat_groups[key]
    ]
    group_names = [str(key) for key in sorted(repeat_groups) if repeat_groups[key]]
    if len(grouped) >= 2:
        training = np.median(np.vstack(grouped[:-1]), axis=0)
        holdout = grouped[-1]
        return training, holdout, grouped, {
            "mode": "repeat-holdout",
            "groups": len(grouped),
            "training_repeats": group_names[:-1],
            "held_out_repeats": group_names[-1:],
            "curves": sum(len(items) for items in repeat_groups.values()),
        }

    channel_folds = []
    channel_names = []
    for channel in measurement.get("channels", []):
        curve = np.asarray(channel.get("response_db", []), dtype=float)
        if curve.size == frequencies.size:
            channel_folds.append(
                _smooth_and_level_align(curve, measured_smooth, frequencies)
            )
            channel_names.append(str(channel.get("output_channel", len(channel_names))))
    if len(channel_folds) >= 2:
        return channel_folds[0], channel_folds[1], channel_folds, {
            "mode": "channel-holdout",
            "groups": len(channel_folds),
            "training_channels": channel_names[:1],
            "held_out_channels": channel_names[1:2],
            "curves": len(channel_folds),
        }
    return measured_smooth, measured_smooth, [measured_smooth], {
        "mode": "aggregate-only",
        "groups": 1,
        "curves": 1,
    }


def _weighted_rmse(
    curve: np.ndarray,
    correction: np.ndarray,
    target: np.ndarray,
    weights: np.ndarray,
    valid: np.ndarray,
) -> float:
    residual = curve[valid] + correction[valid] - target[valid]
    return math.sqrt(float(np.average(residual * residual, weights=weights[valid])))


def _candidate_indices(score: np.ndarray, valid: np.ndarray, limit: int) -> list[int]:
    indices = np.where(valid)[0]
    peaks = []
    for position, index in enumerate(indices):
        left = score[indices[position - 1]] if position > 0 else -math.inf
        right = score[indices[position + 1]] if position + 1 < indices.size else -math.inf
        if score[index] >= left and score[index] >= right:
            peaks.append(index)
    return sorted(peaks, key=lambda index: float(score[index]), reverse=True)[:limit]


def _estimate_q(
    need: np.ndarray, peak_index: int, frequencies: np.ndarray, q_bounds: tuple[float, float]
) -> float:
    peak = float(need[peak_index])
    threshold = max(0.25, peak * 0.5)
    left = peak_index
    right = peak_index
    while left > 0 and need[left - 1] >= threshold:
        left -= 1
    while right + 1 < need.size and need[right + 1] >= threshold:
        right += 1
    bandwidth_octaves = max(0.28, math.log2(frequencies[right] / frequencies[left]))
    ratio = 2.0 ** bandwidth_octaves
    q = math.sqrt(ratio) / max(ratio - 1.0, 1e-6)
    return float(np.clip(q, q_bounds[0], q_bounds[1]))


def _filter_correction(
    frequencies: np.ndarray, filters: list[dict], parameters: np.ndarray, rate: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if not filters:
        empty = np.asarray([], dtype=float)
        return np.zeros_like(frequencies), empty, empty, empty
    shaped = np.asarray(parameters, dtype=float).reshape((-1, 3))
    centers = np.exp(shaped[:, 0])
    q_values = np.exp(shaped[:, 1])
    gains = shaped[:, 2]
    response = filter_response_db(
        frequencies, gains, rate, centers_hz=centers, q_values=q_values,
        shapes=[item.get("shape", "peaking") for item in filters],
    )
    return response, centers, q_values, gains


def _parameter_bounds(filters: list[dict]) -> list[tuple[float, float]]:
    bounds = []
    for item in filters:
        bounds.extend((
            (math.log(item["frequency_bounds"][0]), math.log(item["frequency_bounds"][1])),
            (math.log(item["q_bounds"][0]), math.log(item["q_bounds"][1])),
            item["gain_bounds"],
        ))
    return bounds


def _initial_parameters(filters: list[dict]) -> np.ndarray:
    values = []
    for item in filters:
        values.extend((math.log(item["center_hz"]), math.log(item["q"]), item["gain_db"]))
    return np.asarray(values, dtype=float)


def _fit_filters(
    frequencies: np.ndarray,
    training: np.ndarray,
    target: np.ndarray,
    weights: np.ndarray,
    valid: np.ndarray,
    safety_highpass: np.ndarray,
    filters: list[dict],
    rate: int,
    initial: np.ndarray | None = None,
    total_limit: np.ndarray | None = None,
) -> tuple[np.ndarray, object, float]:
    if not filters:
        return np.asarray([], dtype=float), None, _weighted_rmse(
            training, safety_highpass, target, weights, valid
        )

    def objective(parameters):
        peq, centers, q_values, gains = _filter_correction(
            frequencies, filters, parameters, rate
        )
        correction = safety_highpass + peq
        residual = training[valid] + correction[valid] - target[valid]
        data_cost = float(np.average(residual * residual, weights=weights[valid]))
        # The depth limit applies to the sum of all sections, so overlapping
        # cuts cannot add up past what one filter is allowed to do.
        depth_cost = 0.0
        if total_limit is not None:
            below = np.minimum(peq[valid] - total_limit[valid], 0.0)
            depth_cost = 1.5 * float(np.sum(below * below))
        cuts = np.minimum(gains, 0.0)
        boosts = np.maximum(gains, 0.0)
        # Linear in depth on purpose: a quadratic penalty made two shallow
        # sections cheaper than one deep one and rewarded stacking.
        complexity = 0.03 * float(np.sum(np.abs(cuts)))
        boost_cost = 0.080 * float(np.sum(boosts * boosts))
        count_cost = 0.035 * len(filters)
        narrow_thresholds = np.asarray([
            item["narrow_q_threshold"] for item in filters
        ])
        narrow = np.maximum(q_values - narrow_thresholds, 0.0)
        narrow_cost = 0.025 * float(np.sum(narrow * narrow * np.abs(gains)))
        response_peak = max(0.0, float(np.max(correction)))
        headroom_cost = 0.35 * response_peak * response_peak
        overlap_cost = 0.0
        for left in range(centers.size):
            for right in range(left + 1, centers.size):
                distance = abs(math.log2(centers[left] / centers[right]))
                overlap_cost += 0.02 * max(0.0, 0.28 - distance) ** 2
        return (
            data_cost + complexity + boost_cost + count_cost
            + narrow_cost + headroom_cost + overlap_cost + depth_cost
        )

    start = _initial_parameters(filters) if initial is None else np.asarray(initial, dtype=float)
    result = minimize(
        objective,
        start,
        method="L-BFGS-B",
        bounds=_parameter_bounds(filters),
        options={"maxiter": 350, "ftol": 1e-10, "gtol": 1e-6},
    )
    peq, _, _, _ = _filter_correction(frequencies, filters, result.x, rate)
    rmse = _weighted_rmse(training, safety_highpass + peq, target, weights, valid)
    return np.asarray(result.x, dtype=float), result, rmse


def _new_filter(
    kind: str,
    index: int,
    residual: np.ndarray,
    frequencies: np.ndarray,
    q_bounds: tuple[float, float],
    frequency_range: tuple[float, float],
    internal_mic: bool,
    maximum_boost: float,
) -> dict:
    center = float(frequencies[index])
    need = np.maximum(residual, 0.0) if kind == "cut" else np.maximum(-residual, 0.0)
    q = _estimate_q(need, index, frequencies, q_bounds)
    low_frequency = max(frequency_range[0], center / (2.0 ** 0.48))
    high_frequency = min(frequency_range[1], center * (2.0 ** 0.48))
    if kind == "cut":
        # The least-negative limit anywhere in the search window is used so a
        # moving filter can never cross into a less-trusted band at full depth.
        lower_gain = _window_cut_limit(low_frequency, high_frequency, internal_mic)
        gain = -min(abs(lower_gain), max(0.35, float(residual[index]) * 0.72))
        gain_bounds = (lower_gain, 0.0)
    else:
        gain = min(maximum_boost, max(0.25, float(-residual[index]) * 0.55))
        gain_bounds = (0.0, maximum_boost)
    return {
        "kind": kind,
        "shape": "peaking",
        "center_hz": center,
        "q": q,
        "gain_db": float(gain),
        "frequency_bounds": (low_frequency, high_frequency),
        "q_bounds": q_bounds,
        "gain_bounds": gain_bounds,
        "narrow_q_threshold": 1.4 if internal_mic else 2.25,
    }


def _shelf_candidates(
    residual: np.ndarray,
    frequencies: np.ndarray,
    confidence: np.ndarray,
    valid: np.ndarray,
    internal_mic: bool,
    existing: list[dict],
) -> list[dict]:
    """Cut-only shelf proposals where the residual stays high toward an edge."""
    candidates: list[dict] = []
    present = {item.get("shape") for item in existing}
    valid_frequencies = frequencies[valid]
    if valid_frequencies.size < 8:
        return candidates
    low_edge, high_edge = float(valid_frequencies[0]), float(valid_frequencies[-1])
    for shape, corners in (
        ("lowshelf", LOW_SHELF_CORNERS_HZ), ("highshelf", HIGH_SHELF_CORNERS_HZ)
    ):
        if shape in present:
            continue
        for corner in corners:
            if shape == "lowshelf":
                if corner <= low_edge * 1.4:
                    continue
                region = valid & (frequencies <= corner)
                reach = (low_edge, min(high_edge, corner * 1.5))
            else:
                if corner >= high_edge / 1.4:
                    continue
                region = valid & (frequencies >= corner)
                reach = (max(low_edge, corner / 1.5), high_edge)
            if np.count_nonzero(region) < 6 or float(np.sum(confidence[region])) <= 1e-9:
                continue
            excess = float(np.average(residual[region], weights=confidence[region]))
            fraction = float(np.mean(residual[region] >= 0.5))
            if excess < 0.8 or fraction < 0.6:
                continue
            limit = _window_cut_limit(reach[0], reach[1], internal_mic)
            candidates.append({
                "kind": "cut",
                "shape": shape,
                "center_hz": float(corner),
                "q": 0.707,
                "gain_db": -min(abs(limit), max(0.5, excess * 0.75)),
                "frequency_bounds": (max(low_edge, corner / 1.5), min(high_edge, corner * 1.5)),
                "q_bounds": SHELF_Q_BOUNDS,
                "gain_bounds": (limit, 0.0),
                "narrow_q_threshold": 1.4 if internal_mic else 2.25,
            })
    return candidates


def optimize_peq(
    measurement: dict,
    voicing: str,
    *,
    internal_mic: bool,
    loudness: str = "protected",
    bass: str = "normal",
) -> dict:
    frequencies = np.asarray(measurement["frequency_hz"], dtype=float)
    measured = np.asarray(measurement["level_dbfs"], dtype=float)
    # Fitted to what the ear can resolve, not to every bin of the measurement.
    measured_smooth = perceptual_smooth(frequencies, measured)
    confidence, uncertainty = _measurement_confidence(measurement)
    training, holdout, validation_groups, validation_info = _validation_data(
        measurement, measured_smooth, frequencies
    )

    calibrated_external = bool(
        not internal_mic and measurement.get("microphone_calibration")
    )
    if internal_mic:
        maximum_boost = 1.5
        maximum_filters = 6
        q_bounds = (0.5, 2.0)
        frequency_range = (160.0, 10_000.0)
    elif calibrated_external:
        maximum_boost = 3.0
        maximum_filters = 10
        q_bounds = (0.4, 4.0)
        frequency_range = (100.0, 14_000.0)
    else:
        maximum_boost = 2.0
        maximum_filters = 8
        q_bounds = (0.45, 3.0)
        frequency_range = (140.0, 12_000.0)

    base_target = pleasant_in_room_target(frequencies, "neutral")
    selected_target = pleasant_in_room_target(frequencies, voicing)
    valid = (
        (frequencies >= frequency_range[0])
        & (frequencies <= frequency_range[1])
    )
    # Alignment remains intentionally low: cuts are the normal solution.  The
    # held-out repeat is not used to choose this offset or filter count.
    offset = _weighted_quantile(
        training[valid] - selected_target[valid], confidence[valid], 0.22
    )
    aligned_target = selected_target + offset
    # The knee asks how much output there is, not what will be heard as a
    # resonance, so it reads a plain average.  A peak-weighted one lifts a
    # steep roll-off and would place the corner lower than the speaker earns.
    highpass = estimate_highpass(
        frequencies, perceptual_smooth(frequencies, measured, peak_weighted=False),
        aligned_target,
    )
    safety_highpass = highpass["stages"] * _highpass_response_db(
        frequencies, highpass["frequency_hz"], highpass["q"], measurement["rate_hz"]
    )
    # The high-pass is a protection decision, not an error to be corrected.
    # Folding it into the target stops the optimizer from spending filters and
    # headroom trying to boost back what was deliberately removed, and makes
    # the drawn target roll off with the speaker instead of promising bass it
    # cannot make.
    aligned_target = aligned_target + safety_highpass
    total_cut_limit = _total_cut_limit(frequencies, internal_mic)
    desired = np.clip(aligned_target - training - safety_highpass, -16.0, 4.0)
    safe_boost_floor = _safe_boost_floor(frequencies, training, confidence)

    weights = confidence.copy()
    weights[~valid] *= 0.1
    weights /= max(float(np.mean(weights[valid])), 1e-9)
    boost_decisions = [
        _boost_decision(
            float(center), frequencies, desired, confidence, safe_boost_floor
        )
        for center in DIAGNOSTIC_CENTERS
    ]

    filters: list[dict] = []
    parameters = np.asarray([], dtype=float)
    optimizer_results = []
    selection_trace = []
    current_peq = np.zeros_like(frequencies)
    current_train_rmse = _weighted_rmse(
        training, safety_highpass, aligned_target, weights, valid
    )
    current_holdout_rmse = _weighted_rmse(
        holdout, safety_highpass, aligned_target, weights, valid
    )
    current_group_rmse = [
        _weighted_rmse(group, safety_highpass, aligned_target, weights, valid)
        for group in validation_groups
    ]
    real_holdout = validation_info["mode"] != "aggregate-only"
    # A built-in microphone needs a larger win before another filter is worth
    # trusting.  Calibrated external measurements may justify finer changes.
    if not real_holdout:
        minimum_improvement = 0.12
    elif internal_mic:
        minimum_improvement = 0.10
    elif calibrated_external:
        minimum_improvement = 0.04
    else:
        minimum_improvement = 0.06

    for _ in range(maximum_filters):
        residual = training + safety_highpass + current_peq - aligned_target
        cut_score = gaussian_filter1d(
            np.maximum(residual, 0.0) * confidence, sigma=2.0, mode="nearest"
        )
        boost_score = gaussian_filter1d(
            np.maximum(-residual, 0.0) * confidence, sigma=2.0, mode="nearest"
        )
        candidate_specs = []
        # Compare against where the sections ended up after fitting, not where
        # they were proposed, so a second cut cannot land on top of the first.
        _, existing_centers, _, _ = _filter_correction(
            frequencies, filters, parameters, measurement["rate_hz"]
        )
        for index in _candidate_indices(cut_score, valid, 7):
            if cut_score[index] < 0.30:
                continue
            center = float(frequencies[index])
            if any(abs(math.log2(center / float(existing))) < 0.22 for existing in existing_centers):
                continue
            candidate_specs.append(_new_filter(
                "cut", index, residual, frequencies, q_bounds,
                frequency_range, internal_mic, maximum_boost,
            ))
        for index in _candidate_indices(boost_score, valid, 4):
            center = float(frequencies[index])
            decision = _boost_decision(
                center, frequencies, desired, confidence, safe_boost_floor
            )
            if not decision["permitted"]:
                continue
            if any(abs(math.log2(center / float(existing))) < 0.30 for existing in existing_centers):
                continue
            candidate_specs.append(_new_filter(
                "boost", index, residual, frequencies, q_bounds,
                frequency_range, internal_mic, maximum_boost,
            ))
        candidate_specs.extend(_shelf_candidates(
            residual, frequencies, confidence, valid, internal_mic, filters
        ))

        best = None
        for candidate in candidate_specs:
            trial_filters = filters + [candidate]
            trial_initial = np.concatenate((
                parameters,
                _initial_parameters([candidate]),
            ))
            trial_parameters, result, train_rmse = _fit_filters(
                frequencies, training, aligned_target, weights, valid,
                safety_highpass, trial_filters, measurement["rate_hz"], trial_initial,
                total_limit=total_cut_limit,
            )
            trial_peq, _, _, trial_gains = _filter_correction(
                frequencies, trial_filters, trial_parameters, measurement["rate_hz"]
            )
            if abs(float(trial_gains[-1])) < 0.12:
                continue
            correction = safety_highpass + trial_peq
            holdout_rmse = _weighted_rmse(
                holdout, correction, aligned_target, weights, valid
            )
            group_rmse = [
                _weighted_rmse(group, correction, aligned_target, weights, valid)
                for group in validation_groups
            ]
            train_improvement = current_train_rmse - train_rmse
            holdout_improvement = current_holdout_rmse - holdout_rmse
            worst_change = max(group_rmse) - max(current_group_rmse)
            accepted = (
                train_improvement >= minimum_improvement
                and holdout_improvement >= minimum_improvement
                and worst_change <= 0.06
            )
            if not accepted:
                continue
            score = holdout_improvement + 0.35 * train_improvement - max(0.0, worst_change)
            if best is None or score > best["score"]:
                best = {
                    "score": score,
                    "candidate": candidate,
                    "filters": trial_filters,
                    "parameters": trial_parameters,
                    "result": result,
                    "peq": trial_peq,
                    "train_rmse": train_rmse,
                    "holdout_rmse": holdout_rmse,
                    "group_rmse": group_rmse,
                    "train_improvement": train_improvement,
                    "holdout_improvement": holdout_improvement,
                }
        if best is None:
            break
        filters = best["filters"]
        parameters = best["parameters"]
        current_peq = best["peq"]
        current_train_rmse = best["train_rmse"]
        current_holdout_rmse = best["holdout_rmse"]
        current_group_rmse = best["group_rmse"]
        optimizer_results.append(best["result"])
        _, centers_now, q_now, gains_now = _filter_correction(
            frequencies, filters, parameters, measurement["rate_hz"]
        )
        selection_trace.append({
            "filter": len(filters),
            "kind": best["candidate"]["kind"],
            "shape": best["candidate"]["shape"],
            "frequency_hz": round(float(centers_now[-1]), 1),
            "q": round(float(q_now[-1]), 3),
            "gain_db": round(float(gains_now[-1]), 2),
            "training_improvement_db": round(best["train_improvement"], 3),
            "held_out_improvement_db": round(best["holdout_improvement"], 3),
        })

    # Filter count has now been selected without the held-out repeat. Refit the
    # accepted structure to the robust all-capture aggregate, but keep the
    # training fit if the refit degrades held-out behavior materially.
    if filters:
        final_parameters, final_result, _ = _fit_filters(
            frequencies, measured_smooth, aligned_target, weights, valid,
            safety_highpass, filters, measurement["rate_hz"], parameters,
            total_limit=total_cut_limit,
        )
        final_peq, _, _, _ = _filter_correction(
            frequencies, filters, final_parameters, measurement["rate_hz"]
        )
        final_holdout = _weighted_rmse(
            holdout, safety_highpass + final_peq, aligned_target, weights, valid
        )
        final_group_rmse = [
            _weighted_rmse(
                group, safety_highpass + final_peq, aligned_target, weights, valid
            )
            for group in validation_groups
        ]
        if (
            final_holdout <= current_holdout_rmse + 0.05
            and max(final_group_rmse) <= max(current_group_rmse) + 0.08
        ):
            parameters = final_parameters
            current_peq = final_peq
            current_holdout_rmse = final_holdout
            current_group_rmse = final_group_rmse
            optimizer_results.append(final_result)

    _, centers, q_values, gains = _filter_correction(
        frequencies, filters, parameters, measurement["rate_hz"]
    )
    shapes = np.asarray([item["shape"] for item in filters], dtype=object)
    if gains.size:
        keep = np.abs(gains) >= 0.08
        centers, q_values, gains, shapes = (
            centers[keep], q_values[keep], gains[keep], shapes[keep]
        )
        order = np.argsort(centers)
        centers, q_values, gains, shapes = (
            centers[order], q_values[order], gains[order], shapes[order]
        )
    peq_response = filter_response_db(
        frequencies,
        gains,
        measurement["rate_hz"],
        centers_hz=centers,
        q_values=q_values,
        shapes=list(shapes),
    )
    correction = safety_highpass + peq_response
    shelf = bass_shelf(safe_boost_floor, bass, highpass["frequency_hz"])
    if shelf is not None:
        correction = correction + _lowshelf_response_db(
            frequencies, shelf["frequency_hz"], shelf["q"], shelf["gain_db"],
            measurement["rate_hz"],
        )
    predicted = measured_smooth + correction
    positive_peak = max(0.0, float(np.max(correction)))
    headroom_db = max(1.0, math.ceil((positive_peak + 1.0) * 100.0) / 100.0)
    # The cuts land where the speaker was loudest, so the corrected speaker
    # plays quieter at the same volume setting.  Estimate how much, and let
    # the loudness mode decide how much of it to add back before the limiter.
    loudness_loss_db = max(0.0, (
        pink_loudness_db(frequencies, measured_smooth)
        - pink_loudness_db(frequencies, measured_smooth + correction)
    ))
    makeup_db = loudness_makeup_db(loudness_loss_db, loudness)
    net_input_gain_db = makeup_db - headroom_db
    before_rmse = _weighted_rmse(
        measured_smooth, np.zeros_like(frequencies), aligned_target, weights, valid
    )
    after_rmse = _weighted_rmse(
        measured_smooth, correction, aligned_target, weights, valid
    )
    cv_before = _weighted_rmse(
        holdout, safety_highpass, aligned_target, weights, valid
    )
    cv_after = _weighted_rmse(
        holdout, correction, aligned_target, weights, valid
    )
    cut_limits = [_filter_cut_limit_at(float(center), internal_mic) for center in centers]
    filters_payload = [
        {
            "type": str(shape),
            "frequency_hz": round(float(center), 1),
            "q": round(float(q), 3),
            "gain_db": round(float(gain), 2),
        }
        for center, q, gain, shape in zip(centers, q_values, gains, shapes)
    ]

    return {
        "algorithm": "adaptive-cross-validated-peq-v2",
        "filter_strategy": "adaptive frequency, bandwidth, gain, and count",
        "filter_count": len(filters_payload),
        "maximum_filter_count": maximum_filters,
        "filters": filters_payload,
        "shelves": [item for item in filters_payload if item["type"] != "peaking"],
        # Parallel arrays remain for the panel and older profiles; the graph
        # routes each section by its type when the "filters" list is present.
        "centers_hz": [item["frequency_hz"] for item in filters_payload],
        "q": [item["q"] for item in filters_payload],
        "gains_db": [item["gain_db"] for item in filters_payload],
        "types": [item["type"] for item in filters_payload],
        "q_bounds": list(q_bounds),
        "cut_limit_db": CUT_LIMIT_DB,
        "per_filter_cut_limit_db": PER_FILTER_CUT_LIMIT_DB,
        "cut_limits_db": np.round(cut_limits, 2).tolist(),
        "total_cut_limit_db": np.round(total_cut_limit, 2).tolist(),
        "deepest_correction_db": round(float(np.min(peq_response)) if peq_response.size else 0.0, 2),
        "maximum_allowed_boost_db": maximum_boost,
        "actual_maximum_boost_db": round(
            max(0.0, float(np.max(gains))) if gains.size else 0.0, 2
        ),
        "safe_boost_floor_hz": round(safe_boost_floor, 1),
        "boost_decisions": boost_decisions,
        "headroom_db": headroom_db,
        "highpass": highpass,
        # Flat keys for the graph and for older panels.
        "highpass_hz": highpass["frequency_hz"],
        "highpass_stages": highpass["stages"],
        "bass_mode": bass,
        "bass_shelf": shelf,
        "loudness_mode": loudness,
        "loudness_loss_db": round(loudness_loss_db, 2),
        "makeup_db": makeup_db,
        "net_input_gain_db": round(net_input_gain_db, 2),
        "input_gain_linear": round(10.0 ** (net_input_gain_db / 20.0), 6),
        "weighted_rmse_before_db": round(before_rmse, 3),
        "weighted_rmse_after_db": round(after_rmse, 3),
        "cross_validation": {
            **validation_info,
            "rmse_before_db": round(cv_before, 3),
            "rmse_after_db": round(cv_after, 3),
            "minimum_filter_improvement_db": minimum_improvement,
            "selection_trace": selection_trace,
        },
        "target": {
            "name": "Pleasant in-room loudspeaker target",
            "basis": "gentle bass rise, flat midband, gradual treble decline",
            "voicing": voicing,
            "relative_db": np.round(base_target, 3).tolist(),
            "selected_relative_db": np.round(selected_target, 3).tolist(),
            "aligned_db": np.round(aligned_target, 3).tolist(),
            "offset_db": round(offset, 3),
        },
        "smoothing": {
            "method": "critical-band, peak-weighted",
            "exponent": PEAK_WEIGHT_EXPONENT,
            "octaves": np.round(erb_octaves(frequencies), 3).tolist(),
        },
        "measured_smoothed_db": np.round(measured_smooth, 3).tolist(),
        "predicted_response_db": np.round(predicted, 3).tolist(),
        "correction_response_db": np.round(correction, 3).tolist(),
        "confidence": np.round(confidence, 3).tolist(),
        "uncertainty_db": np.round(uncertainty, 3).tolist(),
        "objective": round(after_rmse * after_rmse, 6),
        "optimizer_success": all(
            result is None or bool(result.success) for result in optimizer_results
        ),
        "optimizer_message": (
            str(optimizer_results[-1].message)
            if optimizer_results else "No filter passed held-out validation."
        ),
    }
