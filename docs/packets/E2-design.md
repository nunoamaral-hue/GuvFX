# GFX-PKT-WAYOND-EXEC-E2 — Worker-Suppressed Demo Job Promotion (design)

> **Design/scoping only.** No code, no orders, no jobs, no listener. Verified
> against `main` (E1a/HARDEN/R2/E1b/E1b-R2 all merged). Every claim cites code.
> **E2 implementation is RED** — it is the first rung that creates real,
> worker-claimable `ExecutionJob` records, so it requires Nuno's explicit recorded
> sign-off (see §12). This document advances no lifecycle state.

## 0. Grounding (verified)

| Fact | Evidence |
|---|---|
| Worker claims only `PLACE_TEST_ORDER`, then `PLACE_ORDER`, then default | `backend/mt5_trade_ingest_worker.py:347‑350` |
| `next_job` with no `job_type` defaults to **SYNC_POSITIONS only** | `backend/execution/views.py` next_job ("If omitted, defaults to SYNC_POSITIONS only") |
| Worker builds `/mt5/order` payload and calls `agent_order` (→ `order_send`) | `mt5_trade_ingest_worker.py:386‑402,82‑84` |
| Bridge `/mt5/order` builds a `TRADE_ACTION_DEAL` request and calls `mt5.order_send` | `scripts/mt5_signal_bridge.py:843‑867` |
| Bridge **already uses `mt5.order_check`** (stop-clamp loop) before `order_send` | `scripts/mt5_signal_bridge.py:530` |
| Bridge enforces broker demo-only (`trade_mode != 0` reject) | `scripts/mt5_signal_bridge.py:823‑825` |
| Model kill-switch guard blocks order-opening `ExecutionJob` INSERT | `backend/execution/models.py` `ExecutionJob.save` + `KILL_SWITCH_BLOCKED_JOB_TYPES` |
| `mt5.order_check(request)` validates margin/retcode **without placing** the order | MT5 API; bridge uses it at :530 |

**Two decisive levers** the design exploits:
1. A **distinct shadow job_type is structurally un-claimable** by the deployed
   worker (it never asks for it), so backend promotion can create records that
   the live order path can never reach — no worker/bridge change, no deploy.
2. `mt5.order_check` is the natural **dry-run primitive**: everything `order_send`
   does (validation, margin, retcode) **except** placing the order.

## 1. Promotion flow (REQUIRED 1)

```
SignalExecutionPlan(status=PLANNED, N legs)
  → [operator/gated] promote_plan(plan)
      → re-validate (mode SHADOW, kill switch, demo, source armed, symbol/lots/SL/TP, staleness)
      → for each ProposedOrderLeg: create ONE suppressed job (job_type=PLACE_ORDER_SHADOW)
      → link leg.execution_job; leg.status=PROMOTED; plan.status=PROMOTED
      → audit PROMOTION_CREATED + JOB_CREATED (per leg)
  → [E2b, gated] shadow worker claims PLACE_ORDER_SHADOW → bridge order_check ONLY → no order_send
```

Promotion is human-driven (operator command / admin action), never automatic.

## 2. Suppression mechanism (REQUIRED 2) — defence in depth, every layer fail-closed

Four independent layers; **any one** prevents an order:

1. **Distinct job_type `PLACE_ORDER_SHADOW`** (new `ExecutionJob.JobType` value).
   The deployed worker's claim calls are `PLACE_TEST_ORDER` → `PLACE_ORDER` →
   default(`SYNC_POSITIONS`) (`mt5_trade_ingest_worker.py:347‑350`); none request
   `PLACE_ORDER_SHADOW`, so the **live worker can never claim it** — structural.
2. **Job payload `execution_mode = "SHADOW"`** — stamped by the promotion service
   (sole creator). The E2b shadow worker reads it; an absent/unrecognised mode on
   a signal job is **fail-closed** (the worker fails the job, never places).
3. **Worker (E2b) routes SHADOW to a dry-run call** — it does **not** invoke the
   live `agent_order`/`order_send` path. It POSTs `/mt5/order` with `dry_run: true`
   (or a dedicated `/mt5/order_check` endpoint) and reports the job as a suppressed
   completion.
4. **Bridge (E2b) on `dry_run: true` runs `mt5.order_check` and RETURNS** —
   `mt5.order_send` (`mt5_signal_bridge.py:867`) is never reached. The bridge
   already runs `order_check` at :530, so this is a guarded early-return, not new
   broker logic.

Plus a **global gate**: `ExecutionControl.signal_execution_mode` (default
`SHADOW`); promotion refuses to create a job in any other mode. `LIVE` is not a
defined value in E2 — it is a separate, far-future, separately-gated packet.

