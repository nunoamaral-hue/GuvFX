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

# "move SL to 4010" / "SL to 4010.5" / "stop to 1,234.5" (an SL keyword + separator + a number).
# The trailing \b anchors the FULL number (no backtrack to a short match); a pip count immediately
# after ("SL at 20 pips") is vetoed in classify_command, not read as "move SL to 20".
_SL_PRICE_RE = re.compile(rf"\b{_SL_WORD}\b[^A-Za-z0-9]{{0,8}}(?:to|at|@|=|:)\s*([0-9][0-9.,]*[0-9]|[0-9])\b", re.I)

# "close TP2" / "close out tp 3" / "close the take profit 2". Requires the IMPERATIVE form: "close"
# directly governs the TP (whitespace + optional out/the). This rejects the proximity/adjective
# idioms "price is close to TP2" and "we're close, TP2 next" (comma/"to" break the \s+ governance).
_CLOSE_LEG_RE = re.compile(r"\bclose\b\s+(?:out\s+|the\s+)?(?:tp|take[\s-]?profit)\s*([123])\b", re.I)
# "close all" / "close remaining" / "close everything" / "close the trade(s)" / "close now" /
# "close position(s)" / "close out". Same imperative rule (close + whitespace + a close-object) so
# "close to all-time high" / "getting close, all targets near" are NOT read as CLOSE_ALL.
_CLOSE_ALL_RE = re.compile(r"\bclose\b\s+(?:out\b|the\s+trade|trades?|all|remaining|everything|position|positions|now)\b", re.I)
# "cancel signal" / "void the order" / "disregard this trade" / "cancel this setup". Requires an
# UNAMBIGUOUS cancel verb (cancel/void/disregard) AND a real trade NOUN. "ignore" is EXCLUDED — it
# is bidirectional ("ignore it, position still valid" tells followers to HOLD, the opposite of a
# cancel); bare "it" is EXCLUDED as a noun for the same reason.
_CANCEL_RE = re.compile(r"\b(cancel|cancelled|canceled|void|voided|disregard)\b[^\n]{0,24}\b(signal|setup|trade|order|pending|position|entry)\b", re.I)
_CANCEL_RE2 = re.compile(r"\b(cancel(?:led|ed)?|void(?:ed)?|disregard)\b\s*$", re.I | re.M)

# A price report of an SL/TP that was HIT is a status update, not a move-SL command.
_SL_HIT_RE = re.compile(r"\b(?:hit|stopped?\s*out|taken\s+out|got\s+stopped|was\s+hit|triggered)\b", re.I)

# status-update markers → NON_ACTIONABLE (recorded, never acted)
_STATUS_RE = re.compile(r"\b(tp\s*[123]|take[\s-]?profit|hit|reached|running|in\s+profit|pips?|secured?|closed\s+in\s+profit|sl\s+hit|stopped\s+out|break[\s-]?even|profit|target)\b", re.I)


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

    # 4) Move SL to a specific price — but veto (a) a REPORT that the SL was hit ("SL @ 4010 was
    #    hit" / "stopped out at 4010") and (b) a PIP count immediately after the number ("SL at 20
    #    pips") — both are status updates, not move commands.
    m = _SL_PRICE_RE.search(t)
    if m and not _SL_HIT_RE.search(t) and not t[m.end():].lstrip().lower().startswith("pip"):
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
