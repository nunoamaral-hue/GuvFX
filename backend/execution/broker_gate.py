"""WP1B/WP2 (ADR-0029) — broker-validation EXECUTION GATE.

ONE central, fail-closed decision service. When ``BROKER_CONNECTIVITY_EXECUTION_GATE`` is enabled,
execution is refused unless the selected broker account is VALIDATED and eligible. The decision is
deterministic, non-secret, auditable, and returns a stable reason code — suitable for backend, worker
and (via API) frontend consumption. Condition logic lives HERE only; authoritative execution funnels
call ``require_execution_gate`` (or ``evaluate_execution_gate``) rather than re-implementing the checks.

While the flag is OFF the gate is TRANSPARENT (``allowed=True``, reason ``gate_disabled``) so existing
production execution behaviour is unchanged. Fail-closed: every ambiguity refuses when the flag is ON.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from trading.models import TradingAccount

logger = logging.getLogger(__name__)

# ── stable, non-secret reason codes ──────────────────────────────────────────────────────────────────
GATE_OK = "ok"
GATE_DISABLED = "gate_disabled"
R_ACCOUNT_MISSING = "account_missing"
R_ACCOUNT_INACTIVE = "account_inactive"
R_ACCOUNT_DISCONNECTED = "account_disconnected"
R_CREDENTIAL_MISSING = "credential_missing"
R_NOT_VALIDATED_NEVER = "not_validated_never"
R_NOT_VALIDATED_CONNECTION_FAILED = "not_validated_connection_failed"
R_NOT_VALIDATED_TECHNICAL_ERROR = "not_validated_technical_error"
R_VALIDATION_STATE_UNKNOWN = "validation_state_unknown"

_VS = TradingAccount.ValidationStatus
_NOT_VALIDATED = {
    _VS.NEVER: R_NOT_VALIDATED_NEVER,
    _VS.CONNECTION_FAILED: R_NOT_VALIDATED_CONNECTION_FAILED,
    _VS.TECHNICAL_ERROR: R_NOT_VALIDATED_TECHNICAL_ERROR,
}

# ── shared cross-WP reason vocabulary (WP1B/WP2/WP3/WP4/WP5) ──────────────────────────────────────────
# Stable, non-secret, customer-safe. Documented in ADR-0029. The final-dispatch gate, credential
# invalidation, pause and resume all speak these; the older creation-gate codes above map onto them.
SR_ACCOUNT_MISSING = "broker_account_missing"
SR_ACCOUNT_AMBIGUOUS = "broker_account_ambiguous"
SR_ACCOUNT_INACTIVE = "broker_account_inactive"
SR_ACCOUNT_DISCONNECTED = "broker_account_disconnected"
SR_ACCOUNT_TOMBSTONED = "broker_account_tombstoned"
SR_CREDENTIAL_MISSING = "broker_credential_missing"
SR_VALIDATION_REQUIRED = "broker_validation_required"
SR_VALIDATION_FAILED = "broker_validation_failed"
SR_VALIDATION_UNAVAILABLE = "broker_validation_unavailable"
SR_HEALTH_DEGRADED = "broker_health_degraded"
SR_HEALTH_STALE = "broker_health_stale"
SR_HEALTH_DISCONNECTED = "broker_health_disconnected"
SR_RESUME_NOT_ELIGIBLE = "broker_resume_not_eligible"
SR_HEALTH_STATE_CHANGED = "broker_health_state_changed"
DISPATCH_OK = "dispatch_ok"

# Creation-gate eligibility code → shared vocabulary.
_ELIGIBILITY_TO_SHARED = {
    GATE_OK: DISPATCH_OK,
    R_ACCOUNT_MISSING: SR_ACCOUNT_MISSING,
    R_ACCOUNT_INACTIVE: SR_ACCOUNT_INACTIVE,
    R_ACCOUNT_DISCONNECTED: SR_ACCOUNT_DISCONNECTED,
    R_CREDENTIAL_MISSING: SR_CREDENTIAL_MISSING,
    R_NOT_VALIDATED_NEVER: SR_VALIDATION_REQUIRED,
    R_NOT_VALIDATED_CONNECTION_FAILED: SR_VALIDATION_FAILED,
    R_NOT_VALIDATED_TECHNICAL_ERROR: SR_VALIDATION_UNAVAILABLE,
    R_VALIDATION_STATE_UNKNOWN: SR_VALIDATION_REQUIRED,
}

# WP3 health state → shared vocabulary (for an adverse/non-eligible contract at dispatch).
_HEALTH_STATE_TO_SHARED = {
    "DEGRADED": SR_HEALTH_DEGRADED,
    "STALE": SR_HEALTH_STALE,
    "DISCONNECTED": SR_HEALTH_DISCONNECTED,
    "TOMBSTONED": SR_ACCOUNT_TOMBSTONED,
    "UNKNOWN": SR_VALIDATION_REQUIRED,  # a row exists but health is not proven → re-validate needed
}


def execution_gate_enabled() -> bool:
    """The WP1B/WP2 execution gate flag. Default OFF ⇒ the gate is transparent (no execution-path change)."""
    return os.getenv("BROKER_CONNECTIVITY_EXECUTION_GATE", "0").strip().lower() in ("1", "true", "yes", "on")


def _health_engine_enabled() -> bool:
    """Health-driven dispatch refusal requires BOTH the execution-gate flag (checked by the caller) and
    the WP3 health flag. Read live; import-local so execution has no hard dependency on reliability."""
    try:
        from reliability.constants import broker_health_enabled
        return broker_health_enabled()
    except Exception:  # noqa: BLE001 — absence of the health engine simply means no health constraint
        return False


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason_code: str

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "reason_code": self.reason_code}


class ExecutionGateRefused(Exception):
    """Raised at an authoritative execution funnel when the gate refuses. ``reason_code`` is non-secret."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        self.reason = reason_code  # alias: interoperable with ExecutionKillSwitchEngaged.reason callers
        super().__init__(f"execution refused by broker validation gate: {reason_code}")