## 3. Plan-leg → job mapping (REQUIRED 3)

One `ProposedOrderLeg` → exactly one job (`OneToOne` link `leg.execution_job`).
Payload (market-only, demo, suppressed):

```json
{ "symbol": "<plan.symbol>", "side": "<plan.direction>",
  "lots": "<leg.lot_size>", "sl_price": "<plan.stop_loss>",
  "tp_price": "<leg.take_profit>", "entry_price": null,
  "magic": <derived>, "is_demo": true, "execution_mode": "SHADOW",
  "comment": "WAY<plan_id>L<leg_index>", "windows_username": "<account.mt5_instance>" }
```

This reuses the proven PLACE_ORDER payload shape (`signal_engine.create_place_order_job`)
so the bridge's existing field handling applies unchanged — only the job_type and
`execution_mode`/`dry_run` differ.

## 4. Status transitions (REQUIRED 4)

| Entity | Before | After promotion | After suppressed worker run (E2b) |
|---|---|---|---|
| `SignalExecutionPlan` | `PLANNED` | `PROMOTED` (or `PROMOTION_HELD` on validation fail) | unchanged |
| `ProposedOrderLeg` | `PLANNED` | `PROMOTED` (+ `execution_job` set) | `SHADOW_FILLED` / `SHADOW_REJECTED` (from `order_check` retcode) |
| `ExecutionJob` | — | `PENDING` (`PLACE_ORDER_SHADOW`) | `SUCCESS` (`{suppressed:true, dry_run_result}`) or `FAILED` |

No status anywhere represents a placed order; the job's terminal `SUCCESS`
carries `suppressed: true` and the `order_check` result, never a ticket from
`order_send`.

## 5. Idempotency / duplicate-promotion prevention (REQUIRED 5)

- A plan must be `PLANNED` to promote; a `PROMOTED` plan is not re-promoted.
- `OneToOne(leg → execution_job)` ⇒ one job per leg; a re-promotion returns the
  existing jobs and creates none (mirrors E1a/E1b idempotency; `IntegrityError`
  fallback).
- Optional belt: a unique payload `comment` tag (`WAY<plan>L<leg>`) so even at the
  broker layer a duplicate is identifiable.

## 6. Demo-only + source enablement (REQUIRED 6)

Re-checked at promotion time (the plan may be old): `account.is_demo` true and
broker `environment != live`; `SignalSourceConfig.auto_demo_execution_enabled`
still true. The bridge independently re-enforces `trade_mode != 0` even in dry-run
(`order_check` on a non-demo account is refused), so demo-only holds at three
layers.

## 7. Kill-switch behaviour (REQUIRED 7)

`promote_plan` calls `order_creation_kill_reason()` first and fails closed; the
model `save()` guard blocks `PLACE_ORDER_SHADOW` creation when the switch is
engaged (extend `KILL_SWITCH_BLOCKED_JOB_TYPES` to include it); the E2b worker
re-checks the kill switch before any bridge call and fails the job if engaged. A
kill engaged mid-flight ⇒ no order_check, no order.

## 8. Validation at promotion time (REQUIRED 8)

Re-apply the E1b gates against current state: symbol ∈ `SIGNAL_ALLOWED_SYMBOLS`;
each leg `lot_size ≤ SIGNAL_MAX_LOT_SIZE` and `Σ ≤ MAX_TOTAL_LOT_PER_SIGNAL`; SL
present; each leg TP present; **staleness re-checked** (`now − signal_timestamp >
SIGNAL_MAX_AGE_SECONDS` ⇒ refuse/hold — a plan that aged out since planning is not
promoted). Failures hold the plan (`PROMOTION_HELD`) and create no jobs.

## 9. Audit chain (REQUIRED 9)

```
signal_intake.SignalAuditEvent : SIGNAL_RECEIVED → APPROVED
execution.PlanAuditEvent       : PLAN_CREATED → LEG_CREATED
execution.PromotionAuditEvent  : PROMOTION_CREATED → JOB_CREATED (per leg) / PROMOTION_REJECTED   [new]
core.AuditEvent (worker)       : EXECUTION_JOB_CLAIMED → EXECUTION_JOB_COMPLETED (suppressed=true) [E2b]
ProposedOrderLeg               : dry_run_result recorded (order_check retcode/margin)             [E2b]
```

Full traceability: signal → plan → leg → suppressed job → dry-run result.

## 10. No-order tests (REQUIRED 10)

- Every promoted job is `PLACE_ORDER_SHADOW` (never `PLACE_ORDER`); payload carries
  `execution_mode=SHADOW`.
- The live worker claim queryset (`next_job` for `PLACE_TEST_ORDER`/`PLACE_ORDER`/
  default) returns **no** `PLACE_ORDER_SHADOW` job — un-claimable.
