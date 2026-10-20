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
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class MasterReadResult:
    """Outcome of a single read of a master's open positions. ``ok`` gates recording."""
    ok: bool
    reason: str
    observed_login: str = ""
    observed_server: str = ""
    observed_trade_mode: Optional[int] = None
    transport_reason: str = ""
    positions: tuple[ObservedPosition, ...] = field(default_factory=tuple)


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
    positions: list[ObservedPosition] = []
    if isinstance(raw_positions, list):
        for p in raw_positions:
            if isinstance(p, dict) and p.get("ticket") is not None:
                try:
                    positions.append(ObservedPosition.from_payload(p))
                except (KeyError, TypeError, ValueError):
                    # A malformed position row fails the WHOLE read closed — never a partial,
                    # possibly-misordered observation (packet §5: no inventing/assuming).
                    logger.warning("copier malformed position row for account %s", getattr(account, "id", None))
                    return MasterReadResult(False, "malformed_position", transport_reason=st.reason_code)
    return MasterReadResult(
        True, "ok", observed_login=obs_login, observed_server=obs_server,
        observed_trade_mode=(int(obs_trade_mode) if obs_trade_mode is not None else None),
        transport_reason=st.reason_code, positions=tuple(positions),
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
def record_observation(master: CopierMaster, result: MasterReadResult) -> dict:
    """Persist ONE identity-verified observation + its derived events, atomically, under a row lock.

    Must only be called with ``result.ok``. Creates the poll + snapshot rows, diffs against the
    master's previous poll, appends events with a monotone per-master sequence, and advances the
    baseline. A crash rolls back the whole unit (poll + events together)."""
    # Row lock serialises concurrent passes for this master (single-flight) and makes the
    # sequence allocation + baseline advance race-free.
    master = CopierMaster.objects.select_for_update().get(pk=master.pk)
    prev_poll = master.last_poll
    prev_positions = _positions_from_poll(prev_poll) if prev_poll is not None else []

    now = timezone.now()
    poll = MasterObservationPoll.objects.create(
        master=master,
        observed_at=now,
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


def observe_master(master: CopierMaster, *, fetch: Callable = fetch_master_positions) -> dict:
    """Observe one master: read + (on success) record. A refused read records nothing (honest)."""
    result = fetch(master.trading_account)
    if not result.ok:
        return {"master_id": master.pk, "ok": False, "reason": result.reason,
                "poll_id": None, "events": 0}
    summary = record_observation(master, result)
    summary.update({"master_id": master.pk, "ok": True, "reason": "ok"})
    return summary


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
