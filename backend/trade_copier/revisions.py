"""trade_copier.revisions — the HYBRID detection-source contract + monotonic-revision stale gate.

PURE (no Django, no I/O, no clock). This module models the *fast* half of the hybrid detection design
(ADR-0071): a bridge that emits a position-state **revision** promptly when it observes a change, each
revision carrying a monotonic per-(account, ticket) ``revision`` ordinal so stale / out-of-order / duplicate
emissions are rejected deterministically. The *slow* half — the authoritative reconciliation poll — already
exists (``trade_copier.observer``); it remains the correctness backstop and is never replaced by this feed.

Two monotone ordering keys exist, each monotone **within its own source** and never compared across sources:

* POLL path  — ``MasterEvent.sequence`` (per-master, allocated under a row lock by the observer). This is the
  AUTHORITATIVE ordering used by the lifecycle (``trade_copier.lifecycle``).
* EVENT path — the per-(account, ticket) ``revision`` carried by each emitted ``PositionRevision``. It is used
  ONLY at ingestion, to fold a stream of emissions into a current open-position set (equivalent to one poll
  snapshot) which the EXISTING pure diff engine (``trade_copier.diff``) then turns into canonical
  ``MasterEvent`` rows. The event path therefore never introduces a second, parallel event namespace: it is a
  faster *producer of poll-equivalent snapshots*, and the poll remains able to correct it.

DARK / repository-only (packet GFX-COPIER-HYBRID-CHANGE-001): this module defines the contract and the pure
ingestion fold. It does NOT open a network endpoint and the live bridge is NOT modified or deployed — wiring a
signed ingestion endpoint + a bridge emitter is a named next governed gate (see the ADR). Nothing here places,
modifies or closes an order.

Bridge emit contract (what a future emitter MUST provide per revision):

    account_id        int      the master account the revision is about (identity bound upstream, as today)
    ticket            int      the broker position ticket (immutable identity of the position)
    revision          int      monotonic, strictly increasing per (account_id, ticket); starts at 1 on OPEN
    closed            bool      True when the position is gone (full close) — carries no sl/tp/volume
    symbol/side       str      carried verbatim; side in {BUY, SELL}
    volume            float    current remaining volume in lots (absent/ignored when closed)
    sl/tp             float?    current protective levels; None or 0.0 == "no level" (same as the poll path)
    broker_event_time datetime the broker's own timestamp for the change (true detection-latency reference)
    observed_time     datetime when the bridge observed/emitted it

``broker_event_time`` is the single honest detection-latency reference; the poll path cannot provide it (MT5's
positions feed exposes no per-change broker timestamp — see ``trade_copier.diff`` docstring), which the
latency model (``trade_copier.cadence``) states explicitly rather than inventing one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable, Optional

from trade_copier.diff import ObservedPosition, _to_level  # reuse the poll path's "0.0 == no level" rule


class DetectionSource(str, Enum):
    """Where a detected change came from. The lifecycle treats POLL as authoritative; EVENT is the fast hint."""
    POLL = "POLL"
    EVENT = "EVENT"


# A revision ordinal must be a strictly-positive int; 0/negative are structurally invalid (never "just stale").
_MIN_REVISION = 1


@dataclass(frozen=True)
class PositionRevision:
    """One emitted position-state revision from the fast EVENT path (the bridge emit contract above).

    Immutable. ``revision`` is the monotonic per-(account, ticket) ordinal that makes stale/out-of-order/
    duplicate rejection deterministic WITHOUT a wall-clock comparison. A ``closed`` revision represents the
    position's disappearance and carries no live levels."""
    account_id: int
    ticket: int
    revision: int
    observed_time: datetime
    broker_event_time: Optional[datetime] = None
    closed: bool = False
    symbol: str = ""
    side: str = ""
    volume: float = 0.0
    sl: Optional[float] = None
    tp: Optional[float] = None

    def structurally_valid(self) -> tuple[bool, str]:
        """Reject a structurally-malformed revision (fail closed — never silently coerce).

        A malformed revision is NOT merely stale: it is dropped and reported, never folded into the snapshot."""
        if not isinstance(self.ticket, int) or self.ticket <= 0:
            return False, "bad_ticket"
        if not isinstance(self.revision, int) or self.revision < _MIN_REVISION:
            return False, "bad_revision"
        if self.observed_time is None:
            return False, "no_observed_time"
        if not self.closed:
            if self.side not in ("BUY", "SELL"):
                return False, "bad_side"
            try:
                if float(self.volume) <= 0:
                    return False, "non_positive_volume"
            except (TypeError, ValueError):
                return False, "non_numeric_volume"
        return True, "ok"

    def to_observed_position(self) -> ObservedPosition:
        """Project an OPEN (non-closed) revision into the diff engine's ``ObservedPosition``.

        Uses the SAME ``0.0 == no level`` normalisation as the poll path so an event-sourced snapshot diffs
        identically to a poll-sourced one. Must not be called on a ``closed`` revision (it has no position)."""
        if self.closed:
            raise ValueError("a closed revision has no position")
        return ObservedPosition(
            ticket=int(self.ticket),
            symbol=str(self.symbol or ""),
            side=str(self.side or ""),
            volume=float(self.volume or 0.0),
            sl=_to_level(self.sl),
            tp=_to_level(self.tp),
        )


