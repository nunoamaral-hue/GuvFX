"""
TI Signals Telegram signal source (content-only parsing).

Parses free-text messages from the "TI Signals" Telegram channel into a structured
``ParsedSignal`` so a ``ti_signals`` SignalProvider can feed the same provider-agnostic
acquisition path as Wayond. This module is pure (no I/O, no Django, no network) and is the
only place that understands TI Signals' message format.

It reuses the EXACT ``ParsedSignal`` / ``Kind`` contract from
``intelligence.telegram_source`` so every downstream consumer (acquisition dispatcher,
services.intake_parsed, execution) treats a TI signal identically to a Wayond one — only the
text format differs.

IMPORTANT — boundary (identical to the Wayond source):
  * This produces *content* intelligence only. It NEVER places, sizes, or approves an order.
    Wiring a parsed signal to execution is the separate, human-gated auto-router path.
  * Malformed / ambiguous messages are UNKNOWN (→ quarantine), never guessed into a
    tradeable signal (data.md: quarantine, don't destroy).

Example TI Signals message (from the channel):

    🔔 XAUUSD BUY (M15)
    Entry: 4019.25-4020.82 (mid 4020.03)
    TP1: 4023.67
    TP2: 4025.28
    TP3: 4027.43
    SL: 4017.61
    有效期限: 2026-07-14 05:17 UTC

The entry is a RANGE with an explicit mid; the mid is used as the single reference entry
(the real fill still comes from MT5 at execution time). ``有效期限`` (expiry) is informational
and preserved only in ``raw_text``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from intelligence.telegram_source import Kind, ParsedSignal, _norm_num

SOURCE = "TI_SIGNALS_TELEGRAM"

# Header line: "🔔 XAUUSD BUY (M15)" — symbol, direction, timeframe. The bell emoji is
# optional. The header MUST begin a line (like the SL line below) so a "SYMBOL BUY (word)"
# substring buried in prose cannot be mistaken for a signal, and the timeframe must be a REAL
# MT5 timeframe token (M/H + digits, D1/W1/MN) so an ordinary word like "(daily)"/"(setup)"
# does not qualify. Case-insensitive; groups are upper-cased on return.
_HEADER_RE = re.compile(
    r"(?:^|\n)\s*(?:🔔\s*)?([A-Za-z][A-Za-z0-9]{2,11})\s+(BUY|SELL)\s*"
    r"\(\s*(M\d{1,3}|H\d{1,2}|D1|W1|MN)\s*\)",
    re.I,
)
# Entry range with an explicit mid: "Entry: 4019.25-4020.82 (mid 4020.03)" → the mid. The
# delimiter after "mid" may be whitespace, colon, or equals ("mid 4020", "mid: 4020", "mid=4020").
_ENTRY_MID_RE = re.compile(
    r"Entry\s*:?\s*[0-9][0-9.,]*\s*[-–—]\s*[0-9][0-9.,]*\s*\(\s*mid[\s:=]+([0-9][0-9.,]*)\s*\)",
    re.I,
)
# A bare entry RANGE with no parenthetical mid — ambiguous, must quarantine (never guess a bound).
_ENTRY_BARE_RANGE_RE = re.compile(
    r"Entry\s*:?\s*[0-9][0-9.,]*\s*[-–—]\s*[0-9][0-9.,]*", re.I
)
# Fallback entry (a genuine single value, no range): "Entry: 4019.25" → the single value.
_ENTRY_SINGLE_RE = re.compile(r"Entry\s*:?\s*([0-9][0-9.,]*)", re.I)
# Stop loss: TI uses "SL: 4017.61" (anchored to a line start so it can't match mid-word).
# "Stop Loss" is also accepted for robustness.
_SL_RE = re.compile(r"(?:^|\n)\s*(?:SL|Stop\s*Loss)\s*:?\s*([0-9][0-9.,]*)", re.I)
# Take-profits: "TP1: 4023.67" / "TP2 4025.28" — same shape as Wayond's TP line.
_TP_RE = re.compile(r"\bTP\s*\d+\s*:?\s*([0-9][0-9.,]*)", re.I)
# Signal validity deadline: "有效期限: 2026-07-14 05:17 UTC" (or English "Valid until" / "Expiry").
# The 2nd CJK glyph appears as both 效 (U+6548, Chinese) and 効 (U+52B9, Japanese) across
# renderings, so match either. Times in these messages are UTC; captured as (date, HH:MM) and
# normalised to an ISO UTC string.
_EXPIRY_RE = re.compile(
    r"(?:有[効效]期限|Valid\s*until|Expir(?:y|es))\s*[:：]?\s*"
    r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s*(?:UTC)?",
    re.I,
)


def _parse_expiry_to_iso(body: str) -> str:
    """Return the message's expiry as an ISO-8601 UTC string, or "" if absent/unparseable."""
    m = _EXPIRY_RE.search(body)
    if not m:
        return ""
    try:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
    except ValueError:
        return ""
    return dt.replace(tzinfo=timezone.utc).isoformat()

