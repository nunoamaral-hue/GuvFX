# Production Regression — Orphaned PLANNED Plan Concurrency Leak

**Independent workstream** (not Customer Zero / Phase A/B / Golden / Beta).
**STATUS: RESOLVED 2026-07-31** — fix (PR #247 `04c6656`) deployed to prod (backend `d5461b63`, listener
`731528ab`); the 10 orphans reclaimed `PLANNED→VOIDED` (`count_active` 10→0); recovery **live-proven** by
signal 165 (plan 145 PROMOTED → MT5 orders 230672–230674 → Trades 419–421 → breakeven). Full post-incident
review: **`docs/PIR_ORPHANED_PLANNED_PLAN_CONCURRENCY_LEAK.md`**. The design/implementation detail below is
retained as the engineering record.

## Root cause (accepted programme fact)
`count_active` counts `SignalExecutionPlan`s in status `PLANNED` only (`execution/models.py:809-813`); the
planning gate rejects new signals once `count_active >= PLAN_MAX_CONCURRENT_GROUPS` (=10)
(`signal_planning.py:242`). A plan whose promotion is rejected (e.g. `daily_drawdown_hit`) — or that never
promotes — is left in `PLANNED` forever (`auto_router._plan_and_promote_one` recorded a deferral but never
transitioned the plan). 10 such orphans accumulated for prod acct #1 / XAUUSD (2026-07-15 → 07-30); the 10th
(plan #144, correctly promotion-rejected on drawdown at 2026-07-30 23:03) saturated the gate, and since
2026-07-31 02:06 UTC every Telegram signal is rejected `concurrent_limit_exceeded`. The drawdown gate itself
behaved correctly; the lifecycle around `PromotionRejected` did not.

## Fix (Parts A + B + C, + monitoring, + W1–W5)
- **Part A — recovery tooling.** `manage.py reclaim_orphaned_planned_plans` — DRY-RUN by default,
  `--apply` performs a compare-and-set `PLANNED→VOIDED`, fully audited (`PLAN_VOIDED`), `--account/--symbol/
  --source/--older-than-seconds/--limit` scoping. Places NO order.
  (`execution/management/commands/reclaim_orphaned_planned_plans.py` + core in `execution_health.py`.)
- **Part B — root-cause correction.** On `PromotionRejected`, the auto_router now transitions the just-created
  plan `PLANNED→VOIDED` (`_void_rejected_plan`, guarded to `PromotionRejected` only — W2). Prevents FUTURE
  accumulation with no flag and without touching existing rows. (`execution/auto_router.py`.)
- **Part C — permanent self-healing.** `reclaim_orphaned_planned_plans` is wired into the monitor chain via
  `sweep_execution_health`. MUTATING, so inert unless `ORPHANED_PLANNED_RECLAIM_ENABLED` (default OFF) — the
  same posture as the other mutating monitor-chain behaviours (`BREAKEVEN_ENABLED`, `PROVIDER_COMMANDS_ENABLED`).
  This honours the controlled-migration gate: deploying the fix must not auto-reclaim the existing backlog
  before the operator reviews a dry-run. (`execution/execution_health.py`, `run_monitor_chain.py`.)
- **Monitoring.** New `detect_saturated_concurrency_gates` raises a deduped WARN/CRITICAL AlertEvent when a
  tradeable account+symbol approaches/hits the cap (auto-resolves on recovery); a `concurrency` block in the
  ops summary reports per-account/symbol PLANNED utilisation + the orphan-age distribution; the monitor-chain
  summary line surfaces `planned_reclaimed`/`concurrency_saturation_alerted`. Reject-reason counts already
  exist in `signal_execution`. Objective: detect this defect class BEFORE trading blocks.

### Refinements (adversarial-review findings W1–W5, implemented)
- **W1** — the reclaim age MUST exceed `SIGNAL_MAX_AGE_SECONDS` (120s); the core function raises `ValueError`
  on any smaller value (the command surfaces it as `CommandError`). Only un-promotable plans are ever voided.
- **W2** — the transition-on-reject fires only on `PromotionRejected` (`plan` is bound); the `PlanRejected`
  branch (plan possibly unbound) is untouched.
- **W3** — the monitor-chain self-heal is wired in mandatorily (prevents future accumulation once enabled);
  Part B additionally prevents accumulation with no flag.
- **W4** — see "Behavioural change" below.
- **W5** — reclaim ages by `created_at`, never `signal_timestamp` (a bogus future-dated signal_timestamp can't
  make a plan un-reapable).

## Safety
The only plan→order path (`signal_promotion._promote_plan`→`_validate`) re-reads `status == PLANNED` AND
re-checks signal age ≤ `SIGNAL_MAX_AGE_SECONDS` before creating any job — so a voided/old plan can **never**
produce an order, and the reclaim only ever runs compare-and-set `PLANNED→VOIDED` (race-safe; a concurrently
promoted plan matches zero rows), idempotent, no child jobs.

## Behavioural change (W4 — document explicitly)
Voiding a rejected/orphaned plan (`PLANNED→VOIDED`) removes it from:
- **Concurrency accounting** — `count_active` counts PLANNED-only, so a voided plan no longer holds a slot.
  This is the fix's intent.
- **Daily accepted-volume count** — `SignalExecutionPlan.count_today` counts PLANNED/PROMOTED/CLOSED and
  excludes VOIDED, so a rejected plan no longer counts toward the per-source `daily_group_cap`.
- **Ops dashboard buckets** — a rejected plan moves from "accepted/pending" to "rejected" in
  `_signal_execution_block`.

This is **more correct** (a rejected plan placed no order, so it should not consume a daily slot) and only
*loosens* a reject-if-high volume limiter — it can never drop a signal or place an order. Expected and intended.

## Tests
`execution/tests_orphaned_planned_reclaim.py` (20 tests): PLANNED→VOIDED transition, concurrency release,
compare-and-set no-op on concurrent promotion, W1 threshold enforcement, W5 created_at basis,
non-PLANNED-untouched, idempotency, gated monitor-chain self-heal (off/on), saturation alert raise+resolve,
Part B transition-on-reject (+ deferral still recorded), the `PlanRejected` branch never voids/crashes, and
the command (dry-run/apply/invalid-threshold). Full execution+reliability suite green (670 tests).

## Deployment (NOT authorised in this phase — for the future Production Repair Deployment)
1. Verified prod DB backup.
2. Deploy backend AND rebuild the `wayond-listener` image (Part B runs in the listener process).
3. Run `reclaim_orphaned_planned_plans --account 1 --symbol XAUUSD` (DRY-RUN) → present the report.
4. (separate Sponsor authorisation) Run with `--apply` → the 10 orphans void → `count_active` 10→0 → trading
   resumes.
5. (optional, later) Set `ORPHANED_PLANNED_RECLAIM_ENABLED=1` for the permanent self-heal backstop.
Rollback = revert the deploy (voided plans stay voided — correct).
