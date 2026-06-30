# GFX-PKT-WAYOND-EXEC-D17 — Demo Auto-Execution Flow (design)

> **Design/scoping only.** No code, no orders, no listener. Verified against the
> repository at `main` + the EXEC-E1a / EXEC-HARDEN-JOBS work. Every claim below
> cites code. Notion remains authoritative for lifecycle; this is Git-side
> evidence.

## 0. Headline — REQUIRED #6 compatibility answer

**The order _primitive_ already exists; the _orchestration_ does not.**

The real executor is the Windows MT5 **signal bridge** (`scripts/mt5_signal_bridge.py`,
port 8788), reached via: backend creates a `PLACE_ORDER` `ExecutionJob` →
`backend/mt5_trade_ingest_worker.py` claims it (`GET /api/execution/jobs/next/?job_type=PLACE_ORDER`,
[mt5_trade_ingest_worker.py:47‑50,346‑350](../../backend/mt5_trade_ingest_worker.py)) →
POSTs `/mt5/order` on the bridge → `mt5.order_send(TRADE_ACTION_DEAL)`
([mt5_signal_bridge.py:840‑867](../../scripts/mt5_signal_bridge.py)).

What already works (DEMO, one order):
- **Immediate market order** at live tick — `entry_price` is accepted but ignored
  ("always use market orders", mt5_signal_bridge.py:401‑402).
- **Common SL + one TP** per order — `request["sl"]` / `request["tp"]` (mt5_signal_bridge.py:857‑862).
- **Explicit volume** — `volume=lots` (mt5_signal_bridge.py:846).
- **Broker-level demo-only** — refuses `account_info.trade_mode != 0` (mt5_signal_bridge.py:823‑825).
- **Allowlist / lot cap / slippage** — `DEMO_ORDER_ALLOWED_SYMBOLS=[EURUSD,GBPUSD,XAUUSD]`,
  `DEMO_ORDER_MAX_LOT_SIZE=0.02`, `deviation=20` (~2 pips) (mt5_signal_bridge.py:84‑85,522,798).

What does **not** exist and must be built:
- **1-signal→3-order fan-out.** Every path is strictly one job = one order = one SL =
  one TP. No splitting anywhere (`create_place_order_job` signal_engine.py:1063‑1148;
  `create_open_trade_job` services.py:65‑100 each emit exactly one job).
- **Multi-TP carry-through.** The Wayond parser already extracts *all* TPs into
  `take_profits` (intelligence/telegram_source.py), and intake keeps them
  (`PendingSignalApproval.take_profits` JSONField, signal_intake/models.py:48) — but the
  E1a bridge **drops the list**, copying only the single `approval.take_profit`
  (signal_proposals.py:255; `ProposedSignalOrder.take_profit` is one CharField).
- **A strategy-less order builder.** `create_place_order_job` requires a `Strategy` +
  active `StrategyAssignment` + `can_deploy_automation` entitlement (signal_engine.py:1063‑1085);
  a raw Telegram signal has none. `JobType.PLACE_ORDER` has **no builder in `services.py`**.
- **Per-signal-group caps.** `SIGNAL_MAX_CONCURRENT_POSITIONS=1` (models.py:114) would
  reject legs 2–3 of the same symbol; `count_today_signal_trades` keys on
  `strategy_id`+symbol (models.py:233‑250) and a Telegram path has no `strategy_id`.
- **Approval-less demo intake + a (later) live listener.** Today intake stops at
  `PENDING_APPROVAL` and `approve()` is a human action; input is file/fixture-only.

> The `mt5_worker/mt5_worker.py` named in the original task is a **DEV dummy**
> (`open_mt5_order_dummy` returns fake ticket 123456, no `MetaTrader5` import,
> mt5_worker.py:106‑117) — it is **not** the production placer and must not be the basis.

**Verdict: Needs changes** — order primitive _Compatible_; orchestration + data model
+ caps _Need changes_ (all additive, no change required to the bridge for the per-order
case).

## 1. Revised demo flow (REQUIRED #1)

```
Telegram source (per-source auto-demo ENABLED + demo account selected)
  → parser (intelligence.telegram_source — already multi-TP)
  → source validation (known source/chat; explicitly armed for auto-demo)
  → dedupe (source + chat + message_id — extend the existing uniq_source_message key)
  → risk/symbol checks (allowlist, SL+≥1 TP present, max age, lot/total caps)
  → SPLIT into N demo market-order legs (N = #TPs, capped 3; common SL; one TP/leg)
  → [E1b] non-executable DEMO EXECUTION PLAN  (no ExecutionJob — worker cannot see it)
  → [E2, Nuno-gated] promote plan → N worker-SUPPRESSED PLACE_ORDER jobs (placed = none)
  → [E3, Nuno-gated] enable real DEMO placement via the existing bridge transport
```