- E2b worker test (mocked): given a SHADOW job, `agent_order` (live) is **never**
  called; the dry-run call is used; an unknown mode → job FAILED, no call.
- E2b bridge test (mocked `mt5`): `/mt5/order` with `dry_run:true` calls
  `mt5.order_check` and **`mt5.order_send` is asserted called 0 times**.
- Promotion refuses when mode≠SHADOW / kill switch / non-demo / not armed / stale /
  caps; idempotent re-promotion creates no new jobs.
- A static guard that the promotion module performs no `order_send`/MT5/network
  call (AST), as in E1a/E1b.

## 11. Worker / bridge changes required (REQUIRED 11)

**Yes — for the full E2 (E2b), both change.** But the design **sequences them so
the first packet needs neither** (see below):

- **E2a (backend only):** new `PLACE_ORDER_SHADOW` job_type, promotion service,
  job link, `signal_execution_mode`, audit, tests. The live worker/bridge are
  **untouched and un-deployed**; the jobs are structurally un-claimable. No order
  is possible because no code path consumes a `PLACE_ORDER_SHADOW` job.
- **E2b (worker + bridge):** the shadow worker branch (claim `PLACE_ORDER_SHADOW`
  → dry-run) and the bridge `dry_run` early-return (`order_check` only). These run
  on the **production MT5 box** — a deployment-gated, separately-signed-off change.

## 12. Governance gates before E2 implementation (REQUIRED 12)

1. **Nuno's explicit, recorded sign-off** to remove human review for demo
   auto-execution (the D17 Amber→Red gate) — E2 creates real `ExecutionJob`s.
2. **Blueprint-06 reconciliation** of the live order path with the target
   execution architecture (`docs/KNOWN_ISSUES.md`, `docs/LIVE_TRADING_RISK_WATCH.md`).
3. **Broker-server timezone verification** — gates real timing/staleness semantics.
4. **Deployment gate for E2b:** the worker/bridge changes touch the live trading
   transport (GREEN Trading domain) on the production MT5 box; they must not be
   deployed until the suppression is proven (order_send-zero tests) and signed off.
5. **Review gate:** an adversarial no-order review (as for E1a/E1b) before each merge.

---

## First implementation packet (REQUIRED) — creates NO order, NOT live-claimable

### GFX-PKT-WAYOND-EXEC-E2a — Plan → suppressed (un-claimable) shadow job records

**Objective.** Backend-only promotion of a `PLANNED` plan into one
`PLACE_ORDER_SHADOW` `ExecutionJob` per leg — a real job record that the deployed
worker **cannot claim** (distinct job_type) and that no code consumes yet. Places
no order; touches no worker/bridge; needs no deployment.

**In scope.**
1. `ExecutionJob.JobType.PLACE_ORDER_SHADOW`; add it to
   `KILL_SWITCH_BLOCKED_JOB_TYPES`.
2. `ExecutionControl.signal_execution_mode` (default `SHADOW`); promotion refuses
   any other mode.
3. `ProposedOrderLeg.execution_job` (OneToOne, nullable) + `PROMOTED` statuses on
   plan/leg; `PromotionAuditEvent` (append-only).
4. `execution.signal_promotion.promote_plan(plan)` — re-validates (mode, kill
   switch, demo, source armed, symbol/lots/SL/TP, staleness), creates one shadow
   job per leg with `execution_mode=SHADOW`, links legs, audits. Idempotent.
5. Operator entry: `manage.py promote_plan` (no automation); admin read-only.
6. Tests: every promoted job is `PLACE_ORDER_SHADOW` with `execution_mode=SHADOW`;
   the live worker claim queryset cannot see it; `order_send` is unreachable (no
   consumer exists); re-promotion is idempotent; all gates reject correctly; AST
   no-order guard.

**Out of scope / prohibited (this packet).** No worker change, no bridge change,
no deployment, no `mt5.order_check`/`order_send` path, no live account, no
credential, no listener. (E2b — the shadow worker + bridge dry-run handler — is a
separate, deployment-gated, separately-signed-off packet.)

**Acceptance.** A promoted plan yields N `PLACE_ORDER_SHADOW` jobs that no deployed
worker claims and no code executes; `order_send` is provably unreachable; the
chain signal→plan→leg→job is audited; CI green.

### Subsequent rungs (each separately gated)
- **E2b** — shadow worker branch + bridge `dry_run` (`order_check` only,
  `order_send` asserted 0×); deployment-gated.
- **E3** — real **demo** placement (remove suppression on demo only); full
  governance.
- **E4** — live accounts: out of scope for the foreseeable future.