def evaluate_execution_gate(account) -> GateDecision:
    """Pure, side-effect-free gate decision. Fail-closed when the flag is ON; transparent when OFF.

    Order is chosen so the reason code reports the FIRST disqualifying condition. Refuses (ON) for a
    missing/inactive/disconnected/tombstoned account, a missing/destroyed credential, and any
    validation_status other than VALIDATED (NEVER / CONNECTION_FAILED / TECHNICAL_ERROR / unknown)."""
    if not execution_gate_enabled():
        return GateDecision(True, GATE_DISABLED)
    if account is None or getattr(account, "pk", None) is None:
        return GateDecision(False, R_ACCOUNT_MISSING)
    if not getattr(account, "is_active", False):
        return GateDecision(False, R_ACCOUNT_INACTIVE)
    if getattr(account, "disconnected_at", None) is not None:
        return GateDecision(False, R_ACCOUNT_DISCONNECTED)
    if not (getattr(account, "password_enc", "") or ""):
        return GateDecision(False, R_CREDENTIAL_MISSING)
    status = getattr(account, "validation_status", None)
    if status == _VS.VALIDATED:
        return GateDecision(True, GATE_OK)
    return GateDecision(False, _NOT_VALIDATED.get(status, R_VALIDATION_STATE_UNKNOWN))


def require_execution_gate(account, *, request=None, actor="", trigger="") -> GateDecision:
    """Enforce the gate at an authoritative funnel: raise ``ExecutionGateRefused`` (audited) when refused,
    else return the allowed decision. Use this where there is no other refusal-audit trail; funnels that
    already audit their own rejections (e.g. promotion) may call ``evaluate_execution_gate`` instead."""
    decision = evaluate_execution_gate(account)
    if not decision.allowed:
        _audit_refusal(account, decision, request=request, actor=actor, trigger=trigger)
        raise ExecutionGateRefused(decision.reason_code)
    return decision


def _audit_refusal(account, decision, *, request=None, actor="", trigger="") -> None:
    """Best-effort, non-secret audit of a gate refusal (fail-open — never blocks execution control flow)."""
    try:
        from core.audit import log_event
        log_event(
            request, "EXECUTION_GATE_REFUSED", severity="WARN",
            entity_type="TradingAccount", entity_id=getattr(account, "pk", None),
            metadata={"reason_code": decision.reason_code, "actor": str(actor or ""), "trigger": str(trigger or "")},
        )
    except Exception:  # noqa: BLE001 — audit must never break the gate
        logger.warning("execution gate refusal audit failed (reason=%s)", decision.reason_code)


