# GuvFX Strategy Builder Specification

This document defines the functional and data model requirements for the GuvFX Strategy Builder. It is the single source of truth for backend models, serializers, and frontend UI.

The goals:
- Express all core components of a trading strategy as **configuration only** (no user code execution).
- Align with how traders think (13 components), while mapping cleanly onto Django + Next.js.
- Keep security constraints from Sid: no dynamic code, no secrets in strategy config, strict enums and numeric ranges.

---

## 1. Core Entities and Design Principles

### 1.1 Strategy as a configuration object

A `Strategy` represents a single trading strategy owned by a user. It is a structured configuration with the following categories:

1. Market & Timeframe
2. Trade Idea (Edge)
3. Setup Conditions
4. Entry Rules
5. Stop Loss Rules
6. Take Profit Rules
7. Position Sizing
8. Trade Management
9. Filters & Conditions (including News)
10. Risk & Money Management (overall)
11. Trading Plan & Routine
12. Psychology Rules
13. Backtesting & Metrics (links, not config)

Implementation principle:
- All of the above are represented as **simple fields or JSON** (enums, strings, numbers, lists, dicts).
- No user-uploaded code, no eval/exec, no dynamic imports.
- Any execution engine (MT5/Python) consumes this config and implements the logic.

### 1.2 High-level Strategy schema (Python view)

The Strategy model must at least include:

- Identity & ownership
  - `owner`: FK to User
  - `name`: str
  - `description`: text

- Market & timeframe
  - `style`: enum
    - `SCALPER`, `INTRADAY`, `SWING`, `POSITION`
  - `market_type`: enum
    - `FOREX`, `INDICES`, `GOLD`, `CRYPTO` (others out of scope for now)
  - `symbol_universe`: string or array of instrument symbols (e.g. "EURUSD,GBPUSD")
  - `timeframe`: enum (e.g. `M1`, `M3`, `M5`, `M15`, `M30`, `H1`, `H4`, `D1`, `W1`)

- Trade idea (edge)
  - `edge_type`: enum
    - `TREND_FOLLOWING`, `MEAN_REVERSION`, `BREAKOUT`, `NEWS_FUNDAMENTAL`
  - `edge_rationale`: short text

- Legacy MA fields (for backward compatibility)
  - `ma_fast_period`: int | null
  - `ma_slow_period`: int | null
  - `ma_type`: enum `SMA|EMA|WMA` | ""

- AI flag
  - `auto_optimize_by_ai`: bool

- Config JSON fields
  - `indicator_blocks`: list of indicator config blocks
  - `entry_rules`: JSON
  - `sl_rules`: JSON
  - `tp_rules`: JSON
  - `filters`: JSON (incl. `news_filter`)
  - `trade_management`: JSON
  - `risk_limits`: JSON
  - `plan_meta`: JSON (plan/routine/psychology metadata)

Audit fields:
- `created_at`, `updated_at` (DateTime)

### 1.3 Indicator blocks and role-based signals

Indicators, oscillators, volatility measures, and pattern recognisers are all represented as **indicator blocks**.

Each block has:

```jsonc
{
  "id": "trend_ma_1",          // stable id used in UI & engine
  "type": "MA",                // MA | RSI | ATR | PATTERN | ...
  "role": "TREND_FILTER",      // TREND_FILTER | ENTRY_FILTER | VOLATILITY_FILTER | PATTERN_FILTER
  "enabled": true,
  "params": {                   // type-specific parameters
    "period": 50,
    "ma_type": "EMA",
    "applied_price": "CLOSE"
  }
}
```

Supported `type` values for v1:
- `MA` – Moving Average
- `RSI` – Relative Strength Index
- `ATR` – Average True Range (volatility)
- `PATTERN` – Chart / price pattern signals

Supported `role` values for v1:
- `TREND_FILTER`
- `ENTRY_FILTER`
- `VOLATILITY_FILTER`
- `PATTERN_FILTER`

Security requirements (Sid):
- `type` and `role` are enums validated server-side.
- `params` are validated per type (min/max ranges, allowed strings).
- No arbitrary field is ever interpreted as code.

Indicator-specific params:

- **MA (Moving Average)**
  - `period`: int, 2–500
  - `ma_type`: `SMA|EMA|WMA`
  - `applied_price`: `OPEN|HIGH|LOW|CLOSE`
  - Optional `timeframe_override`: timeframe string or null

- **RSI**
  - `period`: int, 5–50
  - `overbought`: int, typical default 70
  - `oversold`: int, typical default 30

- **ATR**
  - `period`: int, 5–50
  - `multiplier`: float, 0.1–10.0

- **PATTERN** (chart patterns)
  - `pattern_type`: enum
    - `HEAD_AND_SHOULDERS`, `DOUBLE_TOP`, `DOUBLE_BOTTOM`, `TRIANGLE_CONTINUATION`, `FLAG`
  - `direction`: enum `BULLISH|BEARISH|ANY`
  - `timeframe_override`: timeframe string or null

