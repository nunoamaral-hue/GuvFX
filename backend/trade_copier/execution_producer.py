"""trade_copier.execution_producer — gated DEMO-only ExecutionJob producer (B-exec, DARK).

Turns recorded ``CopierReplication`` OPEN intents into enqueue-only ``ExecutionJob`` rows, under a
FRESH admission re-check (never trusting the stored provenance flags alone). Hard rails (ADR-0051 R2):

* DEMO followers ONLY — a LIVE (or ambiguous) follower is rejected fail-closed; a LIVE destination is
  never produced even if an intent/payload asked for one.
* BUILD != ARM — default-OFF flags. When enabled but not armed, jobs are ``PLACE_ORDER_SHADOW``
  (suppressed, no order). Only ``copier_execute_demo_armed()`` promotes to a real DEMO ``PLACE_ORDER``.
* Fresh authoritative broker constraints re-read at promotion; reject non-authoritative, incomplete
  SL/TP, or an unproducible op. Only OPEN is produced here; CLOSE/MODIFY need a verified CURRENT
  copier-owned follower ticket (none exists until an open executes + reconciles) → fail-closed.
* Idempotent — one ExecutionJob per replication (row-locked check on ``execution_job``).
* Inherits every ``ExecutionJob.save()`` gate (kill switch, stop-trading, broker gate, identity pin);
  a blocked creation is caught per-replication and the intent is left unpromoted.

It calls no ``order_send`` and changes no bridge wall. Arming + any real DEMO run is operator-gated.
"""
from __future__ import annotations

import logging

from django.db import transaction

from execution.models import ExecutionJob
from trade_copier import diff
from trade_copier.flags import copier_execute_demo_armed, copier_execute_enabled
from trade_copier.models import CopierReplication, ReplicationStatus
from trade_copier.replication import _broker_constraints, _cfg, validate_follower_binding
from trade_copier.sizing import compute_follower_volume

logger = logging.getLogger(__name__)

_PRODUCIBLE_OPS = {"OPEN"}   # SCALE_IN later; CLOSE/MODIFY require a verified follower ticket.


def admit_open(replication: CopierReplication) -> tuple[bool, str]:
    """Fresh, fail-closed admission for producing a DEMO OPEN job from a recorded intent."""
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

    ok, reason = validate_follower_binding(follower)   # cross-user/self-follow/binding, defense in depth
    if not ok:
        return False, reason

    if not replication.sltp_translation_ok:
        return False, "sltp_incomplete"
    if replication.target_volume is None:
        return False, "no_target_volume"

    # FRESH authoritative broker constraints — NOT the stored flag.
    constraints = _broker_constraints(acct, replication.follower_symbol)
    if not constraints.authoritative:
        return False, "constraints_not_authoritative"
    return True, "ok"


def _fresh_open_volume(replication: CopierReplication, constraints):
    """Re-size the OPEN from the ORIGINAL master volume against FRESH constraints + current config."""
    event = replication.master_event
    master_vol = float(event.volume_after) if event.event_type == diff.OPEN else float(event.volume_delta)
    return compute_follower_volume(master_vol, _cfg(replication.follower), constraints)


def _payload(replication: CopierReplication, volume: float, is_demo: bool, execution_mode: str) -> dict:
    event = replication.master_event
    return {
        "symbol": replication.follower_symbol,
        "provider_symbol": event.symbol,
        "side": event.side,
        "lots": str(volume),
        "sl_price": (str(replication.translated_sl) if replication.translated_sl is not None else None),
        "tp_price": (str(replication.translated_tp) if replication.translated_tp is not None else None),
        "entry_price": None,                 # market order
        "is_demo": is_demo,
        "execution_mode": execution_mode,    # 'SHADOW' suppresses; 'DEMO' is a real order
        "comment": f"CPY{replication.id}",   # copier correlation tag (cycle-prevention + reconciliation)
        "signal_source": "copier",
        "copier_replication_id": replication.id,
    }


def produce_one(replication: CopierReplication) -> dict:
    """Produce (at most) one enqueue-only ExecutionJob for a single OPEN intent. Idempotent."""
    ok, reason = admit_open(replication)
    if not ok:
        return {"replication_id": replication.id, "produced": False, "reason": reason}

    acct = replication.follower.follower_account
    constraints = _broker_constraints(acct, replication.follower_symbol)
    sr = _fresh_open_volume(replication, constraints)
    if not sr.ok:
        return {"replication_id": replication.id, "produced": False, "reason": f"fresh_size:{sr.detail}"}

    armed = copier_execute_demo_armed()
    job_type = ExecutionJob.JobType.PLACE_ORDER if armed else ExecutionJob.JobType.PLACE_ORDER_SHADOW
    mode = "DEMO" if armed else "SHADOW"

    try:
        with transaction.atomic():
            # Idempotency: lock the intent, re-check it has no job (a concurrent producer may have won).
            locked = CopierReplication.objects.select_for_update().get(pk=replication.pk)
            if locked.execution_job_id is not None:
                return {"replication_id": replication.id, "produced": False, "reason": "already_produced"}
            job = ExecutionJob.objects.create(
                account=acct, job_type=job_type,
                payload=_payload(locked, sr.volume, is_demo=True, execution_mode=mode),
                hosted_idempotency_key=f"copier:{locked.pk}")
            locked.execution_job = job
            locked.status = ReplicationStatus.EXECUTION_REQUESTED
            locked.target_volume = sr.volume     # record the FRESH sized volume actually enqueued
            locked.save(update_fields=["execution_job", "status", "target_volume", "updated_at"])
    except Exception as e:  # noqa: BLE001 — an inherited ExecutionJob.save() gate (or any error) must
        # not sink the batch; the intent is left unpromoted for a later pass.
        logger.warning("copier produce blocked for replication %s: %s", replication.id, type(e).__name__)
        return {"replication_id": replication.id, "produced": False, "reason": f"gate:{type(e).__name__}"}

    return {"replication_id": replication.id, "produced": True, "job_id": job.id,
            "job_type": job_type, "armed": armed, "volume": sr.volume}


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
