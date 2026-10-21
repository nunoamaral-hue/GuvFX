"""trade_copier.observer — read-only master observation + event-ledger recording (PR A).

Flow, per enabled master, STRICTLY read-only (never attaches, mutates, or orders):

  1. Resolve the master account's OWN per-tenant read bridge via ``snapshot_transport`` (fail-closed:
     a hosted account with no READY endpoint is refused, never read via a global/sibling bridge).
  2. GET ``/mt5/positions`` from that bridge and bind the batch to the observed session identity with
     ``verify_snapshot_identity`` (login EXACT == account_number, server casefold on the legacy global
     path, DEMO/LIVE environment). ANY mismatch / failure ⇒ the whole read is refused.
  3. Persist the observation atomically: one ``MasterObservationPoll`` + its ``MasterPositionSnapshot``
     rows, diff it against the master's previous poll (``diff_positions``), and append the resulting
     ``MasterEvent`` rows with a per-master monotone sequence — all in ONE transaction under a row
     lock, so a crash commits a poll AND its events or neither (no gap, no duplicate).

A refused/failed read creates NO poll and NO events and does NOT advance the diff baseline — the
observer never invents an observation and never assumes a missed change did not happen (packet §5).
Nothing here authorises or performs execution.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Optional

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from execution.snapshot_transport import resolve_account_snapshot_base, verify_snapshot_identity
from trade_copier.diff import ObservedPosition, diff_positions
from trade_copier.models import (
    CopierMaster,
    MasterEvent,
    MasterObservationPoll,
    MasterPositionSnapshot,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 10
_OBSERVATION_LEASE_SECONDS = 30  # bounded single-flight window per master (auto-expires)


@transaction.atomic
def _claim_observation_lease(master_pk, token: str, seconds: int) -> bool:
    """Claim the single-flight observation lease for a master BEFORE the remote read (ADR-0051
    BLOCKER-2). Returns False when another pass holds an unexpired lease — that pass then skips, so at
    most one observation per master is ever in flight and acquisition order == persist order."""
    m = CopierMaster.objects.select_for_update().get(pk=master_pk)
    now = timezone.now()
    if m.observation_lease_until and m.observation_lease_until > now:
        return False
    m.observation_lease_token = token
    m.observation_lease_until = now + timedelta(seconds=seconds)
    m.save(update_fields=["observation_lease_token", "observation_lease_until", "updated_at"])
    return True


@transaction.atomic
def _release_observation_lease(master_pk, token: str) -> None:
    """Release the lease iff this pass still owns it (a taken-over lease is left for its new owner)."""
    m = CopierMaster.objects.select_for_update().get(pk=master_pk)
    if m.observation_lease_token == token:
        m.observation_lease_token = ""
        m.observation_lease_until = None
        m.save(update_fields=["observation_lease_token", "observation_lease_until", "updated_at"])


@dataclass(frozen=True)
class MasterReadResult:
    """Outcome of a single read of a master's open positions. ``ok`` gates recording.

    ``read_at`` is stamped when the read completed; it provides the monotonic ordering used to
    discard a stale (older) read that arrives after a newer one has committed (ADR-0051 BLOCKER-2)."""
    ok: bool
    reason: str
    observed_login: str = ""
    observed_server: str = ""
    observed_trade_mode: Optional[int] = None
    transport_reason: str = ""
    positions: tuple[ObservedPosition, ...] = field(default_factory=tuple)
    read_at: Optional[datetime] = None


def _agent_config() -> tuple[str, str]:
    """Global (legacy) agent base + token — same env contract as analytics/execution reads."""
    base = (os.getenv("WINDOWS_AGENT_BASE") or os.getenv("GUVFX_AGENT_URL") or "").rstrip("/")
    token = (os.getenv("WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_AGENT_TOKEN") or "").strip()
    return base, token


def _http_get_json(url: str, token: str) -> Optional[dict]:
    req = urllib.request.Request(url, method="GET", headers={"X-GuvFX-Agent-Token": token})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:  # noqa: S310 (fixed internal scheme)
        raw = r.read().decode("utf-8", "ignore")
    data = json.loads(raw) if raw else {}
    return data if isinstance(data, dict) else None


def fetch_master_positions(account) -> MasterReadResult:
    """Read ``account``'s live open positions from its OWN bridge, identity-firewalled. Fail-closed.

    Single bridge call: ``/mt5/positions`` already returns the observed ``account_login`` /
    ``account_server`` / ``trade_mode`` from the SAME session that produced the positions, so the
    batch is bound to the observed identity directly (no separate snapshot call needed)."""
    global_base, token = _agent_config()
    if not token:
        return MasterReadResult(False, "no_agent_token")
    st = resolve_account_snapshot_base(account, global_base_url=global_base)
    if not st.ok:
        return MasterReadResult(False, st.reason_code, transport_reason=st.reason_code)
    try:
        pdata = _http_get_json(f"{st.base_url}/mt5/positions", token)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        logger.warning("copier positions read failed for account %s: %s", getattr(account, "id", None), e)
        return MasterReadResult(False, "positions_read_error", transport_reason=st.reason_code)
    if not (isinstance(pdata, dict) and pdata.get("ok")):
        return MasterReadResult(False, "positions_not_ok", transport_reason=st.reason_code)

    obs_login = str(pdata.get("account_login") or "")
    obs_server = str(pdata.get("account_server") or "")
    obs_trade_mode = pdata.get("trade_mode")
    legacy = not st.per_tenant
    idc = verify_snapshot_identity(
        account, obs_login, obs_server,
        require_server=legacy, observed_trade_mode=obs_trade_mode, require_environment=True,
    )
    if not idc.ok or (legacy and not obs_server.strip()):
        logger.warning("copier positions identity firewall refused account %s: %s",
                       getattr(account, "id", None), idc.reason_code)
        return MasterReadResult(False, idc.reason_code, transport_reason=st.reason_code)

    raw_positions = pdata.get("positions")
    # BLOCKER-1: a missing/null/non-list positions field — even with ok:true — is an INVALID or
    # ambiguous read, NOT a confirmed-flat account. Fail closed (record nothing, keep the baseline).
    # Only an actual list (incl. an empty list from a successful positions_get) is authoritative; the
    # bridge now reports a failed positions_get as an error (never an empty list), so ok:true + [] is
    # a genuine flat account.
    if not isinstance(raw_positions, list):
        logger.warning("copier positions response not a list for account %s", getattr(account, "id", None))
        return MasterReadResult(False, "positions_not_a_list", transport_reason=st.reason_code)
    positions: list[ObservedPosition] = []
    seen_tickets: set[int] = set()
    for p in raw_positions:
        if not (isinstance(p, dict) and p.get("ticket") is not None):
            logger.warning("copier malformed position row for account %s", getattr(account, "id", None))
            return MasterReadResult(False, "malformed_position", transport_reason=st.reason_code)
        try:
            op = ObservedPosition.from_payload(p)
        except (KeyError, TypeError, ValueError):
            # A malformed row fails the WHOLE read closed — never a partial, possibly-misordered
            # observation (packet §5: no inventing/assuming).
            logger.warning("copier malformed position row for account %s", getattr(account, "id", None))
            return MasterReadResult(False, "malformed_position", transport_reason=st.reason_code)
        if op.ticket in seen_tickets:
            # Duplicate tickets make the open-position set ambiguous → fail closed.
            logger.warning("copier duplicate position ticket for account %s", getattr(account, "id", None))
            return MasterReadResult(False, "duplicate_position_ticket", transport_reason=st.reason_code)
        seen_tickets.add(op.ticket)
        positions.append(op)
    return MasterReadResult(
        True, "ok", observed_login=obs_login, observed_server=obs_server,
        observed_trade_mode=(int(obs_trade_mode) if obs_trade_mode is not None else None),
        transport_reason=st.reason_code, positions=tuple(positions),
        read_at=timezone.now(),
    )


def _level_to_decimal(v: Optional[float]):
    """A protective level for the durable store: ``None`` when absent (MT5 0.0 == no level)."""
    if v is None or abs(float(v)) <= 1e-9:
        return None
    return Decimal(str(v))


def _num_to_decimal(v: Optional[float]):
    if v is None:
        return None
    return Decimal(str(v))


def _positions_from_poll(poll: MasterObservationPoll) -> list[ObservedPosition]:
    """Rebuild the pure ObservedPosition list from a persisted poll's snapshot rows (diff baseline)."""
    out: list[ObservedPosition] = []
    for row in poll.positions.all():
        out.append(ObservedPosition(
            ticket=int(row.position_ticket),
            symbol=row.symbol,
            side=row.side,
            volume=float(row.volume),
            sl=(float(row.sl) if row.sl is not None else None),
            tp=(float(row.tp) if row.tp is not None else None),
            price_open=(float(row.price_open) if row.price_open is not None else None),
            price_current=(float(row.price_current) if row.price_current is not None else None),
            magic=(int(row.magic) if row.magic is not None else None),
            comment=row.comment,
        ))
    return out


