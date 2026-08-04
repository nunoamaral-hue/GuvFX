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

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from trading.models import TradingAccount

from .broker_gate import (
    _ELIGIBILITY_TO_SHARED,
    SR_ACCOUNT_TOMBSTONED,
    SR_HEALTH_DEGRADED,
    SR_HEALTH_DISCONNECTED,
    SR_HEALTH_STALE,
    SR_HEALTH_STATE_CHANGED,
    SR_RESUME_NOT_ELIGIBLE,
    SR_VALIDATION_REQUIRED,
    ExecutionGateRefused,
    evaluate_execution_gate,
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
    # Fast path: on a non-pause contract, only a currently-PAUSED record needs reconciling. A never-paused
    # (or already-resumed) account needs no durable row — this also avoids materialising a spurious
    # paused=False row for a recovered-but-never-tracked account.
    if not contract.get("pause_required"):
        if not BrokerRuntimePause.objects.filter(account=account, paused=True).exists():
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
            # Eligible again. NEVER auto-resume — only record that the paused runtime is now resumable so
            # the controlled WP2 resume service (Workstream D) can act. Keyed on the live contract's
            # ``eligible`` (HEALTHY), NOT WP3's ``resume_eligible`` EDGE: a recovery that arrives via a
            # broken edge (credential replace → re-validate → HEALTHY, which WP3 marks
            # resume_eligible=False) must still mark the durable pause resumable. The controlled resume
            # service additionally re-checks the live contract, so this flag never authorises a resume by
            # itself.
            if rec.paused and contract.get("eligible") and not rec.resume_eligible:
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


# ── WP1B/WP2 Workstream D — CONTROLLED RESUME ────────────────────────────────────────────────────────
# The SOLE authority that clears a broker-health pause. It NEVER runs automatically — no scheduler, save
# hook, signal, validation, credential replacement, provisioning, restart or periodic task calls it (a
# source-coupling test proves this). A successful resume only marks the runtime "no longer broker-paused";
# it starts no runtime, arms no strategy, creates no job/order, and accesses no credential.
@dataclass(frozen=True)
class ResumeResult:
    resumed: bool
    idempotent: bool
    refused: bool
    reason_code: str
    processed_state_version: int
    current_state_version: int
    account_id: int | None

    def as_dict(self) -> dict:
        return {
            "resumed": self.resumed, "idempotent": self.idempotent, "refused": self.refused,
            "reason_code": self.reason_code, "processed_state_version": self.processed_state_version,
            "current_state_version": self.current_state_version, "account_id": self.account_id,
        }


def _resume_audit(event, account, *, requested=None, current=None, reason="", rec=None):
    try:
        from core.audit import log_event
        meta = {"requested_state_version": requested, "current_state_version": current,
                "reason_code": reason}
        if rec is not None:
            meta["pause"] = rec.as_dict()
        log_event(None, event, severity="INFO", entity_type="TradingAccount",
                  entity_id=getattr(account, "pk", None), metadata=meta)
    except Exception:  # noqa: BLE001 — audit is fail-open; never flips the resume decision
        logger.warning("resume audit failed (event=%s)", event)


def request_broker_runtime_resume(account, *, actor="", request=None, now=None) -> ResumeResult:
    """The single, explicit-caller-only controlled resume. Immediately before clearing the pause it
    reloads and re-verifies — under a row lock, in one transaction — the account eligibility, credential,
    validation status and the LIVE WP3 health contract (authoritative; the pause row's ``resume_eligible``
    is advisory only). Idempotent + concurrency-safe on ``state_version``: at most one caller clears the
    pause; duplicates get a safe idempotent result; a newer pause always wins over an older resume; a
    current ineligible contract always refuses; nothing partial persists on a failed verification.

    Inert when either flag is OFF (no pause cleared, no write, no audit). Returns a deterministic,
    non-secret ``ResumeResult``."""
    acct_id = getattr(account, "pk", None)
    if not pause_processing_enabled():
        # DARK: fully inert — no lock, no read, no write, no audit.
        return ResumeResult(False, False, True, SR_RESUME_NOT_ELIGIBLE, 0, 0, acct_id)

    now = now or timezone.now()
    with transaction.atomic():
        # Lock the pause row and the account together for the whole verify+clear (consistent order:
        # pause row, then account) so a concurrent resume / credential replace / disconnect cannot
        # interleave between the final recheck and the commit.
        rec = BrokerRuntimePause.objects.select_for_update().filter(account=account).first()
        acct = TradingAccount.objects.select_for_update().filter(pk=acct_id).first()

        if rec is None:
            _resume_audit("BROKER_RUNTIME_RESUME_REFUSED", account, reason=SR_RESUME_NOT_ELIGIBLE)
            return ResumeResult(False, False, True, SR_RESUME_NOT_ELIGIBLE, 0, 0, acct_id)
        if not rec.paused:
            # Already cleared by an earlier/concurrent caller — idempotent success.
            _resume_audit("BROKER_RUNTIME_RESUME_IDEMPOTENT", account, current=rec.resumed_state_version,
                          rec=rec)
            return ResumeResult(False, True, False, "", rec.resumed_state_version,
                                rec.resumed_state_version, acct_id)

        # 1. Fresh account eligibility (exists / active / not-disconnected / credential / VALIDATED).
        base = evaluate_execution_gate(acct)
        if not base.allowed:
            reason = _ELIGIBILITY_TO_SHARED.get(base.reason_code, SR_VALIDATION_REQUIRED)
            _resume_audit("BROKER_RUNTIME_RESUME_REFUSED", account, reason=reason, rec=rec)
            return ResumeResult(False, False, True, reason, rec.source_state_version, 0, acct_id)

        # 2. Fresh LIVE health contract — the authoritative recovery signal.
        from reliability.broker_health import get_contract
        contract = get_contract(acct)
        if contract is None:
            _resume_audit("BROKER_RUNTIME_RESUME_REFUSED", account, reason=SR_RESUME_NOT_ELIGIBLE, rec=rec)
            return ResumeResult(False, False, True, SR_RESUME_NOT_ELIGIBLE, rec.source_state_version, 0,
                                acct_id)
        cur_version = int(contract.get("state_version") or 0)
        if contract.get("pause_required") or not contract.get("eligible"):
            reason = _HEALTH_TO_PAUSE_REASON.get(contract.get("state"), SR_HEALTH_STATE_CHANGED)
            _resume_audit("BROKER_RUNTIME_RESUME_REFUSED", account, requested=cur_version,
                          current=cur_version, reason=reason, rec=rec)
            return ResumeResult(False, False, True, reason, cur_version, cur_version, acct_id)
        if cur_version < rec.source_state_version:
            # A newer pause (source_state_version) has superseded this recovery — stale, fail closed.
            _resume_audit("BROKER_HEALTH_STALE_RESUME_VERSION_IGNORED", account, requested=cur_version,
                          current=rec.source_state_version, reason=SR_HEALTH_STATE_CHANGED, rec=rec)
            return ResumeResult(False, False, True, SR_HEALTH_STATE_CHANGED, cur_version,
                                rec.source_state_version, acct_id)

        # 3. All preconditions hold under the lock → clear ONLY the broker-health pause.
        rec.paused = False
        rec.resumed_at = now
        rec.resumed_state_version = cur_version
        rec.last_processed_version = max(rec.last_processed_version, cur_version)
        rec.resume_eligible = False
        rec.reason_code = ""
        rec.save()
        _resume_audit("BROKER_RUNTIME_RESUMED", account, requested=cur_version, current=cur_version,
                      rec=rec)
        return ResumeResult(True, False, False, "", cur_version, cur_version, acct_id)
