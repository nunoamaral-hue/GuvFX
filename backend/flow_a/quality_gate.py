"""
Flow A — Trade Quality Gate v0.1 (Draft / Shadow / NOT live-approved).

Core rule: ACCEPT / REJECT / ESCALATE, with **default-reject on uncertainty**.
The thresholds below are *recommended defaults*, not live-approved policy; they
exist only to exercise the shadow pipeline.

ADR-012 (Trading Availability Single Source Of Truth)
-----------------------------------------------------
``can_trade`` / trading availability may come ONLY from the authoritative SSOT
endpoint ``/api/reliability/trading-health/``. Flow A must never recreate,
derive, duplicate, or reinterpret it. In Phase 1 shadow mode execution is fully
suppressed, so availability is **not consulted** (``availability=None``). The
gate contains no alternative availability logic. If an availability check is
ever requested without an authoritative SSOT result, the gate **escalates**
(``FlowAEscalation``) rather than inventing ``can_trade``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .types import EvaluationResult, FlowAEscalation, GateDecision, GateOutcome

# --- Recommended defaults (shadow-only, NOT live-approved policy) -----------
MIN_CONFIDENCE = Decimal("60")
MIN_RISK_REWARD = Decimal("1.0")

THRESHOLDS = {
    "min_confidence": str(MIN_CONFIDENCE),
    "min_risk_reward": str(MIN_RISK_REWARD),
    "status": "DRAFT/SHADOW — not live-approved",
}


def _dec(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _availability_decision(availability):
    """ADR-012 boundary. Returns None when availability is not consulted.

    Phase 1 shadow passes ``availability=None`` (not consulted). Any non-None,
    non-SSOT value is refused — Flow A will not interpret availability from any
    source other than the SSOT endpoint, so it escalates.
    """
    if availability is None:
        return None  # not consulted in shadow mode
    raise FlowAEscalation(
        "ADR-012: trading availability may only come from the SSOT endpoint "
        "/api/reliability/trading-health/. Flow A does not derive can_trade. "
        "Escalating instead of interpreting a non-SSOT availability input."
    )


def assess(evaluation: EvaluationResult, *, availability=None) -> GateDecision:
    """Apply Trade Quality Gate v0.1. Pure; returns an immutable decision.

    Raises ``FlowAEscalation`` only for ADR-012 availability violations.
    """
    # ADR-012 availability handling first (escalation-or-not, never fabricated).
    _availability_decision(availability)

    # Evaluation must have matched, else reject.
    if not evaluation.matched or evaluation.proposed is None:
        return GateDecision(
            outcome=GateOutcome.REJECT,
            reasons=("evaluation did not match strategy",) + evaluation.reasons,
            thresholds=THRESHOLDS,
        )

    p = evaluation.proposed
    reasons: list[str] = []

    entry = _dec(p.get("entry_price"))
    sl = _dec(p.get("sl_price"))
    tp = _dec(p.get("tp_price"))
    confidence = _dec(p.get("confidence"))
    direction = str(p.get("direction", "")).upper()

    # Default-reject on uncertainty: required risk-control inputs must be present
    # and parseable.
    if entry is None:
        reasons.append("entry price missing/unparseable")
    if sl is None:
        reasons.append("stop loss missing/unparseable (required for risk control)")
    if tp is None:
        reasons.append("take profit missing — risk:reward cannot be verified")
    if confidence is None:
        reasons.append("confidence missing/unparseable")

    if reasons:
        return GateDecision(
            outcome=GateOutcome.REJECT, reasons=tuple(reasons), thresholds=THRESHOLDS
        )

    # Confidence floor.
    if confidence < MIN_CONFIDENCE:
        reasons.append(
            f"confidence {confidence} below minimum {MIN_CONFIDENCE}"
        )

    # SL/TP must sit on the correct side of entry for the direction.
    if direction == "BUY":
        if not (sl < entry):
            reasons.append("BUY stop loss must be below entry")
        if not (tp > entry):
            reasons.append("BUY take profit must be above entry")
        risk = entry - sl
        reward = tp - entry
    elif direction == "SELL":
        if not (sl > entry):
            reasons.append("SELL stop loss must be above entry")
        if not (tp < entry):
            reasons.append("SELL take profit must be below entry")
        risk = sl - entry
        reward = entry - tp
    else:
        return GateDecision(
            outcome=GateOutcome.REJECT,
            reasons=(f"unsupported direction {direction!r}",),
            thresholds=THRESHOLDS,
        )

    # Risk:reward floor (only meaningful when risk is positive).
    if risk <= 0:
        reasons.append("non-positive risk distance (entry/SL inconsistent)")
    elif (reward / risk) < MIN_RISK_REWARD:
        reasons.append(
            f"risk:reward {(reward / risk):.2f} below minimum {MIN_RISK_REWARD}"
        )

    if reasons:
        return GateDecision(
            outcome=GateOutcome.REJECT, reasons=tuple(reasons), thresholds=THRESHOLDS
        )

    return GateDecision(
        outcome=GateOutcome.ACCEPT,
        reasons=("passed confidence, SL/TP placement, and risk:reward checks",),
        thresholds=THRESHOLDS,
    )
