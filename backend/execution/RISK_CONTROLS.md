# E3 Runtime Risk Controls (GFX-PKT-E3-RUNTIME-RISK-CONTROLS)

Pre-E3 runtime risk gates. **Additive, shadow-testable, fail-closed.** They place
NO order, call NO `order_send`, and do NOT touch the kill switch. They are the
pre-condition for enabling a future demo-live path — today they gate SHADOW
promotion and shadow-worker execution only.

## Controls

| # | Control | Where | Block reason code |
|---|---------|-------|-------------------|
| 0 | Terminal-node assignment (flag-gated, default OFF) | promotion `_validate` → `evaluate_promotion_risk` | `account_node_unassigned` / `node_not_active` |
| 1 | Per-account exposure limit | promotion `_validate` → `evaluate_promotion_risk` | `account_exposure_exceeded` |
| 2 | Per-symbol exposure limit | promotion | `symbol_exposure_exceeded` |
| 3 | Max open positions / active jobs | promotion | `max_open_positions_reached` |
| 4 | Daily drawdown guard | promotion | `daily_drawdown_hit` |
| 6 | Concurrent-position enforcement | promotion | `concurrent_position_limit` |
| 5 | Runtime staleness re-check | worker `handle_shadow_job` (before `order_check`) | `stale_at_execution` |
| — | Indeterminate risk state | promotion (fail-closed) | `risk_state_indeterminate` |

## Exposure model

Exposure counts **both** order paths on a shared account (per Blueprint doc 06):
real open `Trade` positions (`close_time is null`) **plus** in-flight signal
exposure (leg lots of `PROMOTED` plans). Max-open counts open trades **plus**
active (PENDING/RUNNING) order-opening jobs.

## Audit (control 7)

Every promotion block raises `PromotionRejected(reason)`, which writes a persisted
`PROMOTION_REJECTED` audit with the reason code. The worker staleness block completes
the job `FAILED` with `error=stale_at_execution` and emits the observability
`validation_outcome` + `validation_failure` records. Every block decision is recorded.

## Fail-closed

`evaluate_promotion_risk` wraps all controls; any exception (indeterminate state)
returns `risk_state_indeterminate` → promotion is blocked, never allowed. The worker
staleness check treats an unparseable `signal_timestamp` as stale.

## Configuration (env-overridable defaults)

| Env | Default | Meaning |
|-----|---------|---------|
| `RISK_REQUIRE_TERMINAL_NODE` | **OFF** | When truthy, promotion requires the account to have an operator-declared ACTIVE terminal node. Default OFF: prod accounts currently ride the legacy null-node claim route; enable at E3 only after `manage.py audit_node_assignments --strict` passes. |
| `RISK_MAX_ACCOUNT_EXPOSURE_LOT` | `0.10` | Max total open lots per account |
| `RISK_MAX_SYMBOL_EXPOSURE_LOT` | `MAX_TOTAL_LOT_PER_SIGNAL` (0.06) | Max open lots per account+symbol |
| `RISK_MAX_OPEN_POSITIONS` | `3` | Max open positions + active order jobs |
| `RISK_MAX_DAILY_DRAWDOWN_ABS` | `100.00` | Block if today's realised loss ≥ this |
| `MT5_SIGNAL_MAX_AGE_SECONDS` | `120` | Worker staleness threshold (matches `SIGNAL_MAX_AGE_SECONDS`) |

Defaults are sized so one within-spec signal on a clean account promotes; concurrent
or high-exposure states trip the gates. Tune per account risk appetite before E3.

## What this does NOT do

No `order_send`, no order/ticket/deal, no live Telegram listener, no E3 LIVE mode,
no kill-switch change. The existing shadow pipeline stays operational (a within-limit
fresh promotion still creates its `PLACE_ORDER_SHADOW` jobs; a directly-created
dry-run job without a `signal_timestamp` is unaffected by the staleness re-check).
