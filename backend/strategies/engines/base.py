"""
Engine interface module for GuvFX strategy engines.

Defines lightweight dataclasses for standardised engine I/O.
Existing engines (TBP, ALTS, SCE) are NOT required to adopt these types;
new engines (TC1, hybrid wrappers) use them to keep a consistent contract.

No Django model imports — keep this module side-effect-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class EngineContext:
    """
    Immutable context passed into every engine evaluation call.

    Engines should treat this as read-only input.
    """

    # Django model instances (typed as Any to avoid importing Django here)
    account: Any  # TradingAccount
    assignment: Any  # StrategyAssignment
    symbol: str
    now_ts: datetime
    bar_close_time: str = ""
    dry_run: bool = False
    timeframe: str = "H4"


@dataclass
class EngineDecision:
    """
    Standardised output from an engine evaluation.

    action values:
        "NO_ACTION"    — no trade signal
        "PLACE_ORDER"  — a valid trade signal was generated
    """

    action: str  # "NO_ACTION" | "PLACE_ORDER"
    side: Optional[str] = None  # "BUY" | "SELL" | None
    entry_price: Optional[float] = None
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    risk_pct: float = 0.0
    lots: Optional[float] = None
    reason_code: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
