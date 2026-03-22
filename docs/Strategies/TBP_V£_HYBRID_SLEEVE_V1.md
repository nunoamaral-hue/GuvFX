# TBP_V3_HYBRID_SLEEVE_V1 (Demo-only)

## Purpose
Portfolio strategy combining:
- **Core engine**: TBP v2 top1-per-pair (EURUSD, GBPUSD, XAUUSD)
- **Risk-on sleeve**: TC1 (EURUSD + GBPUSD) applied only on risk-on days
- Portfolio-level risk controls: correlation dampening, vol targeting, macro overlay (existing modules)

## Data + Timeframes
- Execution timeframe: H4
- Intrabar resolution: M5 (simulate_trade replay)
- Macro score: MACRO_SCORE_GLOBAL_V1 (mixed TF; daily label)

## Macro Labels
Labels from MACRO_SCORE_GLOBAL_V1:
- STRONG_RISK_OFF
- MILD_RISK_OFF
- NEUTRAL
- MILD_RISK_ON
- STRONG_RISK_ON

Define:
- risk_on_day := label in {MILD_RISK_ON, STRONG_RISK_ON}

## Engines

### Core: TBP v2 (Top1-per-pair)
For each pair in {EURUSD, GBPUSD, XAUUSD}:
- select top config per pair from stability results (robust_score max)
- trade stream provides r_portfolio per trade
- daily core return = sum of all TBP trade r_portfolio on that UTC day

### Sleeve: TC1 (Risk-on only)
Pairs: {EURUSD, GBPUSD}
- TC1 runs only on risk_on_day
- TC1 produces raw-R per trade; convert to portfolio R:
  r_portfolio = raw_R * base_risk_pct(pair)

### Sleeve weighting
alpha = 0.25
For each UTC day t:
- if risk_on_day(t):
    w_sleeve = alpha
    w_core   = 1 - alpha
  else:
    w_sleeve = 0
    w_core   = 1

Daily blended return:
R(t) = w_core * R_core(t) + w_sleeve * R_sleeve(t)

## Portfolio risk layers (unchanged)
Apply existing pipeline in this order (as currently implemented):
1) correlation dampening (portfolio-level)
2) macro overlay multiplier (MACRO_OVERLAY_V1)
3) portfolio volatility targeting (PORTFOLIO_VOL_TARGET_V1)
4) compound equity by day

## Audit requirements
Must output daily audit fields:
- core_day_return, sleeve_day_return
- w_core, w_sleeve, is_risk_on
- pnl_day, equity_end
- macro_regime_label, macro_scale
- vol_target, realized_vol, vol_scale

## Execution constraints (GuvFX)
- Demo-only initially
- Orders: market orders only; SL/TP mandatory
- Deterministic reason codes
- Full logging + reproducible runbooks