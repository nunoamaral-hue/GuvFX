"""WS-E — provider follow-up command classifier (PURE, no I/O, no DB, no imports of execution).

Classifies a provider FOLLOW-UP message ("Move SL to BE", "Move SL to 4010", "Close all",
"Close TP2", "Cancel signal", …) into a structured command. CONSERVATIVE by design: it only
returns an ACTIONABLE command when the intent is unambiguous; anything else falls to
``NON_ACTIONABLE`` (a status update like a TP-hit) or ``UNKNOWN`` (unrecognised). It never guesses
a destructive action from a status update — a misclassification that turned "TP1 hit ✅" into
CLOSE_ALL would be dangerous, so the patterns require an explicit imperative verb.

This module does NOT touch the certified ENTRY-signal parser or its corpus.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MOVE_SL_BE = "MOVE_SL_BE"
MOVE_SL_PRICE = "MOVE_SL_PRICE"
CLOSE_ALL = "CLOSE_ALL"
CLOSE_LEG = "CLOSE_LEG"
CANCEL = "CANCEL"
NON_ACTIONABLE = "NON_ACTIONABLE"
UNKNOWN = "UNKNOWN"


@dataclass
class Command:
    command_type: str = UNKNOWN
    args: dict = field(default_factory=dict)
    confidence: str = "low"      # "high" | "low"
    reason: str = ""


# --- patterns (case-insensitive). Ordered by specificity in classify_command. ---
# An explicit imperative is required for any ACTIONABLE type.
_SL_WORD = r"(?:sl|s/l|stop[\s-]?loss|stop)"
_MOVE = r"(?:move|moved|set|shift|trail|bring|adjust|put|change)"

# "move SL to BE" / "SL to breakeven" / "move stop to entry" / "SL BE"
_BE_RE = re.compile(rf"\b{_SL_WORD}\b[^A-Za-z0-9]{{0,12}}(?:to|at|@|=|:)?\s*\b(?:b/?e|break[\s-]?even|entry|entries)\b", re.I)
_BE_RE2 = re.compile(rf"\b{_MOVE}\b[^0-9]{{0,20}}\b{_SL_WORD}\b[^A-Za-z0-9]{{0,12}}\b(?:b/?e|break[\s-]?even|entry)\b", re.I)

# "move SL to 4010" / "SL to 4010.5" / "stop to 1,234.5"  (an SL keyword + a number)
_SL_PRICE_RE = re.compile(rf"\b{_SL_WORD}\b[^A-Za-z0-9]{{0,8}}(?:to|at|@|=|:)\s*([0-9][0-9.,]*[0-9]|[0-9])", re.I)

# "close TP2" / "close tp 3" / "close take profit 2"
_CLOSE_LEG_RE = re.compile(r"\bclose\b[^0-9]{0,20}\b(?:tp|take[\s-]?profit)\s*([123])\b", re.I)
# "close all" / "close remaining" / "close everything" / "close the trade(s)" / "close now" / "close position(s)"
_CLOSE_ALL_RE = re.compile(r"\bclose\b[^\n]{0,20}\b(all|remaining|everything|the\s+trade|trades?|position|positions|now)\b", re.I)
# "cancel signal" / "cancel this setup" / "ignore this setup" / "void" / "disregard" / "cancel pending"
_CANCEL_RE = re.compile(r"\b(cancel|cancelled|canceled|void|voided|disregard|ignore)\b[^\n]{0,20}\b(signal|setup|trade|order|pending|this|it|entry)\b", re.I)
_CANCEL_RE2 = re.compile(r"\b(cancel(?:led|ed)?|voided?|disregard)\b\s*$", re.I | re.M)

# status-update markers → NON_ACTIONABLE (recorded, never acted)
_STATUS_RE = re.compile(r"\b(tp\s*[123]|take[\s-]?profit|hit|reached|running|in\s+profit|pips?|secured?|closed\s+in\s+profit|sl\s+hit|stopped\s+out|breakeven\s+secured)\b", re.I)


def classify_command(text: str) -> Command:
    """Classify a follow-up message. Never raises; unclear → NON_ACTIONABLE/UNKNOWN."""
    t = (text or "").strip()
    if not t:
        return Command(UNKNOWN, reason="empty")

    # 1) Move SL to breakeven (before move-SL-to-price, since BE has no numeric target)
    if _BE_RE.search(t) or _BE_RE2.search(t):
        return Command(MOVE_SL_BE, confidence="high", reason="move_sl_breakeven")

    # 2) Close a specific TP leg (before close-all)
    m = _CLOSE_LEG_RE.search(t)
    if m:
        return Command(CLOSE_LEG, args={"leg_index": int(m.group(1))}, confidence="high",
                       reason="close_leg")

    # 3) Close all remaining
    if _CLOSE_ALL_RE.search(t):
        return Command(CLOSE_ALL, confidence="high", reason="close_all")

    # 4) Move SL to a specific price
    m = _SL_PRICE_RE.search(t)
    if m:
        raw = m.group(1).replace(",", "")
        try:
            price = float(raw)
        except ValueError:
            price = None
        if price is not None and price > 0:
            return Command(MOVE_SL_PRICE, args={"price": raw}, confidence="high",
                           reason="move_sl_price")

    # 5) Cancel the pending signal
    if _CANCEL_RE.search(t) or _CANCEL_RE2.search(t):
        return Command(CANCEL, confidence="high", reason="cancel")

    # 6) A recognisable status update (TP hit / running / pips) → non-actionable, still recorded
    if _STATUS_RE.search(t):
        return Command(NON_ACTIONABLE, confidence="low", reason="status_update")

    return Command(UNKNOWN, reason="unrecognised")