# ── WP1B/WP2 FINAL-DISPATCH GATE ─────────────────────────────────────────────────────────────────────
# The creation-time gate (evaluate_execution_gate, enforced in ExecutionJob.save) proves eligibility when
# a job is *created*. Between enqueue and the live order_send, an account can be disconnected, have its
# credential replaced, or have its broker health degrade. The final-dispatch gate re-evaluates FRESH,
# immediately before any real exposure-opening dispatch, and never trusts eligibility captured at enqueue.
def _get_health_contract(account):
    """Fetch the latest WP3 convergence contract for the account (or None). Raises on an internal error
    so the dispatch gate can fail closed — see evaluate_dispatch_gate."""
    from reliability.broker_health import get_contract
    return get_contract(account)


def evaluate_dispatch_gate(account) -> GateDecision:
    """FINAL-DISPATCH decision (shared reason vocabulary). Transparent when the execution-gate flag is OFF.
    When ON it re-evaluates eligibility FRESH; when the WP3 health flag is ALSO on it then consumes the
    latest health contract and refuses an ineligible (adverse or not-yet-healthy) account. Fail-closed:
    an eligibility failure, or an error reading health, refuses."""
    if not execution_gate_enabled():
        return GateDecision(True, GATE_DISABLED)
    base = evaluate_execution_gate(account)  # fresh eligibility — never the enqueue-time snapshot
    if not base.allowed:
        return GateDecision(False, _ELIGIBILITY_TO_SHARED.get(base.reason_code, SR_VALIDATION_REQUIRED))
    if _health_engine_enabled():
        try:
            contract = _get_health_contract(account)
        except Exception:  # noqa: BLE001 — health indeterminate ⇒ fail closed (do not open exposure)
            logger.warning("dispatch gate: health contract read failed for account=%s; failing closed",
                           getattr(account, "pk", None))
            return GateDecision(False, SR_HEALTH_STATE_CHANGED)
        # A contract that exists and is not eligible blocks dispatch. No contract (health has no row yet)
        # adds no constraint — eligibility already vouched for the account.
        if contract is not None and not contract.get("eligible", False):
            state = contract.get("state", "")
            return GateDecision(False, _HEALTH_STATE_TO_SHARED.get(state, SR_HEALTH_STATE_CHANGED))
    return GateDecision(True, DISPATCH_OK)


def evaluate_job_dispatch(job_id) -> GateDecision:
    """Resolve a job's account FRESH from the DB and evaluate the final-dispatch gate. Used by the worker
    immediately before the live order_send. Transparent (no DB read) when the gate flag is OFF. Audits a
    refusal durably (fail-open). ``job_id`` may be an int or an ExecutionJob-like id."""
    if not execution_gate_enabled():
        return GateDecision(True, GATE_DISABLED)
    from execution.models import ExecutionJob
    job = ExecutionJob.objects.select_related("account").filter(pk=job_id).first()
    if job is None:
        return GateDecision(False, SR_ACCOUNT_MISSING)
    decision = evaluate_dispatch_gate(job.account)
    if not decision.allowed:
        _audit_dispatch_refusal(job.account, decision, job_id=job_id)
    return decision


def require_dispatch_gate(account, *, request=None, actor="", trigger="", job_id=None) -> GateDecision:
    """Raising variant for a synchronous dispatch caller: raise ``ExecutionGateRefused`` (audited) when
    the final-dispatch gate refuses, else return the allowed decision."""
    decision = evaluate_dispatch_gate(account)
    if not decision.allowed:
        _audit_dispatch_refusal(account, decision, job_id=job_id, actor=actor, trigger=trigger, request=request)
        raise ExecutionGateRefused(decision.reason_code)
    return decision


def _audit_dispatch_refusal(account, decision, *, job_id=None, actor="", trigger="", request=None) -> None:
    """Durable, non-secret audit of a final-dispatch refusal (fail-open — audit failure must not let the
    dispatch proceed; the caller has already decided to refuse before this runs)."""
    try:
        from core.audit import log_event
        log_event(
            request, "EXECUTION_DISPATCH_REFUSED", severity="WARN",
            entity_type="TradingAccount", entity_id=getattr(account, "pk", None),
            metadata={"reason_code": decision.reason_code, "job_id": job_id,
                      "actor": str(actor or ""), "trigger": str(trigger or "")},
        )
    except Exception:  # noqa: BLE001 — audit must never change the gate outcome
        logger.warning("dispatch gate refusal audit failed (reason=%s)", decision.reason_code)