@transaction.atomic
def record_observation(master: CopierMaster, result: MasterReadResult, *, lease_token=None) -> dict:
    """Persist ONE identity-verified observation + its derived events, atomically, under a row lock.

    Must only be called with ``result.ok``. Creates the poll + snapshot rows, diffs against the
    master's previous poll, appends events with a monotone per-master sequence, and advances the
    baseline. A crash rolls back the whole unit (poll + events together)."""
    # Row lock serialises concurrent passes for this master and makes the sequence allocation +
    # baseline advance race-free. The network read happened BEFORE this lock (so the lock is never
    # held across the network).
    master = CopierMaster.objects.select_for_update().get(pk=master.pk)
    now = timezone.now()

    # BLOCKER-2 (primary ordering guarantee): if this pass's single-flight lease was lost — it expired
    # and a newer pass took over — its result is stale. Discard it: never commit a snapshot whose
    # single-flight ownership lapsed (this is what makes a network-delayed older read unable to win).
    if lease_token is not None and (master.observation_lease_token != lease_token
                                    or not master.observation_lease_until
                                    or master.observation_lease_until <= now):
        logger.warning("copier discarding read for master %s: observation lease lost", master.pk)
        return {"poll_id": None, "positions": len(result.positions), "events": 0,
                "first_poll": False, "lease_lost": True}

    prev_poll = master.last_poll

    # Secondary backstop to single-flight: discard a read whose read_at is not strictly newer than the
    # baseline's. With single-flight this rarely triggers, but it guards a --force diagnostic pass or
    # any path that bypasses the lease. (Skipped when read_at is absent — e.g. a synthetic caller.)
    read_at = result.read_at or now
    if prev_poll is not None and result.read_at is not None and prev_poll.read_at is not None \
            and result.read_at <= prev_poll.read_at:
        logger.warning("copier discarding stale read for master %s (read_at %s <= baseline %s)",
                       master.pk, result.read_at, prev_poll.read_at)
        return {"poll_id": None, "positions": len(result.positions), "events": 0,
                "first_poll": False, "stale_discarded": True}

    prev_positions = _positions_from_poll(prev_poll) if prev_poll is not None else []
    poll = MasterObservationPoll.objects.create(
        master=master,
        observed_at=now,
        read_at=read_at,
        observed_login=result.observed_login,
        observed_server=result.observed_server,
        observed_trade_mode=result.observed_trade_mode,
        position_count=len(result.positions),
        transport_reason=result.transport_reason,
    )
    for p in result.positions:
        MasterPositionSnapshot.objects.create(
            poll=poll,
            position_ticket=p.ticket,
            symbol=p.symbol,
            side=p.side,
            volume=Decimal(str(p.volume)),
            sl=_level_to_decimal(p.sl),
            tp=_level_to_decimal(p.tp),
            price_open=_num_to_decimal(p.price_open),
            price_current=_num_to_decimal(p.price_current),
            magic=(int(p.magic) if p.magic is not None else None),
            comment=(p.comment or "")[:128],
        )

    # The FIRST ever poll establishes the BASELINE only — pre-existing open positions predate
    # observation and must NOT be emitted as OPEN events (the observer never invents events; and for
    # copy semantics, enabling observation must not retroactively mirror positions already open).
    specs = diff_positions(prev_positions, list(result.positions)) if prev_poll is not None else []
    base_seq = MasterEvent.objects.filter(master=master).aggregate(m=Max("sequence"))["m"] or 0
    created = 0
    for i, spec in enumerate(specs, start=1):
        MasterEvent.objects.create(
            master=master,
            sequence=base_seq + i,
            from_poll=poll,
            event_type=spec.event_type,
            position_ticket=spec.ticket,
            symbol=spec.symbol,
            side=spec.side,
            volume_before=Decimal(str(spec.volume_before)),
            volume_after=Decimal(str(spec.volume_after)),
            volume_delta=Decimal(str(spec.volume_delta)),
            prev_sl=_num_to_decimal(spec.prev_sl),
            new_sl=_num_to_decimal(spec.new_sl),
            prev_tp=_num_to_decimal(spec.prev_tp),
            new_tp=_num_to_decimal(spec.new_tp),
            observed_at=now,
        )
        created += 1

    master.last_poll = poll
    master.last_observed_at = now
    master.save(update_fields=["last_poll", "last_observed_at", "updated_at"])

    return {"poll_id": poll.pk, "positions": len(result.positions), "events": created,
            "first_poll": prev_poll is None}


