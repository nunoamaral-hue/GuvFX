"""trade_copier.translation — PURE SL/TP translation for a copied position (no Django, no I/O).

Translates the master's SL/TP to the follower per the follower's policy (packet §10):

  * SAME_PRICE    — copy the absolute SL/TP price (valid when the follower trades the SAME price scale).
  * SAME_DISTANCE — preserve the distance from entry: follower_sl = follower_entry ∓ (master_sl distance).
  * NONE          — do not replicate SL/TP.

Fail-closed: SAME_DISTANCE without a follower entry price cannot be computed and returns a reason
rather than guessing. A level of 0.0/None on the master means "no level" and is passed through as None
(the follower simply has no SL/TP for that side). This module never WEAKENS a stop — it computes the
intended level; whether the destination broker accepts it (min stop distance) is validated at execution
time, where an unobtainable protective level marks the position UNPROTECTED (never silently dropped).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SAME_PRICE = "SAME_PRICE"
SAME_DISTANCE = "SAME_DISTANCE"
NONE = "NONE"

_EPS = 1e-9


@dataclass(frozen=True)
class TranslatedLevels:
    ok: bool
    sl: Optional[float]
    tp: Optional[float]
    reason: str


def _has(v: Optional[float]) -> bool:
    return v is not None and abs(float(v)) > _EPS


def translate_sltp(
    policy: str,
    side: str,
    master_entry: Optional[float],
    master_sl: Optional[float],
    master_tp: Optional[float],
    *,
    follower_entry: Optional[float] = None,
) -> TranslatedLevels:
    """Compute the follower's intended SL/TP. ``side`` is 'BUY' or 'SELL'."""
    if policy == NONE:
        return TranslatedLevels(True, None, None, "policy_none")

    if policy == SAME_PRICE:
        sl = float(master_sl) if _has(master_sl) else None
        tp = float(master_tp) if _has(master_tp) else None
        return TranslatedLevels(True, sl, tp, "same_price")

    if policy == SAME_DISTANCE:
        if not _has(master_entry) or not _has(follower_entry):
            return TranslatedLevels(False, None, None, "requires_entry_prices")
        me = float(master_entry)
        fe = float(follower_entry)
        # Distance preserved on the same side of entry as the master's level.
        sl = (fe + (float(master_sl) - me)) if _has(master_sl) else None
        tp = (fe + (float(master_tp) - me)) if _has(master_tp) else None
        return TranslatedLevels(True, sl, tp, "same_distance")

    return TranslatedLevels(False, None, None, "unknown_policy")
