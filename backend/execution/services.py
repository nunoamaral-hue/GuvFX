from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.contrib.auth import get_user_model

from billing.enforcement import require_entitlement
from execution.models import ExecutionJob
from strategies.models import Strategy, StrategyAssignment
from trading.models import TradingAccount

User = get_user_model()


@dataclass
class OpenTradeParams:
    """
    Parameters required to create an OPEN_TRADE execution job.

    This function intentionally keeps risk calculation simple for now.
    Later we can extend it to compute volume/lot size based on pip value, etc.
    """

    account: TradingAccount
    strategy: Strategy
    assignment: Optional[StrategyAssignment]
    created_by: User

    symbol: str
    direction: str  # "BUY" or "SELL"
    timeframe: str

    entry_type: str  # "MARKET", "LIMIT", or "STOP"
    entry_price: Optional[Decimal]

    sl_price: Decimal
    tp_price: Optional[Decimal]

    risk_per_trade_pct: Optional[Decimal] = None
    comment: str = ""


def resolve_risk_pct(*, params: OpenTradeParams) -> Decimal:
    """
    Decide the effective risk_per_trade_pct to use, in this order:
    1. Caller-provided override (params.risk_per_trade_pct)
    2. Assignment override (assignment.risk_per_trade_override_pct)
    3. Strategy default (strategy.risk_per_trade_pct)
    4. Fallback to 1.0%
    """
    if params.risk_per_trade_pct is not None:
        return params.risk_per_trade_pct

    if params.assignment and params.assignment.risk_per_trade_override_pct is not None:
        return Decimal(params.assignment.risk_per_trade_override_pct)

    if params.strategy.risk_per_trade_pct is not None:
        return Decimal(params.strategy.risk_per_trade_pct)

    return Decimal("1.0")


def create_open_trade_job(params: OpenTradeParams) -> ExecutionJob:
    """
    Create an ExecutionJob with job_type='OPEN_TRADE' and a payload that
    contains all information the MT5 worker needs to open a trade.
    """
    # Defense-in-depth: enforce entitlement at service level so direct
    # callers (e.g. signal_engine) cannot bypass view-level checks.
    owner = params.account.user if params.account else params.created_by
    require_entitlement(owner, "can_deploy_automation")

    effective_risk_pct = resolve_risk_pct(params=params)

    payload = {
        "symbol": params.symbol,
        "direction": params.direction,
        "timeframe": params.timeframe,
        "entry_type": params.entry_type,
        "entry_price": float(params.entry_price) if params.entry_price is not None else None,
        "sl_price": float(params.sl_price),
        "tp_price": float(params.tp_price) if params.tp_price is not None else None,
        "risk_per_trade_pct": float(effective_risk_pct),
        "comment": params.comment,
    }

    job = ExecutionJob.objects.create(
        job_type="OPEN_TRADE",
        account=params.account,
        strategy=params.strategy,
        assignment=params.assignment,
        payload=payload,
        status=ExecutionJob.Status.PENDING,
        created_by=params.created_by,
    )

    return job