Human review is removed **for demo** only when the source is explicitly armed; the
content path is untouched (ADR-009, §7).

## 2. Order behaviour & splitting (REQUIRED #2)

- **Market only.** Reuse the bridge's market execution; `entry_price` recorded for
  audit/voiding but not used to place a pending order.
- **Split rule.** `N = min(len(take_profits), 3)`. Emit **N legs**, each: same `symbol`,
  same `side`, same `sl_price`, **distinct** `tp_price` = TP1…TPN, its own `lots`.
  N legs = N `PLACE_ORDER` jobs (the model is one-order-per-job by design; no batch
  primitive — models.py:168, views.py next_job claims one row).
- **Volume allocation.** `total_lots` configured per source (default `3 × DEMO_FIXED_LOT_SIZE`
  = 0.03, hard-capped). Each leg = `floor(total_lots / N, lot_step)` with the rounding
  remainder added to leg 1 (nearest TP). Invariants: every leg `min_lot ≤ leg ≤
  SIGNAL_MAX_LOT_SIZE (0.02)`, and `Σ legs ≤ MAX_TOTAL_LOT_PER_SIGNAL`. If a leg would fall
  below `min_lot`, reduce N (drop the farthest TP) or **hold** the whole signal.
- **Reject/hold if SL or TP missing.** A signal is tradeable only with `symbol ∧ direction
  ∧ entry ∧ stop_loss ∧ ≥1 take_profit`; otherwise **hold** (audited, no legs created).
  (Parser already requires symbol∧direction∧entry∧SL; this adds the ≥1‑TP requirement.)

## 3. Latency & voiding (REQUIRED #3)

- **Max signal age.** Void if `now − signal_timestamp > MAX_SIGNAL_AGE` (config, default
  120 s). Telegram message date is available to the parser.
- **Stale-price / drift guard.** At execution time the bridge fetches a fresh tick; add a
  rule to **void** if no fresh tick, or if `|current_price − signal_entry| > MAX_ENTRY_DRIFT`
  (config, in pips) — the market moved too far from the quoted entry.
- **Slippage tolerance.** Already supported: the bridge `deviation` field (default 20
  points ≈ 2 pips). Promote it to per-source config; reject fills outside tolerance
  (broker returns a retcode the bridge already inspects, mt5_signal_bridge.py:873‑879).

## 4. Demo-only enforcement (REQUIRED #4)

- **Only demo accounts** — defence in depth at three layers: (a) plan/job creation requires
  `account.is_demo`; (b) the model-layer kill-switch guard already blocks order-opening
  INSERTs when engaged (models.py:205‑217); (c) the bridge refuses `trade_mode != 0`
  (broker truth, mt5_signal_bridge.py:823‑825). **Gap to close:** the bridge's internal
  `execute_mt5_trade` (job path) trusts `payload.is_demo` and does not re-check
  `trade_mode` — add the same broker check to the job path so a misconfigured account
  cannot place live.
- **Source must be explicitly enabled for auto-demo.** New per-source flag
  `auto_demo_execution_enabled` (**default OFF**). No arming → plan stays a proposal; no
  execution.
- **Global kill switch + per-source disable both block.** `ExecutionControl.kill_switch_engaged`
  / `GUVFX_EXECUTION_DISABLED` (model guard) **and** the per-source flag are independent
  fail-closed gates; either off ⇒ no execution.

## 5. Safety controls (REQUIRED #5)

| Control | Source of truth | Change |
|---|---|---|
| Symbol allowlist | `SIGNAL_ALLOWED_SYMBOLS` = EURUSD/GBPUSD/XAUUSD (models.py:109) + bridge allowlist | reuse |
| Max lot / order | `SIGNAL_MAX_LOT_SIZE` = 0.02 (models.py:112) + bridge cap | reuse, enforce per leg |
| Max total lot / signal | — | **new** `MAX_TOTAL_LOT_PER_SIGNAL` |
| Max trades / day | `SIGNAL_MAX_TRADES_PER_DAY` = 10 (models.py:113) | **redefine per signal-group** (a 3-leg signal = 1 group, not 3) |
| Max concurrent | `SIGNAL_MAX_CONCURRENT_POSITIONS` = 1 (models.py:114) | **redefine** as max concurrent signal-groups (legs of one group are allowed) |
| Dedupe | `uniq_source_message (source,message_id)` (signal_intake/models.py:64‑69) | extend key to include `chat_id` |
| Audit chain | `SignalAuditEvent` → `ProposalAuditEvent` (append-only) | **extend**: PLAN_CREATED / LEG_CREATED / PLAN_HELD / PLAN_VOIDED, one row per leg, linked signal→plan→leg |

## 6. Existing payload / worker compatibility (REQUIRED #6)

Covered in §0. Summary table:

