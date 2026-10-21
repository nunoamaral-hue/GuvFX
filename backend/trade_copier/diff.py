"""trade_copier.diff — PURE master-position diff engine (no Django, no I/O).

Given the previous and current observed open-position sets for ONE master account, compute the
ordered, deterministic list of events that occurred between the two observations. This is the
side-effect-free core of the observer (``trade_copier.observer``); it is unit-tested without a
database or a bridge (synthetic certification per ``.claude/rules/evidence.md``).

Observation is strictly READ-ONLY: this module decides WHAT changed, it never issues a command and
never mutates the master.

Detection, per position ticket:

* ``OPEN``            — ticket present now, absent before
* ``FULL_CLOSE``      — ticket present before, absent now
* ``PARTIAL_CLOSE``   — volume decreased
* ``VOLUME_INCREASE`` — volume increased (scale-in / additional fill)
* ``SL_SET`` / ``SL_CHANGE`` / ``SL_REMOVE`` — stop-loss appeared / moved / cleared
* ``TP_SET`` / ``TP_CHANGE`` / ``TP_REMOVE`` — take-profit appeared / moved / cleared

Deliberate honesty limits (see ADR-0051 §3 of the day-1 map):

* A ``FULL_CLOSE`` carries NO reason. The bridge deals feed does not expose ``deal.reason`` today,
  so SL-hit vs TP-hit vs stop-out vs manual close is UNKNOWN. We emit ``FULL_CLOSE`` and leave the
  reason empty rather than guess.
* Pending orders are not represented here — the positions snapshot is market positions only.
* MT5 encodes "no stop" as ``0.0``. ``None`` and ``0.0`` are both treated as "no level".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

# Event-type constants (string-stable; mirrored by MasterEvent.EventType in models.py).
OPEN = "OPEN"
FULL_CLOSE = "FULL_CLOSE"
PARTIAL_CLOSE = "PARTIAL_CLOSE"
VOLUME_INCREASE = "VOLUME_INCREASE"
SL_SET = "SL_SET"
SL_CHANGE = "SL_CHANGE"
SL_REMOVE = "SL_REMOVE"
TP_SET = "TP_SET"
TP_CHANGE = "TP_CHANGE"
TP_REMOVE = "TP_REMOVE"

# Deterministic emission order for the events of a SINGLE ticket within one poll. Lifecycle first
# (open → volume change → close), then protective-level changes; a FULL_CLOSE suppresses the rest.
_TYPE_ORDER = {
    OPEN: 0,
    VOLUME_INCREASE: 1,
    PARTIAL_CLOSE: 2,
    SL_SET: 3, SL_CHANGE: 3, SL_REMOVE: 3,
    TP_SET: 4, TP_CHANGE: 4, TP_REMOVE: 4,
    FULL_CLOSE: 5,
}

# Volume is quoted in lots; the smallest MT5 lot step is 0.01. Half a step is a safe equality
# tolerance that cannot mistake a genuine partial close (>= one step) for float noise.
_VOLUME_EPS = 0.005
# Prices: SL/TP equality tolerance. Smaller than any instrument's minimal stop move; guards float
# round-trips through JSON without masking a real modification.
_PRICE_EPS = 1e-9


@dataclass(frozen=True)
class ObservedPosition:
    """One open market position as observed on the master terminal (read-only).

    ``sl``/``tp`` are ``None`` or ``0.0`` when no level is set. All other fields are carried verbatim
    from the bridge ``/mt5/positions`` payload (plus the additive ``sl``/``tp`` fields — see
    ``scripts/mt5_signal_bridge.py`` ``fetch_positions``)."""
    ticket: int
    symbol: str
    side: str
    volume: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    price_open: Optional[float] = None
    price_current: Optional[float] = None
    magic: Optional[int] = None
    comment: str = ""

    @classmethod
    def from_payload(cls, p: dict) -> "ObservedPosition":
        """Build from a raw bridge position dict, tolerating missing/typed fields."""
        return cls(
            ticket=int(p["ticket"]),
            symbol=str(p.get("symbol") or ""),
            side=str(p.get("side") or ("BUY" if p.get("type") == 0 else "SELL")),
            volume=float(p.get("volume") or 0.0),
            sl=_to_level(p.get("sl")),
            tp=_to_level(p.get("tp")),
            price_open=_to_float_or_none(p.get("price_open")),
            price_current=_to_float_or_none(p.get("price_current")),
            magic=_to_int_or_none(p.get("magic")),
            comment=str(p.get("comment") or ""),
        )


@dataclass(frozen=True)
class EventSpec:
    """A detected change. ``signature`` disambiguates one event-type per ticket within a poll."""
    event_type: str
    ticket: int
    symbol: str
    side: str
    volume_before: float
    volume_after: float
    prev_sl: Optional[float] = None
    new_sl: Optional[float] = None
    prev_tp: Optional[float] = None
    new_tp: Optional[float] = None

    @property
    def volume_delta(self) -> float:
        return round(self.volume_after - self.volume_before, 6)


def _to_float_or_none(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int_or_none(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_level(v) -> Optional[float]:
    """A protective level: ``None`` when unset. MT5's ``0.0`` means "no level"."""
    f = _to_float_or_none(v)
    if f is None:
        return None
    return f