The frontend should:
- Always manipulate these as JSON blocks.
- Provide toggles and inputs for each known type.

---

## 2. Mapping the 13 Strategy Components to Fields

The 13 components from the trading perspective map to the Strategy config as follows:

### 2.1 Market & Timeframe (Component 1)

Fields:
- `style`: SCALPER | INTRADAY | SWING | POSITION
- `market_type`: FOREX | INDICES | GOLD | CRYPTO
- `symbol_universe`: comma-separated instruments (within allowed list per market_type)
- `timeframe`: see below

Constraints:
- If `style = SCALPER` → allowed `timeframe` = `M1`, `M3`, `M5` only.
- If `style = INTRADAY` → allowed `timeframe` = `M15`, `M30`, `H1`.
- If `style = SWING` → `H4`, `D1`.
- If `style = POSITION` → `D1`, `W1`.

- If `market_type = FOREX` → `symbol_universe` restricted to FX pairs (e.g. EURUSD, GBPUSD, USDJPY, etc.).
- Other markets (indices, gold, crypto) can be supported with separate whitelists.

### 2.2 Trade Idea (Edge) (Component 2)

Fields:
- `edge_type`: TREND_FOLLOWING | MEAN_REVERSION | BREAKOUT | NEWS_FUNDAMENTAL
- `edge_rationale`: text (short description)

Impact on defaults:
- `TREND_FOLLOWING` → default `indicator_blocks` includes MA/ADX-style TREND_FILTER.
- `MEAN_REVERSION` → default `indicator_blocks` includes RSI ENTRY_FILTER with OB/OS.
- `BREAKOUT` → default to level-based patterns and PRICE_BREAK entry rules.
- `NEWS_FUNDAMENTAL` → enables/raises importance of `news_filter` in `filters`.

### 2.3 Setup Conditions (Component 3)

Concept: the situation you wait for before even thinking of entry.

Fields (mainly in `indicator_blocks` and `entry_rules`):
- Trend requirement: encoded via MA or ADX-style TREND_FILTER blocks.
- Price location (near support/resistance): encoded via filters using levels/ATR distance.
- Indicator alignment: combos of MA, RSI, ATR with roles `TREND_FILTER` or `SETUP_FILTER` (if added later).

### 2.4 Entry Rules (Component 4)

Fields:
- Stored in `entry_rules` JSON.

Example structure:

```json
"entry_rules": {
  "type": "CANDLE_PATTERN",          // CANDLE_PATTERN | INDICATOR_SIGNAL | PRICE_BREAK
  "patterns": ["PIN_BAR", "ENGULFING"],
  "direction": "BULLISH",           // BULLISH | BEARISH | ANY
  "indicator": {
    "type": "RSI",
    "cross": "ABOVE",
    "level": 50
  },
  "price_break": {
    "relation": "ABOVE_RANGE_HIGH",
    "buffer_pips": 2,
    "require_close": true
  }
}
```

Rules:
- `type` is an enum.
- Only known pattern names and indicator types allowed.
- Engine will interpret these against price data.

### 2.5 Stop Loss Rules (Component 5)

Fields:
- Stored in `sl_rules` JSON.

Example:

```json
"sl_rules": {
  "method": "ATR_MULTIPLE",        // SWING_HIGH_LOW | FIXED_PIPS | ATR_MULTIPLE
  "atr_period": 14,
  "atr_multiple": 1.5,
  "fixed_pips": null,
  "swing_buffer_pips": 3
}
```

### 2.6 Take Profit Rules (Component 6)

Fields:
- Stored in `tp_rules` JSON.

Example:

```json
"tp_rules": {
  "primary": "FIXED_RR",           // FIXED_RR | LEVEL_BASED | TRAILING | PARTIALS
  "rr_target": 2.0,
  "use_trailing": true,
  "trailing": {
    "method": "ATR_TRAIL",         // ATR_TRAIL | SWING_TRAIL
    "atr_period": 14,
    "atr_multiple": 1.5
  },
  "partials": [
    { "at_r_multiple": 1.0, "exit_fraction": 0.5 },
    { "at_r_multiple": 2.0, "exit_fraction": 0.5 }
  ]
}
```

### 2.7 Position Sizing (Component 7)

Fields (partly in Strategy and partly in `risk_limits`):
- `risk_per_trade_pct`: decimal (0.25–2.0 typical)
- `sizing_mode`: enum
  - `FIXED_RISK_PERCENT`
  - `FIXED_LOT_SIZE` (optional)
- `fixed_lot_size`: decimal | null

The engine uses:
- Account equity
- `sl_rules` to compute SL distance
- `risk_per_trade_pct` to derive lot size.

### 2.8 Trade Management (Component 8)

Fields:
- Stored in `trade_management` JSON.

Example:

