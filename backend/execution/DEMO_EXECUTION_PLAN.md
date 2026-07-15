# EXEC-E1b — multi-leg demo execution PLAN (NO order is ever placed)

The execution-side layer that turns an **APPROVED**
`signal_intake.PendingSignalApproval` into a **non-executable**
`SignalExecutionPlan` plus up to three `ProposedOrderLeg` children — the
representation of how the signal *would* be split into demo market orders,
without placing any. It is the next gated rung after E1a, and still creates **no
order and no `ExecutionJob`**.

```
Wayond Telegram ─▶ signal_intake ─▶ PendingSignalApproval ─(human approve)─▶
   execution.signal_planning.plan_demo_execution ─▶ SignalExecutionPlan + N×ProposedOrderLeg
                                                    (candidate; NOT an ExecutionJob)
```

## Why "no order" is structural

The MT5 worker claims work via `ExecutionJob.objects.filter(status=PENDING)`
(see `execution.views` → `next_job`). A `SignalExecutionPlan`/`ProposedOrderLeg`
is a **different model** with **no PENDING status and no worker-claim path**, so
the worker never sees it. Tests assert `ExecutionJob.objects.count()` is
unchanged and that the planner module references no `ExecutionJob`,
`create_place_order_job`, `create_open_trade_job`, `order_send`, MT5, or any
network client (AST guard). Promotion of a plan to executable (worker-suppressed)
jobs is a separate, sponsor-gated packet (E2+).

## Order behaviour

- **Market-only.** `order_type="MARKET"` on the plan and every leg.
- **Up to 3 legs**, one per take-profit (`N = min(len(take_profits), 3)`),
  **sharing one common stop-loss**, each carrying a **distinct TP** (TP1…TPN).
- **Volume split** (deterministic): the per-source `total_lot_target` is capped to
  `min(total, N × SIGNAL_MAX_LOT_SIZE, MAX_TOTAL_LOT_PER_SIGNAL)`, split equally
  in broker lot-steps (`0.01`), with the rounding remainder assigned to the
  earliest leg(s) (leg 1 first). Every leg stays within `SIGNAL_MAX_LOT_SIZE`
  (0.02) and the sum within `MAX_TOTAL_LOT_PER_SIGNAL` (0.06).

## Outcomes

| Outcome | Status | Legs | Cause |
|---------|--------|------|-------|
| Planned | `PLANNED` | 1–3 | all checks pass |
| Held | `HELD` | 0 | missing SL, missing TP, or volume split impossible |
| Voided | `VOIDED` | 0 | stale signal (age > `SIGNAL_MAX_AGE_SECONDS`) |
| Rejected (no plan) | — (raises `PlanRejected`) | — | not approved, kill switch, source not armed, non-demo/live account, symbol not allowed, per-group cap |

Every outcome writes append-only `PlanAuditEvent` rows
(`PLAN_CREATED`/`PLAN_HELD`/`PLAN_VOIDED` + `LEG_CREATED`), linked to the
originating approval — extending the `signal_intake.SignalAuditEvent` chain.
Rejection audits persist even though no plan is created.

## Safety gates

- **Demo-only** — `account.is_demo` required; live broker `environment` rejected.
- **Source arming** — a `SignalSourceConfig` row must exist with
  `auto_demo_execution_enabled = True` (**default OFF**); otherwise rejected.
- **Kill switch** — `ExecutionControl.kill_switch_engaged` / `GUVFX_EXECUTION_DISABLED`
  fail closed (shared `order_creation_kill_reason`).
- **Symbol allowlist** — `SIGNAL_ALLOWED_SYMBOLS` (EURUSD/GBPUSD/XAUUSD).
- **Per-signal-group caps** — `PLAN_MAX_GROUPS_PER_DAY` / `PLAN_MAX_CONCURRENT_GROUPS`
  count **plans (groups)**, not legs. The daily cap (default **24**, env-tunable) is
  **per-SOURCE**: each provider (e.g. `wayond`, `ti_signals`) has an independent daily
  budget over acted-on groups (PLANNED/PROMOTED/CLOSED), so one source can never consume
  another's allowance. The concurrency cap stays per account+symbol across sources.
- **Idempotency** — one plan per approval (`OneToOne`) and per
  `(source, chat_id, message_id)`; a duplicate returns the existing plan.

## Models

- `SignalSourceConfig` — per-source arming flag (default OFF) + total lot target.
- `SignalExecutionPlan` — the signal group (non-executable).
- `ProposedOrderLeg` — one leg per TP (non-executable).
- `PlanAuditEvent` — append-only plan/leg audit.

## Operator entry point (no listener, no automation)

```bash
cd backend
python manage.py plan_demo_execution --enable-source WAYOND_TELEGRAM --total-lot 0.03
python manage.py plan_demo_execution --approval <id> --account <demo_id>
```

There is **no** automatic approval→plan trigger and **no** web endpoint that
creates plans; the admin add forms for plans/legs are disabled, so the
safety-gated planner is the only writer.

## Next (gated, NOT in this packet)

E2: promote a `PLANNED` plan to N **worker-suppressed** `PLACE_ORDER` jobs
(dry-run — `order_check`, no `order_send`; logs intended order, places none).
E3: real demo placement. Each a separate, sponsor-gated escalation behind the
human-gated governance sign-off.
