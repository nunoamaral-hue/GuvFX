"""
TBP V3 Hybrid Sleeve v1 — Wrapper engine

Composes:
  CORE  : existing TBP engine (trendline-break-pocket-ali)
  SLEEVE: existing TC1 engine (tc1-engine-v1), ONLY on risk-on days
           and ONLY for EURUSD + GBPUSD

Risk-on definition (v1): macro label in {MILD_RISK_ON, STRONG_RISK_ON}

Until the macro provider is upgraded (currently a stub returning
"UNKNOWN"), the sleeve is dormant — all risk allocation goes to CORE.

Algorithm (deterministic):
  1. Hard guards: demo-only, stage == LIVE
  2. Risk gates: check_risk_gates()
  3. Macro label -> risk-on determination
  4. Sleeve eligibility: risk-on AND symbol in {EURUSD, GBPUSD}
  5. Weights: w_sleeve = alpha if sleeve_enabled else 0; w_core = 1 - w_sleeve
  6. Base risk from PORTFOLIO_RISK_V1_1 map -> core / sleeve allocation
  7. Call CORE (TBP) as dispatcher does — normal lot sizing — then
     rescale lots: lots_scaled = lots * (core_risk_pct / strategy.risk_per_trade_pct)
     (NO model mutation; deterministic post-hoc rescaling)
  8. CORE_PRIORITY: if TBP fires -> use it
  9. Else if sleeve_enabled -> call TC1 with sleeve_risk_pct
 10. Else -> NO_ACTION
 11. Record audit event

Job creation pattern:
  CORE (TBP): wrapper returns SignalResult (no job_id). Dispatcher creates job.
  SLEEVE (TC1): TC1 creates job internally. Wrapper returns result as-is.
  (This asymmetry is documented for v1; both paths work correctly.)

Safety: demo-only guard, stage gating, market orders with SL/TP,
risk gates via risk_manager.check_risk_gates().
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict

from strategies.risk_manager import (
    check_risk_gates,
    record_signal_event,
)
from strategies.models import StrategyRuntimeEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HYBRID_TEMPLATE_SLUG = "tbp-v3-hybrid-sleeve-v1"

# Reason codes (stable enum)
STAGE_NOT_LIVE = "STAGE_NOT_LIVE"
DEMO_ONLY_GUARD = "DEMO_ONLY_GUARD"
CORE_TBP_SIGNAL = "CORE_TBP_SIGNAL"
SLEEVE_TC1_SIGNAL = "SLEEVE_TC1_SIGNAL"
SLEEVE_DISABLED_RISK_OFF = "SLEEVE_DISABLED_RISK_OFF"
SLEEVE_DISABLED_SYMBOL = "SLEEVE_DISABLED_SYMBOL"
MACRO_LABEL_UNAVAILABLE = "MACRO_LABEL_UNAVAILABLE"
NO_SIGNAL = "NO_SIGNAL"
LOT_SIZE_INVALID = "LOT_SIZE_INVALID"

# Portfolio risk mapping v1.1  (fraction — 0.03 = 3 %)
PORTFOLIO_RISK_V1_1: Dict[str, float] = {
    "EURUSD": 0.03,
    "XAUUSD": 0.0225,     # reserved; XAUUSD not in SIGNAL_ALLOWED_SYMBOLS yet
    "GBPUSD": 0.015,
}
PORTFOLIO_RISK_DEFAULT = 0.015
RISK_MAP_VERSION = "PORTFOLIO_RISK_V1_1"

# Sleeve-eligible pairs (v1 — EURUSD + GBPUSD only)
SLEEVE_PAIRS = frozenset({"EURUSD", "GBPUSD"})

# Risk-on macro labels
RISK_ON_LABELS = frozenset({"MILD_RISK_ON", "STRONG_RISK_ON"})

# Default alpha (sleeve weight when enabled)
DEFAULT_ALPHA = 0.25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_audit(
    macro_label: str,
    is_risk_on: bool,
    alpha: float,
    w_core: float,
    w_sleeve: float,
    base_risk_pct: float,
    core_risk_pct: float,
    sleeve_risk_pct: float,
    engine_selected: str,
) -> Dict[str, Any]:
    """Build the standard portfolio audit payload."""
    return {
        "portfolio_definition": "TBP_V3_HYBRID_SLEEVE_V1",
        "template_slug": HYBRID_TEMPLATE_SLUG,
        "macro_regime_label": macro_label,
        "is_risk_on": 1 if is_risk_on else 0,
        "alpha": alpha,
        "w_core": round(w_core, 6),
        "w_sleeve": round(w_sleeve, 6),
        "base_risk_pct": round(base_risk_pct, 6),
        "core_risk_pct": round(core_risk_pct, 6),
        "sleeve_risk_pct": round(sleeve_risk_pct, 6),
        "engine_selected": engine_selected,
        "risk_map_version": RISK_MAP_VERSION,
    }


def _no_action(
    assignment,
    symbol: str,
    reason: str,
    event_type: str,
    payload: Dict[str, Any],
    bar_close_time: str,
    SignalResult,
) -> "SignalResult":
    """Record event + return NO_ACTION SignalResult (reduces boilerplate)."""
    payload["reason_code"] = reason
    record_signal_event(
        assignment=assignment,
        strategy_key=HYBRID_TEMPLATE_SLUG,
        symbol=symbol,
        event_type=event_type,
        reason_code=reason,
        payload=payload,
        bar_close_time=bar_close_time,
    )
    return SignalResult(
        ok=True,
        signal_type=None,
        symbol=symbol,
        reason=reason,
        details=payload,
    )


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def evaluate_tbp_v3_hybrid_sleeve_v1(
    strategy,
    account,
    assignment,
    symbol: str,
    now_ts: datetime,
    bar_close_time: str = "",
    *,
    dry_run: bool = False,
) -> "SignalResult":
    """
    Evaluate the TBP V3 Hybrid Sleeve wrapper.

    Deterministic composition of CORE (TBP) + SLEEVE (TC1).

    CORE is always evaluated.  SLEEVE is evaluated only when:
      - macro regime is risk-on (MILD_RISK_ON or STRONG_RISK_ON)
      - symbol is in SLEEVE_PAIRS (EURUSD, GBPUSD)

    Selection rule (CORE_PRIORITY):
      1. If TBP fires -> use TBP signal (rescaled lots)
      2. Else if sleeve_enabled and TC1 fires -> use TC1 signal
      3. Else -> NO_ACTION

    CORE results are returned WITHOUT job_id; the dispatcher creates the
    PLACE_ORDER job.  SLEEVE results may include job_id (TC1 creates jobs
    internally and respects dry_run).

    Returns a SignalResult (imported from signal_engine to avoid circular).
    """
    # Lazy imports — break circular dependency (same pattern as TC1/ALTS/SCE)
    from strategies.signal_engine import (
        SignalResult,
        TrendlineBreakPocketConfig,
        validate_signal_safety,
        fetch_rates,
        RatesFetchError,
        evaluate_trendline_break_pocket_signal,
    )
    from strategies.engines.tc1_engine_v1 import evaluate_tc1_engine_v1, TC1Config
    from strategies.services.macro import get_macro_regime_label, MACRO_PROVIDER_VERSION
    from execution.models import ExecutionJob

    filters = strategy.filters or {}

    diag: Dict[str, Any] = {
        "engine_name": "TBP_V3_HYBRID_SLEEVE_V1",
        "template_slug": HYBRID_TEMPLATE_SLUG,
        "symbol": symbol,
        "bar_close_time": bar_close_time,
        "dry_run": dry_run,
    }

    # =================================================================
    # 1. HARD GUARD — demo only
    # =================================================================
    if not getattr(account, "is_demo", False):
        return _no_action(
            assignment, symbol, DEMO_ONLY_GUARD,
            StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            diag, bar_close_time, SignalResult,
        )

    # =================================================================
    # 2. HARD GUARD — stage must be LIVE
    # =================================================================
    if getattr(assignment, "stage", "") != "LIVE":
        return _no_action(
            assignment, symbol, STAGE_NOT_LIVE,
            StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED,
            diag, bar_close_time, SignalResult,
        )

    # =================================================================
    # 3. Risk gates (shared with all engines)
    # =================================================================
    risk_limits = {
        "daily_loss_cap_r": filters.get("daily_loss_cap_r", 3.0),
        "daily_trade_cap": filters.get("max_trades_per_day", 4),
        "weekly_loss_cap_r": filters.get("weekly_loss_cap_r", 6.0),
        "max_concurrent_positions": filters.get("max_concurrent_positions", 1),
        "consecutive_loss_pause": filters.get("consecutive_loss_pause", 3),
        "cooldown_minutes": filters.get("cooldown_minutes", 120),
        "no_trade_hours": filters.get("no_trade_hours", []),
    }
    allowed, risk_reason = check_risk_gates(
        assignment=assignment,
        strategy_key=HYBRID_TEMPLATE_SLUG,
        symbol=symbol,
        config_risk_limits=risk_limits,
        now_ts=now_ts,
    )
    if not allowed:
        diag["risk_reason"] = risk_reason
        return _no_action(
            assignment, symbol, f"RISK_GATE:{risk_reason}",
            StrategyRuntimeEvent.EVENT_RISK_THROTTLED,
            diag, bar_close_time, SignalResult,
        )

    # =================================================================
    # 4. Macro label / risk-on
    # =================================================================
    try:
        macro_label = get_macro_regime_label(
            now_ts, account=account, assignment=assignment,
        )
    except Exception:
        macro_label = "UNKNOWN"

    is_risk_on = macro_label in RISK_ON_LABELS

    # =================================================================
    # 5. Sleeve eligibility
    # =================================================================
    sleeve_pair_allowed = symbol in SLEEVE_PAIRS
    sleeve_enabled = is_risk_on and sleeve_pair_allowed

    if not is_risk_on:
        sleeve_reason = (
            MACRO_LABEL_UNAVAILABLE if macro_label == "UNKNOWN"
            else SLEEVE_DISABLED_RISK_OFF
        )
    elif not sleeve_pair_allowed:
        sleeve_reason = SLEEVE_DISABLED_SYMBOL
    else:
        sleeve_reason = ""  # sleeve is active

    # =================================================================
    # 6. Weights (alpha from filters, default 0.25)
    # =================================================================
    raw_alpha = filters.get("alpha", DEFAULT_ALPHA)
    try:
        alpha = float(raw_alpha)
    except (TypeError, ValueError):
        alpha = DEFAULT_ALPHA

    w_sleeve = alpha if sleeve_enabled else 0.0
    w_core = 1.0 - w_sleeve

    # =================================================================
    # 7. Base risk mapping (PORTFOLIO_RISK_V1_1)
    # =================================================================
    base_risk_pct = PORTFOLIO_RISK_V1_1.get(symbol, PORTFOLIO_RISK_DEFAULT)
    core_risk_pct = base_risk_pct * w_core
    sleeve_risk_pct = base_risk_pct * w_sleeve

    # =================================================================
    # 8. Call CORE (TBP) — NO model mutation
    #    TBP evaluates at strategy.risk_per_trade_pct for lot sizing.
    #    We rescale lots AFTER: lots *= (core_risk_pct / risk_per_trade_pct)
    # =================================================================
    core_filters = {**filters, "template_slug": "trendline-break-pocket-ali"}
    tbp_config = TrendlineBreakPocketConfig.from_filters(core_filters)

    core_result = None  # will hold SignalResult if CORE fires

    is_valid, error_reason = validate_signal_safety(
        strategy, account, assignment, symbol, tbp_config,
    )
    if is_valid:
        try:
            h4_count = min(tbp_config.trendline_lookback_bars + 50, 300)
            h4_rates = fetch_rates(account, symbol, "H4", count=h4_count)
            d1_rates = fetch_rates(account, symbol, "D1", count=100)
        except RatesFetchError as exc:
            logger.warning(
                "[HYBRID] CORE rates fetch failed for %s: %s", symbol, exc,
            )
            h4_rates = None
            d1_rates = None

        if h4_rates and d1_rates:
            core_signal = evaluate_trendline_break_pocket_signal(
                strategy=strategy,
                account=account,
                assignment=assignment,
                symbol=symbol,
                config=tbp_config,
                h4_rates=h4_rates,
                d1_rates=d1_rates,
            )

            if core_signal.ok and core_signal.signal_type in ("BUY", "SELL"):
                # --- Lot rescaling (deterministic, no mutation) ---
                strat_risk = float(strategy.risk_per_trade_pct or 0)
                if strat_risk > 0 and core_signal.lots:
                    scale = core_risk_pct / strat_risk
                    lots_scaled = round(core_signal.lots * scale, 2)
                    if lots_scaled <= 0:
                        logger.warning(
                            "[HYBRID] Rescaled lots <= 0: "
                            "lots_original=%s scale=%s",
                            core_signal.lots, scale,
                        )
                        diag["core_skip_reason"] = LOT_SIZE_INVALID
                    else:
                        core_signal.lots = lots_scaled
                        core_result = core_signal
                elif core_signal.lots:
                    # risk_per_trade_pct is 0/None — can't scale
                    logger.warning(
                        "[HYBRID] Cannot rescale lots: "
                        "risk_per_trade_pct=%s", strat_risk,
                    )
                    diag["core_skip_reason"] = LOT_SIZE_INVALID
                    # Do NOT select this signal
                # else: no lots in signal — no action
    else:
        logger.debug(
            "[HYBRID] CORE safety check failed for %s: %s",
            symbol, error_reason,
        )

    # =================================================================
    # 9. Selection rule: CORE_PRIORITY -> fallback to SLEEVE
    # =================================================================
    engine_selected = "NONE"
    selected_result = None

    if core_result and core_result.signal_type in ("BUY", "SELL"):
        engine_selected = "TBP"
        selected_result = core_result
        selected_result.reason = CORE_TBP_SIGNAL

    elif sleeve_enabled:
        # Build TC1 config with overridden risk_pct (new instance — no mutation)
        tc1_cfg = TC1Config.from_filters(filters)
        tc1_cfg.risk_pct = sleeve_risk_pct

        sleeve_result = evaluate_tc1_engine_v1(
            strategy=strategy,
            account=account,
            assignment=assignment,
            symbol=symbol,
            config=tc1_cfg,
            now_ts=now_ts,
            bar_close_time=bar_close_time,
            dry_run=dry_run,
        )

        if sleeve_result.ok and sleeve_result.signal_type in ("BUY", "SELL"):
            engine_selected = "TC1"
            selected_result = sleeve_result
            selected_result.reason = SLEEVE_TC1_SIGNAL

    # =================================================================
    # 10. Build audit payload
    # =================================================================
    audit = _build_audit(
        macro_label=macro_label,
        is_risk_on=is_risk_on,
        alpha=alpha,
        w_core=w_core,
        w_sleeve=w_sleeve,
        base_risk_pct=base_risk_pct,
        core_risk_pct=core_risk_pct,
        sleeve_risk_pct=sleeve_risk_pct,
        engine_selected=engine_selected,
    )
    if sleeve_reason:
        audit["sleeve_reason"] = sleeve_reason

    # =================================================================
    # 11. Annotate SLEEVE job payload (TC1 created job internally)
    #     CORE jobs are created by the dispatcher AFTER this function
    #     returns, so we annotate CORE jobs in the dispatcher branch.
    # =================================================================
    if selected_result and selected_result.job_id:
        try:
            job = ExecutionJob.objects.get(pk=selected_result.job_id)
            job.payload["portfolio"] = audit
            job.payload["macro_label"] = macro_label or "UNKNOWN"
            job.payload["macro_provider"] = MACRO_PROVIDER_VERSION
            job.save(update_fields=["payload"])
        except Exception as exc:
            logger.warning(
                "[HYBRID] Failed to annotate job %s: %s",
                selected_result.job_id, exc,
            )

    # =================================================================
    # 12. Record wrapper-level audit event
    # =================================================================
    event_payload: Dict[str, Any] = {**diag, "portfolio": audit}
    if selected_result:
        event_payload["selected_signal"] = selected_result.to_dict()

    event_type = (
        StrategyRuntimeEvent.EVENT_SIGNAL_FIRED
        if engine_selected != "NONE"
        else StrategyRuntimeEvent.EVENT_SIGNAL_SKIPPED
    )

    record_signal_event(
        assignment=assignment,
        strategy_key=HYBRID_TEMPLATE_SLUG,
        symbol=symbol,
        event_type=event_type,
        reason_code=(
            selected_result.reason if selected_result
            else (sleeve_reason or NO_SIGNAL)
        ),
        payload=event_payload,
        bar_close_time=bar_close_time,
    )

    # =================================================================
    # 13. Return result
    # =================================================================
    if selected_result:
        details = selected_result.details or {}
        details["portfolio"] = audit
        selected_result.details = details
        return selected_result

    return SignalResult(
        ok=True,
        signal_type=None,
        symbol=symbol,
        reason=sleeve_reason or NO_SIGNAL,
        details={**diag, "portfolio": audit},
    )