```json
"trade_management": {
  "move_to_breakeven": {
    "enabled": true,
    "at_r_multiple": 1.0
  },
  "pyramiding": {
    "enabled": false,
    "max_additions": 0,
    "spacing_r_multiple": 1.0
  }
}
```

### 2.9 Filters & Conditions (Component 9)

Fields:
- Stored in `filters` JSON.
- Includes `news_filter`.

Example:

```json
"filters": {
  "news_filter": {
    "mode": "AVOID_NEWS",          // AVOID_NEWS | NEWS_ONLY | NEWS_BIASED
    "impact_levels": ["HIGH"],
    "event_types": ["NFP", "CPI"],
    "pre_event_minutes": 30,
    "post_event_minutes": 30
  },
  "time_filters": {
    "avoid_friday_close": true,
    "avoid_rollover": true
  },
  "max_trades_per_day": 5
}
```

Security:
- News integration is driven by an internal calendar API; event names and impact levels are enums only.
- No user-specified URLs or code.

### 2.10 Risk & Money Management (Component 10)

Fields:
- Stored in `risk_limits` JSON.

Example:

```json
"risk_limits": {
  "daily_max_loss_r": 3.0,
  "weekly_max_loss_r": 8.0,
  "max_open_risk_pct": 5.0,
  "correlation_groups": [
    {
      "name": "USD majors",
      "symbols": ["EURUSD", "GBPUSD", "USDJPY"],
      "max_total_risk_pct": 3.0
    }
  ]
}
```

These limits should be enforced at execution time.

### 2.11 Trading Plan & Routine (Component 11)

Fields:
- Stored in `plan_meta` JSON.

Example:

```json
"plan_meta": {
  "pre_session_checklist": [
    "Check economic calendar",
    "Mark key levels",
    "Define directional bias"
  ],
  "post_session_checklist": [
    "Review trades",
    "Capture screenshots",
    "Update journal"
  ]
}
```

### 2.12 Psychology Rules (Component 12)

Fields:
- Can be embedded in `plan_meta` or separate `psychology_rules` section inside `plan_meta`.

Example:

```json
"plan_meta": {
  "psychology_rules": {
    "after_big_win_r": 3.0,
    "cooldown_minutes_after_big_win": 30,
    "max_consecutive_losses_before_reduce_size": 3,
    "reduced_risk_per_trade_pct": 0.5
  }
}
```

### 2.13 Backtesting & Metrics (Component 13)

Backtesting itself is handled by the `backtests` app. Strategy config interacts with backtesting by:
- Providing a consistent structure that backtest engines can interpret.
- Exposing backtest configs and results on the Strategy detail page.

Data:
- Backtest metrics (win rate, avg R, max DD, trade count) are NOT stored in Strategy; they live in BacktestRun/analytics tables.
- Strategy can reference "preferred" backtest config id if needed (optional).

---

## 3. Frontend UX Guidelines (Wizard + Detail Page)

### 3.1 Strategy creation wizard

- `/strategies` page presents a "Create Strategy" wizard that guides through:
  1. Market & Timeframe (style, market_type, symbol_universe, timeframe)
  2. Trade Idea (edge_type, edge_rationale)
  3. Setup & Entry (maps to indicator_blocks + entry_rules)
  4. Stops & Targets (sl_rules + tp_rules)
  5. Sizing & Management (risk_per_trade_pct, trade_management)
  6. Filters (filters.news_filter, time filters, max trades per day)
  7. Risk Limits (risk_limits)
  8. Plan & Routine (plan_meta)

- Each step saves to the same Strategy via API.

### 3.2 Strategy detail page

The Strategy detail page should show:
- A summary of the core info (name, style, market, timeframe, edge_type).
- Editable sections:
  - Core settings (metadata + market/timeframe).
  - Signals & Indicators (indicator_blocks for MA/RSI/ATR/PATTERN).
  - Entry/Exit rules (entry_rules, sl_rules, tp_rules).
  - Filters & News (filters.news_filter, time filters, max trades/day).
  - Risk & Money Management (risk_limits).
  - Plan & Routine (plan_meta).
  - Change history and AI suggestions (already implemented).

UX principles:
- Use toggles + collapsible sections to avoid overwhelming users.
- Provide defaults where possible (RSI 14/70/30, ATR 14/1.5, etc.).
- Always persist via PATCH to `/api/strategies/strategies/{id}/` using these config fields.

---

## 4. Security Constraints (Sid)

- No user-supplied code is ever executed.
- All strategy logic is represented as enums, numbers, and structured JSON.
- Indicator types and params are validated server-side against whitelists and ranges.
- News integration uses trusted providers and a fixed schema (`mode`, `impact_levels`, `event_types`, etc.).
- Sensitive data (MT5 passwords, KYC, payment secrets) is **not** part of Strategy config; those live in separate encrypted models.

This spec is the reference for all future Strategy model, serializer, and UI changes.
