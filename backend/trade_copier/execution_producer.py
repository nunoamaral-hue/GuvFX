"""trade_copier.execution_producer — gated DEMO-only ExecutionJob producer from replication intents (B-exec).

Turns recorded OPEN replication intents into enqueue-only ``ExecutionJob`` rows, DEMO followers ONLY,
under a FRESH admission re-check performed INSIDE the transactional boundary (never trusting values read
before the lock). Hard rails (ADR-0051 R2 / Friday packet §5):

* **P0-A (flag bypass)** — ``copier_execute_enabled()`` is enforced at the entry of every public write
  seam AND re-checked under the lock; the SHADOW/DEMO mode is captured ONCE and used consistently, so a
  mid-operation flag flip cannot change the produced job. Flags OFF ⇒ no job, no state mutation.
* **P0-B (TOCTOU)** — inside the lock we re-read + re-validate the replication, the follower
  (enabled/config/owner/binding), the destination account (env/active), the symbol, the FRESH broker
  constraints, and the master state; a disabled/rebound/deactivated follower, owner mismatch, stale OPEN
  (superseded by a later CLOSE, or outside the freshness window) is rejected. No broker network call is
  made under the lock.
* **P0-C (payload)** — ``lots`` is a valid positive number sized against FRESH broker constraints; the
  payload carries source/max-lot/provenance (copier ``CPY<id>`` comment); a below-min or clamp-altered
  volume is rejected rather than silently shrunk and reported as success.
* **P0-D (protection/facts)** — an executable OPEN requires complete, directionally-valid SL+TP and
  FRESH authoritative broker constraints; missing/stale/ambiguous/non-authoritative facts are rejected.
* **P1 (SHADOW semantics)** — a suppressed SHADOW job sets ``SHADOW_JOB_RECORDED`` (NOT
  EXECUTION_REQUESTED). Only a real armed DEMO ``PLACE_ORDER`` sets ``EXECUTION_REQUESTED``; only broker
  evidence may ever become ``EXECUTION_VERIFIED``.
* Idempotent (one job per intent, row-locked). Inherits every ``ExecutionJob.save()`` gate. Never LIVE,
  never a bridge-wall bypass. BUILD != ARM — arming + any real DEMO run is operator-gated.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from execution.models import ExecutionJob
from trade_copier import diff
from trade_copier.flags import copier_execute_demo_armed, copier_execute_enabled
from trade_copier.models import CopierFollower, CopierReplication, MasterEvent, ReplicationStatus
from trade_copier.replication import (
    _broker_constraints,
    _cfg,
    _master_position_from_event,
    validate_follower_binding,
)
from trade_copier.sizing import compute_follower_volume
from trade_copier.symbols import resolve_follower_symbol

logger = logging.getLogger(__name__)

_PRODUCIBLE_OPS = {"OPEN"}          # SCALE_IN later; CLOSE/MODIFY need a verified follower ticket.
_FRESHNESS_SECONDS = 3600          # an OPEN older than this is stale (recovery window) — not produced.
_EPS = 1e-9


def _has(v) -> bool:
    return v is not None and abs(float(v)) > _EPS


def _superseded_by_close(event: MasterEvent) -> bool:
    """True when a LATER FULL_CLOSE for the same master position exists — the OPEN is stale."""
    return (MasterEvent.objects
            .filter(master_id=event.master_id, position_ticket=event.position_ticket,
                    event_type=diff.FULL_CLOSE, sequence__gt=event.sequence)
            .exists())


def _protection_ok(snap) -> tuple[bool, str]:
    """An executable OPEN requires COMPLETE, directionally-valid SL AND TP (packet §5 P0-D)."""
    if snap is None or not _has(snap.price_open):
        return False, "master_entry_unavailable"
    if not (_has(snap.sl) and _has(snap.tp)):
        return False, "incomplete_protection"   # require complete SL+TP for an executable OPEN
    entry, sl, tp, side = float(snap.price_open), float(snap.sl), float(snap.tp), snap.side
    if side == "BUY" and not (sl < entry < tp):
        return False, "protection_not_directional"
    if side == "SELL" and not (tp < entry < sl):
        return False, "protection_not_directional"
    return True, "ok"


def admit_open(replication: CopierReplication) -> tuple[bool, str]:
    """Fail-closed admission for producing a DEMO OPEN. Safe to call before AND inside the lock."""
    if replication.execution_job_id is not None:
        return False, "already_produced"
    if replication.status != ReplicationStatus.SHADOW_RECORDED:
        return False, "not_shadow_intent"
    if replication.requested_op not in _PRODUCIBLE_OPS:
        return False, "op_not_producible"            # CLOSE/MODIFY/SCALE_IN: fail-closed here
    follower = replication.follower
    if not follower.enabled:
        return False, "follower_disabled"

    acct = follower.follower_account
    # DEMO ONLY — never a LIVE/ambiguous destination (fail-closed).
    try:
        from trading.account_policy import is_demo_environment
        if not is_demo_environment(acct):
            return False, "follower_not_demo"
    except Exception:  # noqa: BLE001 — unresolved environment is NOT demo
        return False, "environment_unresolved"
    if not getattr(acct, "is_active", False):
        return False, "account_inactive"

    ok, reason = validate_follower_binding(follower)   # cross-user/self-follow/binding, defense in depth
    if not ok:
        return False, reason

    if not replication.sltp_translation_ok:
        return False, "sltp_incomplete"
    if replication.target_volume is None:
        return False, "no_target_volume"

    event = replication.master_event
    # Staleness: an OPEN superseded by a later CLOSE, or older than the freshness window, is not produced.
    if _superseded_by_close(event):
        return False, "open_superseded_by_close"
    if event.observed_at and event.observed_at < timezone.now() - timedelta(seconds=_FRESHNESS_SECONDS):
        return False, "stale_open_outside_window"

    # Complete, directionally-valid protection from the authoritative same-poll master snapshot.
    snap = _master_position_from_event(event)
    ok, reason = _protection_ok(snap)
    if not ok:
        return False, reason

    # FRESH authoritative broker constraints — NOT the stored flag.
    constraints = _broker_constraints(acct, replication.follower_symbol)
    if not constraints.authoritative:
        return False, "constraints_not_authoritative"
    # F3: the destination instrument must be ENABLED/ready — a disabled/unready symbol fails closed.
    from execution.models import BrokerInstrument
    if not BrokerInstrument.objects.filter(
            account_id=acct.id, broker_symbol=replication.follower_symbol, enabled=True).exists():
        return False, "instrument_not_ready"
    # F2: re-verify the symbol mapping at PRODUCE time — a CopierSymbolMap revoked or changed since the
    # intent was recorded fails closed (the stored follower_symbol is not trusted on its own).
    sym = resolve_follower_symbol(follower.master.trading_account, event.symbol, acct)
    if not sym.ok or sym.follower_symbol != replication.follower_symbol:
        return False, "symbol_map_changed"
    return True, "ok"


def _fresh_open_size(replication: CopierReplication, constraints):
    """Re-size the OPEN from the ORIGINAL master volume against FRESH constraints + current config."""
    event = replication.master_event
    master_vol = float(event.volume_after) if event.event_type == diff.OPEN else float(event.volume_delta)
    return compute_follower_volume(master_vol, _cfg(replication.follower), constraints)


def _max_lot(constraints, follower) -> float | None:
    caps = [c for c in (constraints.max_volume,
                        (float(follower.max_volume) if follower.max_volume is not None else None))
            if c is not None and c > 0]
    return min(caps) if caps else None


def _payload(replication, volume, max_lot, is_demo, execution_mode) -> dict:
    event = replication.master_event
    return {
        "symbol": replication.follower_symbol,
        "provider_symbol": event.symbol,
        "side": event.side,
        "lots": str(volume),                          # a valid positive number; bridge floats it
        "sl_price": str(replication.translated_sl) if replication.translated_sl is not None else None,
        "tp_price": str(replication.translated_tp) if replication.translated_tp is not None else None,
        "entry_price": None,                          # market order
        "is_demo": is_demo,
        "execution_mode": execution_mode,             # 'SHADOW' suppresses; 'DEMO' is a real order
        "comment": f"CPY{replication.id}",            # copier provenance (cycle-prevention + reconcile)
        "signal_source": "copier",
        "max_lot": (str(max_lot) if max_lot is not None else None),   # source/follower cap for the bridge
        "copier_replication_id": replication.id,
    }


def produce_one(replication: CopierReplication) -> dict:
    """Produce (at most) one enqueue-only ExecutionJob for a single OPEN intent. Gated + idempotent.

    P0-A: the producer flag is enforced at entry AND re-checked under the lock; the mode is captured once.
    P0-B: ALL admission is re-run INSIDE the lock on the freshly re-read, locked replication."""
    if not copier_execute_enabled():
        return {"replication_id": replication.id, "produced": False, "reason": "producer_disabled"}

    ok, reason = admit_open(replication)              # cheap preflight (re-checked under the lock)
    if not ok:
        return {"replication_id": replication.id, "produced": False, "reason": reason}

    # Capture the mode ONCE; a flag flip mid-operation cannot change the produced job.
    armed = copier_execute_demo_armed()
    job_type = ExecutionJob.JobType.PLACE_ORDER if armed else ExecutionJob.JobType.PLACE_ORDER_SHADOW
    mode = "DEMO" if armed else "SHADOW"

    try:
        with transaction.atomic():
            locked = (CopierReplication.objects
                      .select_for_update()
                      .select_related("follower", "follower__follower_account", "master_event")
                      .get(pk=replication.pk))
            # P0-A re-check under the lock: the producer flag must still be on.
            if not copier_execute_enabled():
                return {"replication_id": replication.id, "produced": False, "reason": "producer_disabled"}
            # F1 (P0-B): lock AND freshly re-read the follower ROW so a disable/rebind committed during
            # promotion is seen and fails closed (the select_related relation alone would be a stale cache).
            # Lock ONLY the follower row (of="self"); the nullable broker_server join cannot be FOR UPDATE'd.
            fresh_follower = (CopierFollower.objects
                              .select_for_update(of=("self",))
                              .select_related("follower_account", "follower_account__broker_server",
                                              "master__trading_account")
                              .get(pk=locked.follower_id))
            locked.follower = fresh_follower
            # P0-B: re-validate EVERYTHING on the locked row (follower/account/owner/binding/staleness/
            # protection/fresh constraints/symbol-map) — a disable/rebind/supersede/revoke loses.
            ok, reason = admit_open(locked)
            if not ok:
                return {"replication_id": replication.id, "produced": False, "reason": reason}

            acct = locked.follower.follower_account
            constraints = _broker_constraints(acct, locked.follower_symbol)
            sr = _fresh_open_size(locked, constraints)
            if not sr.ok:
                return {"replication_id": replication.id, "produced": False, "reason": f"fresh_size:{sr.detail}"}
            # P0-C: never silently shrink the requested trade — a clamp-to-cap is a material mismatch.
            if sr.detail == "clamped_to_max":
                return {"replication_id": replication.id, "produced": False, "reason": "volume_would_be_clamped"}
            if not (sr.volume and sr.volume > 0):
                return {"replication_id": replication.id, "produced": False, "reason": "non_positive_volume"}

            max_lot = _max_lot(constraints, locked.follower)
            job = ExecutionJob.objects.create(
                account=acct, job_type=job_type,
                payload=_payload(locked, sr.volume, max_lot, is_demo=True, execution_mode=mode),
                hosted_idempotency_key=f"copier:{locked.pk}")
            # P1: a SHADOW job is NOT an execution request.
            locked.execution_job = job
            locked.status = (ReplicationStatus.EXECUTION_REQUESTED if armed
                             else ReplicationStatus.SHADOW_JOB_RECORDED)
            locked.target_volume = sr.volume          # the FRESH sized volume actually enqueued
            locked.save(update_fields=["execution_job", "status", "target_volume", "updated_at"])
    except Exception as e:  # noqa: BLE001 — an inherited ExecutionJob.save() gate (or any error) must not
        # sink the batch; the intent is left unpromoted (no partial mutation — the atomic rolled back).
        logger.warning("copier produce blocked for replication %s: %s", replication.id, type(e).__name__)
        return {"replication_id": replication.id, "produced": False, "reason": f"gate:{type(e).__name__}"}

    return {"replication_id": replication.id, "produced": True, "job_id": job.id,
            "job_type": job_type, "armed": armed, "volume": sr.volume,
            "status": ReplicationStatus.EXECUTION_REQUESTED if armed else ReplicationStatus.SHADOW_JOB_RECORDED}


def produce_pending(*, limit: int = 200) -> dict:
    """Produce jobs for pending SHADOW_RECORDED OPEN intents on enabled followers. Gated + fail-closed."""
    if not copier_execute_enabled():
        return {"enabled": False, "produced": 0, "considered": 0}
    qs = (CopierReplication.objects
          .filter(status=ReplicationStatus.SHADOW_RECORDED, execution_job__isnull=True,
                  requested_op="OPEN", follower__enabled=True)
          .select_related("follower", "follower__follower_account", "master_event")
          .order_by("id")[:limit])
    produced = 0
    considered = 0
    for rep in qs:
        considered += 1
        try:
            if produce_one(rep).get("produced"):
                produced += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("copier produce_pending error on replication %s: %s", rep.id, e)
    return {"enabled": True, "armed": copier_execute_demo_armed(), "considered": considered, "produced": produced}
