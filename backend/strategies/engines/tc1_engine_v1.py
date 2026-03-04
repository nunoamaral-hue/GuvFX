"""
TC1 — Trend Continuation Engine v1

H4 execution timeframe.  Deterministic EMA-crossover + pullback-zone +
confirmation-candle strategy.

Algorithm (all steps evaluated on the last closed H4 bar):
  1. EMA50 / EMA200 on H4 closes  →  trend direction.
  2. Pullback zone: |close − EMA50| ≤ 0.25 × ATR14.
  3. Confirmation candle:
       BUY:  close > open AND close > EMA50
       SELL: close < open AND close < EMA50
  4. Entry at market (reference = latest close).
  5. SL = entry ∓ 1.2 × ATR14.
  6. TP = entry ± RR × SL_distance  (RR fixed 1.5 in v1).
  7. Lot sizing via calculate_lot_size (shared pipeline).

Safety: demo-only hard guard, stage==LIVE gate, max 0.02 lots,
EURUSD/GBPUSD only, all pre-checks via risk_manager.check_risk_gates().

This engine creates its own PLACE_ORDER jobs internally (matches the
pattern used by schedulers for ALTS/SCE).  The dispatcher just returns
the SignalResult.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from django.utils import timezone

from strategies.indicators import compute_atr, compute_ema
from strategies.execution_guards import (
    normalize_prices,
    validate_sl_tp_placement,
    validate_min_stop_distance,
    get_pip_size,
    check_spread_gate,
)
from strategies.risk_manager import (
    check_risk_gates,
    record_signal_event,
    increment_daily_trade_count,
    ORDER_PLACED,
)
from strategies.models import StrategyRuntimeEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TC1_TEMPLATE_SLUG = "tc1-engine-v1"

# Reason codes (stable enum — add to risk_manager.py if needed)
DEMO_ONLY_GUARD = "DEMO_ONLY_GUARD"
STAGE_NOT_LIVE = "STAGE_NOT_LIVE"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
TC1_SKIP_NO_TREND = "TC1_SKIP_NO_TREND"
TC1_SKIP_NOT_IN_PULLBACK_ZONE = "TC1_SKIP_NOT_IN_PULLBACK_ZONE"
TC1_SKIP_CANDLE_NOT_CONFIRM = "TC1_SKIP_CANDLE_NOT_CONFIRM"
LOT_SIZE_INVALID = "LOT_SIZE_INVALID"
TC1_SIGNAL_BUY = "TC1_SIGNAL_BUY"
TC1_SIGNAL_SELL = "TC1_SIGNAL_SELL"

# Algorithm parameters (deterministic, no macro in v1)
EMA_FAST = 50
EMA_SLOW = 200
ATR_PERIOD = 14
PULLBACK_ATR_MULT = 0.25   # price within ±0.25×ATR of EMA50
SL_ATR_MULT = 1.2           # SL distance = 1.2 × ATR14
RR_FIXED = 1.5              # fixed R:R for v1 (no macro dependency)
DEFAULT_RISK_PCT = 1.5      # 1.5 % (percentage value for calculate_lot_size)
H4_BAR_COUNT = 300          # need ≥200 for EMA200 seed
MIN_STOP_PIPS = 5.0         # minimum stop distance guard


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class TC1Config:
    """Configuration for TC1 engine, loaded from strategy.filters."""

    ema_fast: int = EMA_FAST
    ema_slow: int = EMA_SLOW
    atr_period: int = ATR_PERIOD
    pullback_atr_mult: float = PULLBACK_ATR_MULT
    sl_atr_mult: float = SL_ATR_MULT
    rr: float = RR_FIXED
    risk_pct: float = DEFAULT_RISK_PCT

    # Risk limits (passed to check_risk_gates)
    max_trades_per_day: int = 4
    daily_loss_cap_r: float = 3.0
    weekly_loss_cap_r: float = 6.0
    max_concurrent_positions: int = 1
    consecutive_loss_pause: int = 3
    cooldown_minutes: int = 120
    no_trade_hours: List[int] = field(default_factory=list)
    pairs_enabled: List[str] = field(
        default_factory=lambda: ["EURUSD", "GBPUSD"],
    )

    @classmethod
    def from_filters(cls, filters: dict) -> "TC1Config":
        """Create config from strategy.filters JSON."""
        return cls(
            ema_fast=filters.get("tc1_ema_fast", EMA_FAST),
            ema_slow=filters.get("tc1_ema_slow", EMA_SLOW),
            atr_period=filters.get("tc1_atr_period", ATR_PERIOD),
            pullback_atr_mult=filters.get("tc1_pullback_atr_mult", PULLBACK_ATR_MULT),
            sl_atr_mult=filters.get("tc1_sl_atr_mult", SL_ATR_MULT),
            rr=filters.get("tc1_rr", RR_FIXED),
            risk_pct=filters.get("tc1_risk_pct", DEFAULT_RISK_PCT),
            max_trades_per_day=filters.get("max_trades_per_day", 4),
            daily_loss_cap_r=filters.get("tc1_daily_loss_cap_r", 3.0),
            weekly_loss_cap_r=filters.get("tc1_weekly_loss_cap_r", 6.0),
            max_concurrent_positions=filters.get("max_concurrent_positions", 1),
            consecutive_loss_pause=filters.get("tc1_consecutive_loss_pause", 3),
            cooldown_minutes=filters.get("tc1_cooldown_minutes", 120),
            no_trade_hours=filters.get("tc1_no_trade_hours", []),
            pairs_enabled=filters.get("pairs_enabled", ["EURUSD", "GBPUSD"]),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _no_action(
    assignment,
    symbol: str,
    reason_code: str,
    event_type: str,
    diag: Dict[str, Any],
    bar_close_time: str,
    SignalResult,
) -> "SignalResult":
    """Record event and return a NO_ACTION SignalResult (reduces boilerplate)."""
    diag["reason_code"] = reason_code
    record_signal_event(
        assignment=assignment,
        strategy_key=TC1_TEMPLATE_SLUG,
        symbol=symbol,
        event_type=event_type,
        reason_code=reason_code,
        payload=diag,
        bar_close_time=bar_close_time,
    )
    return SignalResult(
        ok=True,
        signal_type=None,
        symbol=symbol,
        reason=reason_code,
        details=diag,
    )


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def evaluate_tc1_engine_v1(
    strategy,
    account,
    assignment,
    symbol: str,
    config: TC1Config,
    now_ts: datetime,
    bar_close_time: str = "",
    *,
    dry_run: bool = False,
) -> "SignalResult":
    """
    Evaluate TC1 trend-continuation signal for *symbol* on H4.

    Creates a PLACE_ORDER job internally when a signal fires
    (unless *dry_run* is True).

    Returns a SignalResult (imported from signal_engine to avoid circular).
    """
    # Lazy imports to break circular dependency (same pattern as SCE/ALTS)
    from strategies.signal_engine import (
        SignalResult,
        fetch_rates,
        RatesFetchError,
        calculate_lot_size,
        create_place_order_job,
    )

    diag: Dict[str, Any] = {
        "engine": {
            "engine_name": "TC1_ENGINE_V1",
            "template_slug": TC1_TEMPLATE_SLUG,
            "timeframe": "H4",
        },
        "symbol": symbol,
        "bar_close_time": bar_close_time,
        "dry_run": dry_run,
    }

    # -----------------------------------------------------------------
    # 1. HARD GUARD — demo only
    # -----------------------------------------------------------------
    if not getattr(account, "is_demo", False):
        return _no_action(
            assignment, symbol, DEMO_ONLY_GUARD,
            StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            diag, bar_close_time, SignalResult,
        )

    # -----------------------------------------------------------------
    # 2. HARD GUARD — stage must be LIVE
    # -----------------------------------------------------------------
    if getattr(assignment, "stage", "") != "LIVE":
        return _no_action(
            assignment, symbol, STAGE_NOT_LIVE,
            StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            diag, bar_close_time, SignalResult,
        )

    # -----------------------------------------------------------------
    # 3. Risk gates (shared with all engines)
    # -----------------------------------------------------------------
    risk_limits = {
        "daily_loss_cap_r": config.daily_loss_cap_r,
        "daily_trade_cap": config.max_trades_per_day,
        "weekly_loss_cap_r": config.weekly_loss_cap_r,
        "max_concurrent_positions": config.max_concurrent_positions,
        "consecutive_loss_pause": config.consecutive_loss_pause,
        "cooldown_minutes": config.cooldown_minutes,
        "no_trade_hours": config.no_trade_hours,
    }

    allowed, risk_reason = check_risk_gates(
        assignment=assignment,
        strategy_key=TC1_TEMPLATE_SLUG,
        symbol=symbol,
        config_risk_limits=risk_limits,
        now_ts=now_ts,
    )
    if not allowed:
        return _no_action(
            assignment, symbol, risk_reason,
            StrategyRuntimeEvent.EVENT_RISK_THROTTLED,
            diag, bar_close_time, SignalResult,
        )

    # -----------------------------------------------------------------
    # 4. Fetch H4 data
    # -----------------------------------------------------------------
    try:
        h4_bars = fetch_rates(account, symbol, "H4", count=H4_BAR_COUNT)
        logger.info("[TC1] Fetched %d H4 bars for %s", len(h4_bars), symbol)
    except RatesFetchError as exc:
        logger.warning("[TC1] rates fetch failed for %s: %s", symbol, exc)
        diag["error"] = str(exc)
        return SignalResult(
            ok=True, signal_type=None, symbol=symbol,
            reason="rates_fetch_failed", details=diag,
        )

    min_bars_needed = config.ema_slow + 10  # EMA200 seed + buffer
    if len(h4_bars) < min_bars_needed:
        diag["bars_received"] = len(h4_bars)
        diag["bars_needed"] = min_bars_needed
        return _no_action(
            assignment, symbol, INSUFFICIENT_DATA,
            StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            diag, bar_close_time, SignalResult,
        )

    # -----------------------------------------------------------------
    # 5. Indicators
    # -----------------------------------------------------------------
    ema50_series = compute_ema(h4_bars, config.ema_fast, field="close")
    ema200_series = compute_ema(h4_bars, config.ema_slow, field="close")
    atr14 = compute_atr(h4_bars, config.atr_period)

    last_idx = len(h4_bars) - 1
    ema50 = ema50_series[last_idx]
    ema200 = ema200_series[last_idx]

    # Update audit payload with indicator values
    diag["engine"]["ema50"] = round(ema50, 6) if ema50 is not None else None
    diag["engine"]["ema200"] = round(ema200, 6) if ema200 is not None else None
    diag["engine"]["atr14"] = round(atr14, 6) if atr14 else 0.0
    diag["bars_count"] = len(h4_bars)

    if ema50 is None or ema200 is None or atr14 <= 0:
        return _no_action(
            assignment, symbol, INSUFFICIENT_DATA,
            StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            diag, bar_close_time, SignalResult,
        )

    # -----------------------------------------------------------------
    # 6. Trend detection (EMA crossover)
    # -----------------------------------------------------------------
    if ema50 > ema200:
        trend_side = "BUY"
    elif ema50 < ema200:
        trend_side = "SELL"
    else:
        return _no_action(
            assignment, symbol, TC1_SKIP_NO_TREND,
            StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            diag, bar_close_time, SignalResult,
        )

    diag["engine"]["trend_side"] = trend_side

    # -----------------------------------------------------------------
    # 7. Pullback zone — price within ±pullback_atr_mult × ATR of EMA50
    # -----------------------------------------------------------------
    last_bar = h4_bars[last_idx]
    last_close = float(last_bar["close"])
    pullback_band = config.pullback_atr_mult * atr14

    diag["last_close"] = last_close
    diag["pullback_band"] = round(pullback_band, 6)
    diag["distance_to_ema50"] = round(abs(last_close - ema50), 6)

    if abs(last_close - ema50) > pullback_band:
        return _no_action(
            assignment, symbol, TC1_SKIP_NOT_IN_PULLBACK_ZONE,
            StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            diag, bar_close_time, SignalResult,
        )

    # -----------------------------------------------------------------
    # 8. Confirmation candle (last closed bar)
    # -----------------------------------------------------------------
    bar_open = float(last_bar["open"])

    if trend_side == "BUY":
        confirmed = last_close > bar_open and last_close > ema50
    else:
        confirmed = last_close < bar_open and last_close < ema50

    diag["candle_open"] = bar_open
    diag["candle_confirmed"] = confirmed

    if not confirmed:
        return _no_action(
            assignment, symbol, TC1_SKIP_CANDLE_NOT_CONFIRM,
            StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            diag, bar_close_time, SignalResult,
        )

    # -----------------------------------------------------------------
    # 9. Entry / SL / TP
    # -----------------------------------------------------------------
    entry_price = last_close  # market order at current close
    sl_distance = config.sl_atr_mult * atr14
    tp_distance = sl_distance * config.rr
    pip = get_pip_size(symbol)

    if trend_side == "BUY":
        sl_price = entry_price - sl_distance
        tp_price = entry_price + tp_distance
    else:
        sl_price = entry_price + sl_distance
        tp_price = entry_price - tp_distance

    entry_price, sl_price, tp_price = normalize_prices(
        entry_price, sl_price, tp_price, trend_side, symbol,
    )

    diag["entry_price"] = entry_price
    diag["sl_price"] = sl_price
    diag["tp_price"] = tp_price
    diag["sl_distance"] = round(sl_distance, 6)

    # Validate SL/TP placement
    valid, reason = validate_sl_tp_placement(entry_price, sl_price, tp_price, trend_side)
    if not valid:
        diag["sl_tp_error"] = reason
        return _no_action(
            assignment, symbol, "MIN_STOP_VIOLATION",
            StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            diag, bar_close_time, SignalResult,
        )

    valid_stop, stop_reason = validate_min_stop_distance(
        entry_price, sl_price, symbol, min_pips=MIN_STOP_PIPS,
    )
    if not valid_stop:
        diag["min_stop_error"] = stop_reason
        return _no_action(
            assignment, symbol, "MIN_STOP_VIOLATION",
            StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            diag, bar_close_time, SignalResult,
        )

    # Spread gate (log-only; hard enforcement in Windows bridge)
    _spread_ok, spread_reason = check_spread_gate(symbol, max_spread_pips=3.0)
    diag["spread_gate"] = spread_reason

    # -----------------------------------------------------------------
    # 10. Risk sizing
    # -----------------------------------------------------------------
    risk_pct = float(
        assignment.risk_per_trade_override_pct
        or strategy.risk_per_trade_pct
        or config.risk_pct
    )
    stop_pips = abs(entry_price - sl_price) / pip
    lots, lot_warning = calculate_lot_size(account, risk_pct, stop_pips, symbol)

    diag["engine"]["risk_pct"] = risk_pct
    diag["engine"]["lots"] = lots
    diag["engine"]["rr"] = config.rr
    diag["stop_pips"] = round(stop_pips, 1)
    if lot_warning:
        diag["lot_warning"] = lot_warning

    if lots is None or lots <= 0:
        return _no_action(
            assignment, symbol, LOT_SIZE_INVALID,
            StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            diag, bar_close_time, SignalResult,
        )

    # -----------------------------------------------------------------
    # 11. Signal + event
    # -----------------------------------------------------------------
    reason_code = TC1_SIGNAL_BUY if trend_side == "BUY" else TC1_SIGNAL_SELL

    diag["signal"] = {
        "side": trend_side,
        "entry": entry_price,
        "sl": sl_price,
        "tp": tp_price,
        "lots": lots,
        "rr": config.rr,
    }

    record_signal_event(
        assignment=assignment,
        strategy_key=TC1_TEMPLATE_SLUG,
        symbol=symbol,
        event_type=StrategyRuntimeEvent.EVENT_SIGNAL_FIRED,
        reason_code=ORDER_PLACED,
        payload=diag,
        bar_close_time=bar_close_time,
    )
    increment_daily_trade_count(assignment, TC1_TEMPLATE_SLUG, symbol)

    signal = SignalResult(
        ok=True,
        signal_type=trend_side,
        symbol=symbol,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        lots=lots,
        reason=reason_code,
        details=diag,
    )

    # -----------------------------------------------------------------
    # 12. Job creation (skip in dry-run)
    # -----------------------------------------------------------------
    if dry_run:
        diag["dry_run_skipped_job"] = True
        logger.info("[TC1] dry_run — skipping PLACE_ORDER for %s %s", trend_side, symbol)
        return signal

    job = create_place_order_job(
        request=None,
        strategy=strategy,
        account=account,
        assignment=assignment,
        signal=signal,
        user=strategy.owner,
        bar_close_time=bar_close_time,
    )

    # Attach engine audit to job payload
    job.payload["engine"] = diag["engine"]
    job.save(update_fields=["payload"])

    signal.job_id = job.id
    signal.reason = "job_queued"

    return signal
