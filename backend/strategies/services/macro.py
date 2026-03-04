"""
Macro regime label provider (stub v1).

Returns "UNKNOWN" for all queries.  A future version will compute real
macro labels from VIX, DXY, yield-curve slope, etc.

This module intentionally avoids Django model imports so it can be
unit-tested in isolation.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

MACRO_PROVIDER_VERSION = "MACRO_PROVIDER_V1_STUB"

# Valid macro labels (for documentation / downstream consumers)
VALID_LABELS = frozenset({
    "STRONG_RISK_ON",
    "MILD_RISK_ON",
    "NEUTRAL",
    "MILD_RISK_OFF",
    "STRONG_RISK_OFF",
    "UNKNOWN",
})


def get_macro_regime_label(
    now_ts: datetime,
    account: Any = None,
    assignment: Any = None,
) -> str:
    """
    Return the current macro regime label.

    Stub implementation — always returns "UNKNOWN".

    Parameters
    ----------
    now_ts : evaluation timestamp
    account : TradingAccount (unused in stub)
    assignment : StrategyAssignment (unused in stub)

    Returns
    -------
    One of VALID_LABELS (always "UNKNOWN" in this version).
    """
    return "UNKNOWN"
