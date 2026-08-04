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


def execution_gate_enabled() -> bool:
    """The WP1B/WP2 execution gate flag. Default OFF ⇒ the gate is transparent (no execution-path change)."""
    return os.getenv("BROKER_CONNECTIVITY_EXECUTION_GATE", "0").strip().lower() in ("1", "true", "yes", "on")


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
