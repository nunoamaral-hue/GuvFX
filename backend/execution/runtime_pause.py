"""WP1B/WP2 (ADR-0029) — broker-health runtime PAUSE (degradation processing).

The execution layer consumes the authoritative WP3 convergence contract
(``reliability.broker_health.get_contract``) and persists a durable, idempotent broker-health pause
(``execution.models.BrokerRuntimePause``). It NEVER reproduces WP3 transition logic, NEVER auto-resumes,
NEVER deletes/tombstones a runtime, and NEVER touches credentials or runtime/strategy config — a pause
only gates execution.

Idempotency + races are keyed on the health ``state_version``: a version is processed at most once, a
smaller version is ignored, and a larger version may supersede — an older decision can never reverse a
newer one. All processing is inert unless BOTH ``BROKER_CONNECTIVITY_EXECUTION_GATE`` and
``BROKER_CONNECTIVITY_HEALTH_ENABLED`` are on.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from .broker_gate import (
    SR_ACCOUNT_TOMBSTONED,
    SR_HEALTH_DEGRADED,
    SR_HEALTH_DISCONNECTED,
    SR_HEALTH_STALE,
    SR_HEALTH_STATE_CHANGED,
    ExecutionGateRefused,
    execution_gate_enabled,
)
from .models import BrokerRuntimePause

logger = logging.getLogger(__name__)

# WP3 health state → shared pause reason (pause_required is only ever true for these four states).
_HEALTH_TO_PAUSE_REASON = {
    "DEGRADED": SR_HEALTH_DEGRADED,
    "STALE": SR_HEALTH_STALE,
    "DISCONNECTED": SR_HEALTH_DISCONNECTED,
    "TOMBSTONED": SR_ACCOUNT_TOMBSTONED,
}


def pause_processing_enabled() -> bool:
    """Health-driven pause/resume requires BOTH broker-connectivity flags."""
    if not execution_gate_enabled():
        return False
    try:
        from reliability.constants import broker_health_enabled
        return broker_health_enabled()
    except Exception:  # noqa: BLE001 — no health engine ⇒ no pause processing
        return False


def _get_contract(account):
    from reliability.broker_health import get_contract
    return get_contract(account)


def _audit(event, account, *, version=None, rec=None, extra=None):
    try:
        from core.audit import log_event
        meta = {"state_version": version}
        if rec is not None:
            meta.update(rec.as_dict())
        if extra:
            meta.update(extra)
        log_event(None, event, severity="WARN", entity_type="TradingAccount",
                  entity_id=getattr(account, "pk", None), metadata=meta)
    except Exception:  # noqa: BLE001 — audit is fail-open; it must never change pause control flow
        logger.warning("broker pause audit failed (event=%s)", event)


def process_broker_health_pause(account, *, now=None) -> dict | None:
    """Reconcile the durable pause record with the latest WP3 contract. Persists a pause when the current
    contract is pause_required; records a recovery signal (``resume_eligible``) WITHOUT resuming. Returns
    the pause snapshot, or None when inert / no health row. Serialised per-account with
    ``select_for_update`` so concurrent callers cannot double-apply or clobber the version."""
    if not pause_processing_enabled():
        return None
    contract = _get_contract(account)
    if contract is None:
        return None
    now = now or timezone.now()
    version = int(contract.get("state_version") or 0)
    # Fast path: a healthy/eligible account that has never been paused needs no durable row.
    if not contract.get("pause_required") and not contract.get("resume_eligible"):
        if not BrokerRuntimePause.objects.filter(account=account).exists():
            return None
    with transaction.atomic():
        rec, created = BrokerRuntimePause.objects.select_for_update().get_or_create(account=account)
        if not created and version < rec.last_processed_version:
            _audit("BROKER_HEALTH_STALE_PAUSE_VERSION_IGNORED", account, version=version, rec=rec)
            return rec.as_dict()
        if not created and version == rec.last_processed_version:
            return rec.as_dict()  # already processed this exact version — idempotent no-op

        rec.last_processed_version = version
        if contract.get("pause_required"):
            newly = not rec.paused
            rec.paused = True
            rec.reason_code = _HEALTH_TO_PAUSE_REASON.get(contract.get("state"), SR_HEALTH_STATE_CHANGED)
            rec.source_state_version = version
            rec.resume_eligible = False
            rec.resumed_at = None
            if newly:
                rec.paused_at = now
            rec.save()
            _audit("BROKER_RUNTIME_PAUSED" if newly else "BROKER_HEALTH_PAUSE_REQUESTED",
                   account, version=version, rec=rec)
        else:
            # Eligible or recovering. NEVER auto-resume — only record the recovery signal; the controlled
            # WP2 resume service (Workstream D) is the sole path that clears ``paused``.
            if rec.paused and contract.get("resume_eligible") and not rec.resume_eligible:
                rec.resume_eligible = True
                rec.save()
                _audit("BROKER_RECOVERY_DETECTED", account, version=version, rec=rec)
            else:
                rec.save()  # persist the advanced watermark even when nothing else changed
        return rec.as_dict()


def is_broker_paused(account) -> bool:
    """True iff the account has a live health contract that is currently pause_required (both flags on).
    Drives the CREATION-time block immediately, independent of whether the durable record was reconciled
    yet. Inert (False) when either flag is OFF."""
    if not pause_processing_enabled():
        return False
    contract = _get_contract(account)
    return bool(contract is not None and contract.get("pause_required"))


def require_not_broker_paused(account, *, request=None, trigger="") -> None:
    """Creation-time guard: refuse creating a new exposure-opening job when the account's CURRENT broker
    health is pause_required. No-op unless both flags are on. Raises ``ExecutionGateRefused`` (audited)."""
    if not pause_processing_enabled():
        return
    contract = _get_contract(account)
    if contract is not None and contract.get("pause_required"):
        reason = _HEALTH_TO_PAUSE_REASON.get(contract.get("state"), SR_HEALTH_STATE_CHANGED)
        _audit("EXECUTION_GATE_REFUSED", account, version=contract.get("state_version"),
               extra={"reason_code": reason, "trigger": str(trigger or ""), "stage": "creation_paused"})
        raise ExecutionGateRefused(reason)


def pause_state(account) -> dict | None:
    """Read-only durable pause snapshot for reporting/UI (WP4/WP5). None when no record exists."""
    rec = BrokerRuntimePause.objects.filter(account=account).first()
    return rec.as_dict() if rec else None
