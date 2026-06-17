"""
Flow A — OPEN_TRADE candidate construction.

Builds an immutable :class:`OpenTradeCandidate` from an accepted evaluation. The
candidate is a *description of a trade that was not placed*. It is intentionally
NOT created via ``execution.services.create_open_trade_job`` and is NOT an
``execution.ExecutionJob`` — see :mod:`flow_a.suppression`.
"""

from __future__ import annotations

from .types import EvaluationResult, OpenTradeCandidate


def build_candidate(evaluation: EvaluationResult) -> OpenTradeCandidate:
    """Construct the OPEN_TRADE candidate artifact from a matched evaluation."""
    if not evaluation.matched or evaluation.proposed is None:
        raise ValueError("cannot build a candidate from an unmatched evaluation")

    p = evaluation.proposed
    return OpenTradeCandidate(
        symbol=p["symbol"],
        direction=p["direction"],
        timeframe=p.get("timeframe", ""),
        entry_type=p.get("entry_type", "MARKET"),
        entry_price=p.get("entry_price"),
        sl_price=p["sl_price"],
        tp_price=p.get("tp_price"),
        risk_per_trade_pct=p["risk_per_trade_pct"],
        comment=p.get("comment", ""),
    )
