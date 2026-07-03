"""
Wayond Telegram signal source (content-only ingestion).

Parses free-text messages from the "Wayond | FX Signals" Telegram channel into a
structured signal, so they can feed the GuvFX intelligence -> WIMS *content*
pipeline. This module is pure (no I/O, no Django, no network) and is the only
place that understands Wayond's message format.

IMPORTANT — boundary:
  * This produces *content* intelligence only. It NEVER places, sizes, or
    approves an order. Wiring a parsed signal to execution requires the separate,
    human-gated control path (see the Notion packet); it must not happen here.
  * Malformed / ambiguous messages are QUARANTINED (kind=UNKNOWN), never guessed
    into a tradeable signal (data.md: quarantine, don't destroy).

Example Wayond messages (from the channel):

    NZDJPY | Potential upward movement
    NZDJPY | BUY 91.300
    ❌ Stop Loss 90.900 (40 pips)
    ✅ TP1 91.450
    ✅ TP2 91.700
    ✅ TP3 92.100

    TP1 hit! That gives us +250 pips. 🏅
    Move SL to 59200
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SOURCE = "WAYOND_TELEGRAM"

# Order line: "NZDJPY | BUY 91.300"  /  "BTCUSD | SELL 59450"
_ORDER_RE = re.compile(
    r"\b([A-Z][A-Z0-9]{2,11})\s*\|\s*(BUY|SELL)\s+([0-9][0-9.,]*)", re.I
)
# SL / TP allow an OPTIONAL colon after the label: real Wayond SELL messages use
# "STOP LOSS: 4028" and "TP1: 4015" (colon), while BUY messages use "Stop Loss 3985"
# and "TP1 3995" (no colon). Certified against real screenshots (corpus V1).
_SL_RE = re.compile(r"Stop\s*Loss\s*:?\s*([0-9][0-9.,]*)", re.I)
_TP_RE = re.compile(r"\bTP\s*\d+\s*:?\s*([0-9][0-9.,]*)", re.I)
_INTENT_RE = re.compile(r"Potential\s+(upward|downward)\s+movement", re.I)
_TP_HIT_RE = re.compile(r"\bTP\s*\d+\s*hit", re.I)
_SL_HIT_RE = re.compile(r"\bSL\s+hit\b", re.I)
_PIPS_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*pips", re.I)
_MOVE_SL_RE = re.compile(r"Move\s+SL\s+to\s+([0-9][0-9.,]*)", re.I)


class Kind:
    SIGNAL = "SIGNAL"      # a new entry signal
    UPDATE = "UPDATE"      # TP-hit / move-SL follow-up
    UNKNOWN = "UNKNOWN"    # could not be parsed -> quarantine


@dataclass(frozen=True)
class ParsedSignal:
    kind: str
    message_id: str
    raw_text: str
    source: str = SOURCE
    # SIGNAL fields
    market: str = ""
    direction: str = ""
    entry: str = ""
    stop_loss: str = ""
    take_profits: tuple = ()
    # UPDATE fields
    update_type: str = ""   # TP_HIT | MOVE_SL
    pips: str = ""
    new_stop_loss: str = ""
    # quarantine reason
    reason: str = ""

    def is_tradeable_shape(self) -> bool:
        return self.kind == Kind.SIGNAL and bool(
            self.market and self.direction and self.entry and self.stop_loss
        )


def _norm_num(s: str) -> str:
    return s.replace(",", "").strip()


def parse_message(text: str, message_id: str = "") -> ParsedSignal:
    """Parse one Telegram message body into a ParsedSignal (never raises)."""
    body = text or ""
    mid = str(message_id or "")

    order = _ORDER_RE.search(body)
    sl = _SL_RE.search(body)
    if order and sl:
        tps = tuple(_norm_num(m) for m in _TP_RE.findall(body))
        return ParsedSignal(
            kind=Kind.SIGNAL,
            message_id=mid,
            raw_text=body,
            market=order.group(1).upper(),
            direction=order.group(2).upper(),
            entry=_norm_num(order.group(3)),
            stop_loss=_norm_num(sl.group(1)),
            take_profits=tps,
        )

    # Follow-up updates (not a new entry).
    if _TP_HIT_RE.search(body):
        pips = _PIPS_RE.search(body)
        return ParsedSignal(
            kind=Kind.UPDATE, message_id=mid, raw_text=body,
            update_type="TP_HIT", pips=(pips.group(1) if pips else ""),
        )
    move = _MOVE_SL_RE.search(body)
    if move:
        return ParsedSignal(
            kind=Kind.UPDATE, message_id=mid, raw_text=body,
            update_type="MOVE_SL", new_stop_loss=_norm_num(move.group(1)),
        )
    # Stop-loss-hit note (real Wayond update, e.g. "SL hit" / "SL hit, 4035 for
    # re-entries!"). Any re-entry price is informational only — never auto-traded.
    if _SL_HIT_RE.search(body):
        return ParsedSignal(
            kind=Kind.UPDATE, message_id=mid, raw_text=body, update_type="SL_HIT",
        )

    return ParsedSignal(
        kind=Kind.UNKNOWN, message_id=mid, raw_text=body,
        reason="no order+stop-loss pattern and no recognised update",
    )


def to_producer_signal(p: ParsedSignal, timestamp: str = "", confidence: str = "") -> dict:
    """Map a parsed SIGNAL to the dict shape SignalIntelligenceProducer expects.

    TP1 becomes the single ``take_profit`` field; all TP levels are preserved in
    the summary so nothing is lost. Raises ValueError for non-tradeable shapes.
    """
    if not p.is_tradeable_shape():
        raise ValueError(f"ParsedSignal is not a tradeable SIGNAL shape: {p.kind}/{p.reason}")
    tp1 = p.take_profits[0] if p.take_profits else ""
    tp_str = ", ".join(p.take_profits) if p.take_profits else "n/a"
    return {
        "signal_id": p.message_id or f"{p.market}-{p.direction}-{p.entry}",
        "market": p.market,
        "direction": p.direction,
        "entry": p.entry,
        "stop_loss": p.stop_loss,
        "take_profit": tp1,
        # Wayond messages carry no timestamp/confidence; default rather than fail.
        # The ingestion command threads the real Telegram message date in here.
        "timestamp": timestamp or "n/a",
        "confidence": confidence or "0",
        "summary": f"{p.market} {p.direction} @ {p.entry} (SL {p.stop_loss}, TPs {tp_str})",
    }


@dataclass
class IngestPlan:
    """Result of classifying a batch of messages (no side effects)."""
    signals: list = field(default_factory=list)      # ParsedSignal (new, deduped)
    updates: list = field(default_factory=list)      # ParsedSignal (follow-ups)
    quarantined: list = field(default_factory=list)  # ParsedSignal (UNKNOWN)
    duplicates: list = field(default_factory=list)   # message_ids skipped


def classify_messages(messages, seen_ids=None) -> IngestPlan:
    """Classify a batch of {message_id, text} dicts into a dedup'd IngestPlan.

    ``seen_ids`` is a set of already-ingested message ids (idempotency / replay
    protection). Pure: callers decide what to persist.
    """
    seen = set(seen_ids or ())
    plan = IngestPlan()
    batch_seen = set()
    for m in messages:
        mid = str(m.get("message_id", ""))
        text = m.get("text", "")
        if mid and (mid in seen or mid in batch_seen):
            plan.duplicates.append(mid)
            continue
        if mid:
            batch_seen.add(mid)
        p = parse_message(text, mid)
        if p.kind == Kind.SIGNAL:
            plan.signals.append(p)
        elif p.kind == Kind.UPDATE:
            plan.updates.append(p)
        else:
            plan.quarantined.append(p)
    return plan