@dataclass(frozen=True)
class FoldResult:
    """Outcome of folding a revision stream into a current snapshot (pure)."""
    positions: tuple[ObservedPosition, ...]
    applied_revision: dict[int, int]     # ticket -> highest revision applied (the per-ticket high-water mark)
    rejected: tuple[tuple[int, int, str], ...]   # (ticket, revision, reason) for each dropped emission


def is_stale(incoming_revision: int, last_applied_revision: Optional[int]) -> bool:
    """Monotone stale rule: an emission is stale/duplicate/out-of-order iff its revision is NOT strictly
    greater than the last applied revision for that ticket. ``None`` last ⇒ nothing applied yet ⇒ not stale.

    This is the whole correctness guarantee of the fast path: within a ticket, only a strictly-newer revision
    advances the snapshot; a replay or a re-ordered-older emission can never move it backwards."""
    if last_applied_revision is None:
        return False
    return int(incoming_revision) <= int(last_applied_revision)


def fold_revisions(revisions: Iterable[PositionRevision], *,
                   prior_applied: Optional[dict[int, int]] = None) -> FoldResult:
    """Fold an UNORDERED stream of EVENT-path revisions for ONE account into a current open-position set.

    Deterministic and idempotent. Per ticket, the HIGHEST valid revision wins (strict-monotone gate), so the
    order in which emissions arrive does not matter and a duplicate/older emission is rejected. ``prior_applied``
    seeds the per-ticket high-water marks from a previous fold, so re-feeding an already-seen revision is a
    no-op (replay safety across calls). A ``closed`` winning revision removes the ticket from the snapshot.

    Returns the resulting positions plus the advanced high-water marks and a sanitised list of rejected
    emissions (stale or structurally invalid) for telemetry — never raised, never silently discarded."""
    hwm: dict[int, int] = dict(prior_applied or {})
    winner: dict[int, PositionRevision] = {}
    rejected: list[tuple[int, int, str]] = []

    # First pass: pick the strictly-newest structurally-valid revision per ticket, above any prior hwm.
    for rev in revisions:
        ok, reason = rev.structurally_valid()
        if not ok:
            rejected.append((int(getattr(rev, "ticket", 0) or 0), int(getattr(rev, "revision", 0) or 0), reason))
            continue
        current_best = winner.get(rev.ticket)
        best_rev = current_best.revision if current_best is not None else hwm.get(rev.ticket)
        if is_stale(rev.revision, best_rev):
            rejected.append((rev.ticket, rev.revision, "stale_revision"))
            continue
        winner[rev.ticket] = rev

    # Second pass: materialise the snapshot + advance the high-water marks.
    positions: list[ObservedPosition] = []
    for ticket, rev in winner.items():
        hwm[ticket] = rev.revision
        if not rev.closed:
            positions.append(rev.to_observed_position())
    positions.sort(key=lambda p: p.ticket)
    return FoldResult(positions=tuple(positions), applied_revision=hwm, rejected=tuple(rejected))
