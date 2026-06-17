"""
Flow A — Strategy Evaluation layer.

Deterministically evaluates a single Wayond signal against a *given* strategy
configuration. This is NOT a Strategy Selection engine and it ingests NO market
data — both are explicitly out of scope. The strategy is supplied to Flow A as a
mapping mirroring the relevant ``strategies.models.Strategy`` fields, so no ORM
coupling (and no Postgres-only migrations) are required for the shadow path.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from intelligence.envelope import SignalIntelligenceEnvelope

from .types import EvaluationResult

DEFAULT_RISK_PCT = "1.0"


def _symbol_universe(strategy: Mapping) -> list[str]:
    raw = str(strategy.get("symbol_universe", "") or "")
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _resolve_risk_pct(strategy: Mapping) -> str:
    """Mirror of ``execution.services.resolve_risk_pct`` ordering (strategy
    default -> 1.0). Risk percent only — not trading availability (ADR-012)."""
    val = strategy.get("risk_per_trade_pct")
    if val in (None, ""):
        return DEFAULT_RISK_PCT
    try:
        return str(Decimal(str(val)))
    except (InvalidOperation, ValueError):
        return DEFAULT_RISK_PCT


def evaluate(
    envelope: SignalIntelligenceEnvelope, strategy: Mapping
) -> EvaluationResult:
    """Evaluate the signal against the strategy. Pure; returns an immutable result.

    Match criteria (deterministic, no market data):
      * strategy is active
      * the signal's market is in the strategy's symbol universe
      * the signal direction is BUY or SELL
    On match, ``proposed`` carries the trade parameters distilled from the
    signal + the strategy's resolved risk percent.
    """
    p = envelope.structured_payload
    reasons: list[str] = []

    if not bool(strategy.get("is_active", False)):
        reasons.append("strategy is not active")

    universe = _symbol_universe(strategy)
    if universe and p.market.upper() not in universe:
        reasons.append(
            f"signal market {p.market!r} not in strategy universe {universe}"
        )

    direction = p.direction.upper()
    if direction not in ("BUY", "SELL"):
        reasons.append(f"unsupported signal direction {p.direction!r}")

    if reasons:
        return EvaluationResult(matched=False, reasons=tuple(reasons), proposed=None)

    proposed = {
        "symbol": p.market,
        "direction": direction,
        "timeframe": str(strategy.get("timeframe", "") or ""),
        "entry_type": "MARKET",
        "entry_price": p.entry,
        "sl_price": p.stop_loss,
        "tp_price": p.take_profit,
        "risk_per_trade_pct": _resolve_risk_pct(strategy),
        "confidence": p.confidence,
        "comment": f"flow-a-shadow:{p.signal_id}",
    }
    return EvaluationResult(
        matched=True,
        reasons=("strategy matched signal (market, direction, active)",),
        proposed=proposed,
    )
