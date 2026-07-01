# EXEC-E2a — plan → suppressed, un-claimable shadow jobs (NO order is placed)

Backend-only promotion of a `PLANNED` `SignalExecutionPlan` into one
`PLACE_ORDER_SHADOW` `ExecutionJob` per leg. The jobs are **real** records but
**suppressed and un-claimable**: nothing executes them, so no order is placed.
This is the first rung that creates `ExecutionJob` records; it touches **no** MT5,
worker, or bridge code.

```
SignalExecutionPlan(PLANNED, N legs)
  → execution.signal_promotion.promote_plan_to_shadow_jobs(plan)   [operator/gated]
      → re-validate (mode SHADOW, kill switch, demo, source armed, symbol, lots, SL, TP, staleness)
      → one PLACE_ORDER_SHADOW ExecutionJob per leg (execution_mode=SHADOW), link leg.execution_job
      → plan/legs → PROMOTED; audit PROMOTION_CREATED + JOB_CREATED
  → [E2b, deployment-gated] shadow worker → bridge order_check ONLY → no order_send
```

## Why "no order" holds — three independent suppression layers

1. **`execution_mode=SHADOW`** in the payload — a fail-closed flag the future E2b
   shadow worker must honour.
2. **No deployed consumer requests `PLACE_ORDER_SHADOW`** — all four claimers
   (ingest worker, signal-bridge poller, demo bridge, dummy worker) request only
   `PLACE_TEST_ORDER`/`PLACE_ORDER`/`SYNC_POSITIONS`.
3. **`next_job` endpoint guard** — `next_job` excludes `PLACE_ORDER_SHADOW` unless
   the caller carries an explicit `worker_permissions.shadow_worker` flag. So even
   a worker (or staff) that explicitly requests `?job_type=PLACE_ORDER_SHADOW` is
   served nothing. No `WorkerIdentity` has that permission today, so no shadow job
   is ever served.

Promotion creates **only** `PLACE_ORDER_SHADOW` jobs — never an executable
`PLACE_ORDER`/`OPEN_TRADE`/`PLACE_TEST_ORDER` — and the module makes **no** MT5 /
`order_send` / `order_check` / Windows-agent / network call (AST static guard).
The kill switch also blocks shadow-job creation (belt-and-braces).

## Gates (re-validated at promotion time — the plan may be old)

`plan.status == PLANNED` · global `ExecutionControl.signal_execution_mode ==
SHADOW` · kill switch / `GUVFX_EXECUTION_DISABLED` · `SignalSourceConfig`
armed · `account.is_demo` + live-broker-env reject · symbol allowlist · SL
present · each leg TP present · per-leg `lot ≤ SIGNAL_MAX_LOT_SIZE` and total `≤
MAX_TOTAL_LOT_PER_SIGNAL` · staleness (`now − signal_timestamp ≤
SIGNAL_MAX_AGE_SECONDS`). Failures raise `PromotionRejected` and write a
persisted `PROMOTION_REJECTED` audit; no jobs are created.

## Models / fields added

- `ExecutionJob.JobType.PLACE_ORDER_SHADOW` (+ in `KILL_SWITCH_BLOCKED_JOB_TYPES`).
- `ExecutionControl.signal_execution_mode` (default `SHADOW`; only valid value).
- `ProposedOrderLeg.execution_job` (OneToOne → the shadow job) + `PROMOTED` status.
- `SignalExecutionPlan.PROMOTED` status.
- `PromotionAuditEvent` (append-only: PROMOTION_CREATED / JOB_CREATED / PROMOTION_REJECTED).

## Idempotency

One job per leg (`OneToOne(leg → execution_job)`); a `PROMOTED` plan returns its
existing shadow jobs and creates none.

## Operator entry (no automation, no listener)

```bash
cd backend
python manage.py promote_plan_to_shadow --plan <id>
```

Admin is read-only for plans/legs/jobs; only `SignalSourceConfig` (arming) is
editable. There is no automatic promotion trigger.

## Next (gated, NOT in this packet)

**E2b** — the shadow worker branch (claim `PLACE_ORDER_SHADOW`) + the bridge
`dry_run` path (which must **add** `mt5.order_check` to `execute_demo_order` after
the `trade_mode != 0` check and before `order_send`, asserted called 0×). These
run on the production MT5 box — a separate, deployment-gated, sponsor-gated packet.
**E3** — real demo placement. Both behind Nuno's recorded sign-off.