| Capability | Today | For demo x3 |
|---|---|---|
| Immediate market order | ✅ bridge (entry_price ignored) | reuse |
| Common SL + one TP / order | ✅ bridge `sl`/`tp` | reuse |
| Explicit per-order volume | ✅ `lots` on PLACE_ORDER (signal_engine) / bridge | reuse the `lots` shape, **not** OPEN_TRADE's risk-% payload |
| Demo-only at broker | ✅ HTTP `/mt5/order` `trade_mode==0` | reuse; add to job path |
| 3 orders from 1 signal | ❌ | **new fan-out** (upstream) |
| Multi-TP carry-through | ❌ dropped at bridge | **new** field/legs model |
| Strategy-less builder | ❌ | **new** PLACE_ORDER-from-signal builder |
| Per-group caps | ❌ per-leg only | **redefine** |

**No change is required to `mt5_signal_bridge.py` for the per-order case** (3 separate
`/mt5/order` calls each place one order with one SL + one TP); the work is the upstream
fan-out, data model, caps, and a strategy-less creator.

## 7. ADR-009 preserved (REQUIRED #7)

The auto-exec path is **execution-side only** (`signal_intake` → `execution`). The content
path (`intelligence` → `wims`) is untouched and never triggers an order; the one-way
boundary (`wims`/`intelligence`/`signal_intake` never import `execution`) remains enforced
by the existing AST/regex CI guard (signal_intake/tests.py `Adr009BoundaryGuardTests`).
WIMS never trades.

## 8. Governance gate (read before any executing packet)

Removing human review and promoting a signal into **real orders** — even demo — crosses
the GuvFX human-gated boundary ("No unrestricted LLM live-trading authority… must never
place, size, or approve live or paper orders without an explicit human-gated control
path", CLAUDE.md overlay; `.claude/rules/architecture`). Auto-demo execution is therefore
an **Amber→Red** change requiring Nuno's explicit, recorded sign-off **before** the
executing packet (E2+) proceeds. This design does not advance lifecycle state.

---

## First implementation packet (REQUIRED #8) — places NO live-account orders

### GFX-PKT-WAYOND-EXEC-E1b — Multi-leg demo execution PLAN (no order, no ExecutionJob)

**Objective.** Build the entire new orchestration brain — TP carry-through, deterministic
split, volume allocation, latency/voiding, per-source arming, per-group caps, audit — as a
**non-executable DEMO EXECUTION PLAN**, structurally invisible to the worker (like
`ProposedSignalOrder`). Places **no** order (demo or live), creates **no** `ExecutionJob`,
starts **no** listener.

**In scope.**
1. Carry `take_profits` through intake → plan (stop dropping it). Add a child
   `ProposedOrderLeg` model (`plan` FK, `leg_index`, `tp_price`, `lot_size`, shared
   `sl_price`) — one plan → N legs. (Relaxes/avoids the E1a `OneToOne(approval)` for the
   multi-leg case.)
2. Deterministic `split_signal_into_legs(signal, config)` → N legs (N = min(#TP, 3)),
   common SL, one TP/leg, volume allocation rule (§2). Pure function, fully unit-tested.
3. Validators (pure, fixture-tested, **no live data**): SL∧≥1 TP present; max signal age;
   entry-drift/stale guard; symbol allowlist; per-leg + per-signal lot caps; per-signal-group
   daily/concurrent caps.
4. Per-source `auto_demo_execution_enabled` flag (**default OFF**) + demo-account binding.
5. Audit chain extension: PLAN_CREATED / LEG_CREATED / PLAN_HELD / PLAN_VOIDED with reason
   codes, linked signal→plan→leg.
6. Admin/read surface for plans + legs (read-mostly; no executing action).

**Out of scope / prohibited (this packet).** No `ExecutionJob` creation; no
`order_send`/MT5/Windows-agent call; no broker credentials; no live Telegram listener; no
live account; no production access/deploy/migration. (E2 = promote a demo plan to
**worker-suppressed** PLACE_ORDER jobs; E3 = real demo placement — both separate and
Nuno-gated.)

**Acceptance.** Tests prove: `ExecutionJob.objects.count()` unchanged; a multi-TP signal
yields N legs with correct common SL, distinct TPs, and a valid volume split; missing
SL/TP ⇒ HELD (no legs); stale/old signal ⇒ VOIDED; source disabled ⇒ no plan; kill switch
engaged ⇒ blocked; ADR-009 boundary guard still green. CI green.

### Subsequent ladder (for reference, each separately Nuno-gated)
- **E2** — promote a reviewed demo plan to N **worker-suppressed** `PLACE_ORDER` jobs
  (dry-run flag → bridge runs `order_check`, skips `order_send`; logs intended order,
  places none).
- **E3** — enable real **demo** placement through the existing bridge transport, demo-only
  enforced at all three layers; latency/slippage live.
- **E4** — live accounts: out of scope for the foreseeable future; full governance,
  Blueprint-06 ratification, broker-timezone verification, and explicit live sign-off.
