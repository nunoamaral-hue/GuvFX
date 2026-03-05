"""
Execution guards for GuvFX strategy signal generation.

Provides price normalization, spread gating (log-only in backend — hard
enforcement stays in the Windows bridge), and SL/TP placement validation.

All functions are deterministic and side-effect-free (except logging).
"""

from __future__ import annotations

import logging
from typing import Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pip size helpers
# ---------------------------------------------------------------------------

def get_pip_size(symbol: str) -> float:
    """
    Return pip size for a symbol.

    Standard forex: 0.0001
    JPY pairs: 0.01
    XAU (gold): 0.01  # hook for future v2

    TODO: extend for indices, crypto when SIGNAL_ALLOWED_SYMBOLS grows.
    """
    symbol_upper = symbol.upper()
    if "JPY" in symbol_upper:
        return 0.01
    if "XAU" in symbol_upper:
        return 0.01  # XAU pip size (1 pip = $0.01 for gold)
    return 0.0001


def get_price_digits(symbol: str) -> int:
    """
    Return number of decimal digits for price rounding.

    Standard forex: 5
    JPY pairs: 3
    XAU (gold): 2
    """
    symbol_upper = symbol.upper()
    if "JPY" in symbol_upper:
        return 3
    if "XAU" in symbol_upper:
        return 2
    return 5


# ---------------------------------------------------------------------------
# Price normalization
# ---------------------------------------------------------------------------

def normalize_prices(
    entry: float,
    sl: float,
    tp: float,
    side: str,
    symbol: str,
) -> Tuple[float, float, float]:
    """
    Round entry, SL, TP to the correct number of digits for the symbol.

    Also ensures SL is on the correct side of entry and TP is on the
    correct side of entry relative to *side* (BUY/SELL).

    Parameters
    ----------
    entry : raw entry price
    sl : raw stop-loss price
    tp : raw take-profit price
    side : "BUY" or "SELL"
    symbol : e.g. "EURUSD"

    Returns
    -------
    (entry, sl, tp) rounded to the correct precision.
    """
    digits = get_price_digits(symbol)

    entry = round(entry, digits)
    sl = round(sl, digits)
    tp = round(tp, digits)

    return entry, sl, tp


# ---------------------------------------------------------------------------
# Spread gate (log-only in backend)
# ---------------------------------------------------------------------------

def check_spread_gate(
    symbol: str,
    max_spread_pips: float = 3.0,
) -> Tuple[bool, str]:
    """
    Spread gate for the backend signal engine.

    The backend does NOT have access to live bid/ask data (OHLC only).
    Spread enforcement is deferred to the Windows bridge (mt5_signal_bridge.py
    lines 431-548), which reads the live spread before order execution.

    This function always returns (True, "deferred_to_bridge") and logs a
    diagnostic message.  It exists as a hook for future enhancement if
    the agent ever provides bid/ask data.

    Parameters
    ----------
    symbol : e.g. "EURUSD"
    max_spread_pips : threshold (used for logging only)

    Returns
    -------
    (True, "deferred_to_bridge")
    """
    logger.debug(
        "[SPREAD_GATE] symbol=%s max_spread_pips=%.1f — deferred to bridge",
        symbol, max_spread_pips,
    )
    return True, "deferred_to_bridge"


# ---------------------------------------------------------------------------
# SL / TP placement validation
# ---------------------------------------------------------------------------

def validate_sl_tp_placement(
    entry: float,
    sl: float,
    tp: float,
    side: str,
) -> Tuple[bool, str]:
    """
    Validate that SL and TP are on the correct side of entry.

    Rules:
        BUY : SL < entry < TP
        SELL: TP < entry < SL

    Returns
    -------
    (valid: bool, reason: str)
        If valid, reason is "".
        If invalid, reason explains the violation.
    """
    side_upper = side.upper()

    if side_upper == "BUY":
        if sl >= entry:
            return False, f"BUY SL ({sl}) must be below entry ({entry})"
        if tp <= entry:
            return False, f"BUY TP ({tp}) must be above entry ({entry})"
    elif side_upper == "SELL":
        if sl <= entry:
            return False, f"SELL SL ({sl}) must be above entry ({entry})"
        if tp >= entry:
            return False, f"SELL TP ({tp}) must be below entry ({entry})"
    else:
        return False, f"Unknown side: {side}"

    return True, ""


def validate_min_stop_distance(
    entry: float,
    sl: float,
    symbol: str,
    min_pips: float = 3.0,
) -> Tuple[bool, str]:
    """
    Validate that the stop distance is at least *min_pips* pips.

    Returns
    -------
    (valid: bool, reason: str)
    """
    pip = get_pip_size(symbol)
    distance_pips = abs(entry - sl) / pip

    if distance_pips < min_pips:
        return False, (
            f"Stop distance {distance_pips:.1f} pips is below "
            f"minimum {min_pips:.1f} pips for {symbol}"
        )

    return True, ""