# Follow-up updates (generic English — reused verbatim from the Wayond source semantics).
_TP_HIT_RE = re.compile(r"\bTP\s*\d+\s*hit", re.I)
_SL_HIT_RE = re.compile(r"\bSL\s+hit\b", re.I)
_PIPS_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*pips", re.I)
_MOVE_SL_RE = re.compile(r"Move\s+SL\s+to\s+([0-9][0-9.,]*)", re.I)


def parse_ti_signals(text: str, message_id: str = "") -> ParsedSignal:
    """Parse one TI Signals Telegram message body into a ParsedSignal (never raises)."""
    body = text or ""
    mid = str(message_id or "")

    # Follow-up UPDATES are matched FIRST so a message that recaps the signal header/SL block
    # (e.g. "SL hit on XAUUSD SELL (M15) … SL: 4021.69") is classified as an update and never
    # re-ingested as a fresh entry.
    if _TP_HIT_RE.search(body):
        pips = _PIPS_RE.search(body)
        return ParsedSignal(
            kind=Kind.UPDATE, message_id=mid, raw_text=body, source=SOURCE,
            update_type="TP_HIT", pips=(pips.group(1) if pips else ""),
        )
    move = _MOVE_SL_RE.search(body)
    if move:
        return ParsedSignal(
            kind=Kind.UPDATE, message_id=mid, raw_text=body, source=SOURCE,
            update_type="MOVE_SL", new_stop_loss=_norm_num(move.group(1)),
        )
    if _SL_HIT_RE.search(body):
        return ParsedSignal(
            kind=Kind.UPDATE, message_id=mid, raw_text=body, source=SOURCE,
            update_type="SL_HIT",
        )

    header = _HEADER_RE.search(body)
    sl = _SL_RE.search(body)
    if header and sl:
        em = _ENTRY_MID_RE.search(body)
        entry = em.group(1) if em else ""
        # A bare range with no resolvable mid is ambiguous — leave entry empty so it
        # quarantines, rather than guessing a bound. The single-value fallback applies only
        # when there is no range at all.
        if not entry and not _ENTRY_BARE_RANGE_RE.search(body):
            es = _ENTRY_SINGLE_RE.search(body)
            entry = es.group(1) if es else ""
        tps = tuple(_norm_num(m) for m in _TP_RE.findall(body))
        parsed = ParsedSignal(
            kind=Kind.SIGNAL,
            message_id=mid,
            raw_text=body,
            source=SOURCE,
            market=header.group(1).upper(),
            direction=header.group(2).upper(),
            entry=_norm_num(entry),
            stop_loss=_norm_num(sl.group(1)),
            take_profits=tps,
            expiry=_parse_expiry_to_iso(body),
        )
        # Only a fully-formed entry (symbol+direction+entry+SL) is tradeable; a header with
        # no resolvable entry falls through to quarantine rather than guessing a price.
        if parsed.is_tradeable_shape():
            return parsed
        return ParsedSignal(
            kind=Kind.UNKNOWN, message_id=mid, raw_text=body, source=SOURCE,
            reason="ti header present but entry not resolvable (ambiguous/missing)",
        )

    return ParsedSignal(
        kind=Kind.UNKNOWN, message_id=mid, raw_text=body, source=SOURCE,
        reason="no ti header+stop-loss pattern and no recognised update",
    )