def observe_master(master: CopierMaster, *, fetch: Callable = fetch_master_positions,
                   lease_seconds: int = _OBSERVATION_LEASE_SECONDS) -> dict:
    """Observe one master under a bounded single-flight lease (ADR-0051 BLOCKER-2).

    The lease is claimed BEFORE the remote read, so at most one observation per master is in flight;
    a second concurrent pass skips (``observation_in_flight``) and reads fresh on its next cycle. A
    refused read records nothing. The lease is released in ``finally``; if it had already expired and
    been taken over, ``record_observation`` discards this (stale) result (``lease_lost``)."""
    token = uuid.uuid4().hex
    if not _claim_observation_lease(master.pk, token, lease_seconds):
        return {"master_id": master.pk, "ok": False, "reason": "observation_in_flight",
                "poll_id": None, "events": 0}
    try:
        result = fetch(master.trading_account)
        if not result.ok:
            return {"master_id": master.pk, "ok": False, "reason": result.reason,
                    "poll_id": None, "events": 0}
        summary = record_observation(master, result, lease_token=token)
        if summary.get("stale_discarded"):
            reason = "stale_read_discarded"
        elif summary.get("lease_lost"):
            reason = "lease_lost"
        else:
            reason = "ok"
        summary.update({"master_id": master.pk, "ok": True, "reason": reason})
        return summary
    finally:
        _release_observation_lease(master.pk, token)


def run_observer_pass(*, limit: Optional[int] = None, fetch: Callable = fetch_master_positions) -> dict:
    """One observation pass over all enabled masters. Returns a secret-free summary.

    One master's failure never aborts the pass (independent, fail-closed per master)."""
    qs = CopierMaster.objects.filter(observe_enabled=True).order_by("id")
    if limit:
        qs = qs[:limit]
    results = []
    for master in qs:
        try:
            results.append(observe_master(master, fetch=fetch))
        except Exception as e:  # noqa: BLE001 — one master's error must not sink the pass
            logger.exception("copier observe_master failed for master %s: %s", master.pk, e)
            results.append({"master_id": master.pk, "ok": False, "reason": "observe_exception",
                            "poll_id": None, "events": 0})
    ok = sum(1 for r in results if r.get("ok"))
    events = sum(r.get("events", 0) for r in results)
    return {"masters": len(results), "ok": ok, "refused": len(results) - ok,
            "events": events, "results": results}
