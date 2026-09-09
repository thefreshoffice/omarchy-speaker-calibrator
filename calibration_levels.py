"""How loud, in decibels: the arithmetic that decides gains and trims.

These are the few numbers the panel needs to answer "how much louder or
quieter", and none of them involve a measurement.  They live apart from the
optimizer so that asking for them does not drag in numpy and scipy: the panel
reads the status on every refresh, and importing the signal-processing stack
for three lines of arithmetic cost more than everything else it does put
together.  The optimizer re-exports them, so there is still one definition.
"""

from __future__ import annotations

# How much of the loudness lost to the cuts is added back as input gain.
LOUDNESS_MODES = {"protected": 0.0, "balanced": 0.5, "matched": 1.0}
# Beyond this the limiter would be working on most peaks of loud music.  With
# the 1 dB limiter margin this also bounds the electrical drive in any band
# to 5 dB above the uncorrected speaker, whatever else is selected.
MAKEUP_CAP_DB = 6.0

# Comparing a correction against no correction is only meaningful when both
# play at the same loudness; louder almost always wins otherwise.  Anything
# past this is a sign the numbers are wrong rather than the speaker.
BYPASS_MATCH_FLOOR_DB = -20.0


def bypass_level_match_db(fit: dict) -> float:
    """How far to turn the plain speakers down so a comparison is about tone.

    The corrected speaker is quieter than the raw one by the loudness the cuts
    removed, less whatever make-up was added back at the input.  Turning the
    bypassed path down by the same amount leaves only the tone to judge.  It
    never turns the plain speakers up: that would ask for headroom the raw
    signal has not got.
    """
    loss = float(fit.get("loudness_loss_db", 0.0))
    net = float(fit.get("net_input_gain_db", -float(fit.get("headroom_db", 1.0))))
    return round(max(BYPASS_MATCH_FLOOR_DB, min(0.0, net - loss)), 2)


def loudness_makeup_db(loudness_loss_db: float, loudness: str) -> float:
    """Input gain that pays back part of the loudness the cuts removed.

    The share applies to the capped loss, so "balanced" is always a real
    step between "protected" and "matched", even when the loss is large.
    """
    fraction = LOUDNESS_MODES.get(loudness, 0.0)
    return round(min(MAKEUP_CAP_DB, max(0.0, loudness_loss_db)) * fraction, 2)