def _has_level(v: Optional[float]) -> bool:
    return v is not None and abs(v) > _PRICE_EPS


def _levels_differ(a: Optional[float], b: Optional[float]) -> bool:
    return abs((a or 0.0) - (b or 0.0)) > _PRICE_EPS


def _index(positions: Iterable[ObservedPosition]) -> dict[int, ObservedPosition]:
    out: dict[int, ObservedPosition] = {}
    for p in positions:
        out[p.ticket] = p  # last-wins; a well-formed snapshot has unique tickets
    return out


def _level_events(prev: ObservedPosition, new: ObservedPosition) -> list[EventSpec]:
    """SL/TP set/change/remove events for a position present in BOTH snapshots."""
    events: list[EventSpec] = []
    # Stop-loss
    if _levels_differ(prev.sl, new.sl):
        if not _has_level(prev.sl) and _has_level(new.sl):
            etype = SL_SET
        elif _has_level(prev.sl) and not _has_level(new.sl):
            etype = SL_REMOVE
        else:
            etype = SL_CHANGE
        events.append(EventSpec(etype, new.ticket, new.symbol, new.side,
                                prev.volume, new.volume, prev_sl=prev.sl, new_sl=new.sl))
    # Take-profit
    if _levels_differ(prev.tp, new.tp):
        if not _has_level(prev.tp) and _has_level(new.tp):
            etype = TP_SET
        elif _has_level(prev.tp) and not _has_level(new.tp):
            etype = TP_REMOVE
        else:
            etype = TP_CHANGE
        events.append(EventSpec(etype, new.ticket, new.symbol, new.side,
                                prev.volume, new.volume, prev_tp=prev.tp, new_tp=new.tp))
    return events


def diff_positions(prev: Iterable[ObservedPosition],
                   new: Iterable[ObservedPosition]) -> list[EventSpec]:
    """Deterministic diff of two open-position snapshots for one master account.

    Returns events ordered by ``(ticket, lifecycle-order)`` so the output is stable and replayable.
    Pure: no DB, no network, no clock.
    """
    prev_idx = _index(prev)
    new_idx = _index(new)
    events: list[EventSpec] = []

    for ticket in set(prev_idx) | set(new_idx):
        before = prev_idx.get(ticket)
        after = new_idx.get(ticket)

        if before is None and after is not None:
            # New position opened this interval.
            events.append(EventSpec(OPEN, after.ticket, after.symbol, after.side,
                                    0.0, after.volume, new_sl=after.sl, new_tp=after.tp))
            continue

        if before is not None and after is None:
            # Position gone — fully closed (reason unknown; see module docstring).
            events.append(EventSpec(FULL_CLOSE, before.ticket, before.symbol, before.side,
                                    before.volume, 0.0, prev_sl=before.sl, prev_tp=before.tp))
            continue

        # Present in both: volume change and/or protective-level change.
        assert before is not None and after is not None
        dv = after.volume - before.volume
        if dv < -_VOLUME_EPS:
            events.append(EventSpec(PARTIAL_CLOSE, after.ticket, after.symbol, after.side,
                                    before.volume, after.volume))
        elif dv > _VOLUME_EPS:
            events.append(EventSpec(VOLUME_INCREASE, after.ticket, after.symbol, after.side,
                                    before.volume, after.volume))
        events.extend(_level_events(before, after))

    events.sort(key=lambda e: (e.ticket, _TYPE_ORDER.get(e.event_type, 99)))
    return events
