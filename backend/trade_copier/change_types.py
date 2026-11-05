"""trade_copier.change_types — PURE Phase-5 change classification (ADR-0074).

Maps the existing pure diff engine's per-ticket ``EventSpec`` output (``trade_copier.diff``) onto the explicit,
named master-change taxonomy the completion packet enumerates. It adds NOTHING to detection semantics: the
diff engine already compares only the *structural* fields (ticket / volume / sl / tp) with its volume and price
epsilons, so floating price, floating P&L, metadata ordering and formatting-only decimal differences never
produce a change here — they are simply never diffed. This module only *names* what diff already found, and
collapses a simultaneous SL-and-TP change on one ticket into the composite ``SL_TP_CHANGED_TOGETHER``.

No Django, no I/O, no clock. Reused by the signed event-ingestion detection path and by tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from trade_copier import diff
from trade_copier.diff import EventSpec, ObservedPosition

# ─── the named change taxonomy (packet Phase 5) ───
ENTRY_OPENED = "ENTRY_OPENED"
STOP_LOSS_ADDED = "STOP_LOSS_ADDED"
STOP_LOSS_CHANGED = "STOP_LOSS_CHANGED"
STOP_LOSS_REMOVED = "STOP_LOSS_REMOVED"
TAKE_PROFIT_ADDED = "TAKE_PROFIT_ADDED"
TAKE_PROFIT_CHANGED = "TAKE_PROFIT_CHANGED"
TAKE_PROFIT_REMOVED = "TAKE_PROFIT_REMOVED"
SL_TP_CHANGED_TOGETHER = "SL_TP_CHANGED_TOGETHER"
VOLUME_REDUCED = "VOLUME_REDUCED"        # a volume reduction on an open position == a partial close in MT5
PARTIAL_CLOSE = "PARTIAL_CLOSE"
VOLUME_INCREASED = "VOLUME_INCREASED"    # scale-in / additional fill (not propagated — recorded for evidence)
POSITION_CLOSED = "POSITION_CLOSED"

# diff event-type → named change type (1:1; the SL/TP pair is collapsed separately below).
_DIFF_TO_CHANGE = {
    diff.OPEN: ENTRY_OPENED,
    diff.FULL_CLOSE: POSITION_CLOSED,
    diff.PARTIAL_CLOSE: PARTIAL_CLOSE,
    diff.VOLUME_INCREASE: VOLUME_INCREASED,
    diff.SL_SET: STOP_LOSS_ADDED,
    diff.SL_CHANGE: STOP_LOSS_CHANGED,
    diff.SL_REMOVE: STOP_LOSS_REMOVED,
    diff.TP_SET: TAKE_PROFIT_ADDED,
    diff.TP_CHANGE: TAKE_PROFIT_CHANGED,
    diff.TP_REMOVE: TAKE_PROFIT_REMOVED,
}

_SL_TYPES = frozenset({diff.SL_SET, diff.SL_CHANGE, diff.SL_REMOVE})
_TP_TYPES = frozenset({diff.TP_SET, diff.TP_CHANGE, diff.TP_REMOVE})


@dataclass(frozen=True)
class DetectedChange:
    """One named change for one ticket. ``source_event_types`` keeps the underlying diff types (so a
    composite SL_TP_CHANGED_TOGETHER remains auditable back to its SL and TP parts)."""
    change_type: str
    ticket: int
    symbol: str
    side: str
    volume_before: float
    volume_after: float
    prev_sl: Optional[float] = None
    new_sl: Optional[float] = None
    prev_tp: Optional[float] = None
    new_tp: Optional[float] = None
    source_event_types: tuple[str, ...] = ()


def change_type_for(event_type: str) -> str:
    """The named change type for one diff event-type (no collapsing). ``""`` for an unknown type."""
    return _DIFF_TO_CHANGE.get(event_type, "")


def classify_events(events: Iterable[EventSpec]) -> list[DetectedChange]:
    """Map an ordered ``EventSpec`` list (one master's diff) onto named ``DetectedChange`` rows.

    Per ticket, a simultaneous SL-family AND TP-family change collapses into a single
    ``SL_TP_CHANGED_TOGETHER`` carrying both deltas; everything else maps 1:1. Deterministic and order-stable
    (grouped by ticket, in first-seen order)."""
    by_ticket: dict[int, list[EventSpec]] = {}
    order: list[int] = []
    for ev in events:
        if ev.ticket not in by_ticket:
            by_ticket[ev.ticket] = []
            order.append(ev.ticket)
        by_ticket[ev.ticket].append(ev)

    out: list[DetectedChange] = []
    for ticket in order:
        group = by_ticket[ticket]
        sl_ev = next((e for e in group if e.event_type in _SL_TYPES), None)
        tp_ev = next((e for e in group if e.event_type in _TP_TYPES), None)
        handled: set[int] = set()
        if sl_ev is not None and tp_ev is not None:
            out.append(DetectedChange(
                change_type=SL_TP_CHANGED_TOGETHER, ticket=ticket, symbol=sl_ev.symbol, side=sl_ev.side,
                volume_before=sl_ev.volume_before, volume_after=sl_ev.volume_after,
                prev_sl=sl_ev.prev_sl, new_sl=sl_ev.new_sl, prev_tp=tp_ev.prev_tp, new_tp=tp_ev.new_tp,
                source_event_types=(sl_ev.event_type, tp_ev.event_type)))
            handled.add(id(sl_ev))
            handled.add(id(tp_ev))
        for ev in group:
            if id(ev) in handled:
                continue
            ct = change_type_for(ev.event_type)
            if not ct:
                continue
            out.append(DetectedChange(
                change_type=ct, ticket=ticket, symbol=ev.symbol, side=ev.side,
                volume_before=ev.volume_before, volume_after=ev.volume_after,
                prev_sl=ev.prev_sl, new_sl=ev.new_sl, prev_tp=ev.prev_tp, new_tp=ev.new_tp,
                source_event_types=(ev.event_type,)))
    return out


def classify_position_changes(prev: Iterable[ObservedPosition],
                              new: Iterable[ObservedPosition]) -> list[DetectedChange]:
    """Classify the change between two open-position snapshots for one master (the EVENT-path detector).

    Thin wrapper over the authoritative pure ``diff.diff_positions`` — so an event-sourced snapshot and a
    poll-sourced snapshot classify identically. No look-ahead, no invented reason."""
    return classify_events(diff.diff_positions(prev, new))
