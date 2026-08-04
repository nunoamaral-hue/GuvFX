# WP6-D — Execution Safety Certification

Prove that **no ineligible account can open exposure**, at every enforcement point and across **every route
in the authoritative inventory**. Matrix cases: `EXE-1..10`. Safety-critical gate: `GATE-D`.

The authoritative inventory is **`backend/execution/execution_entrypoints.json`** (the WSE artefact). The
WP6 validation test cross-checks that every `opens_exposure=true` route in that file is (a) declared in the
matrix `route_coverage` and (b) covered by an `EXE-*` case.

## The three authoritative enforcement points

1. **Creation gate** — `execution/models.py::ExecutionJob.save` runs `require_execution_gate` +
   `require_not_broker_paused` for `BROKER_GATE_BLOCKED_JOB_TYPES` (`OPEN_TRADE`, `PLACE_ORDER`,
   `PLACE_TEST_ORDER`). The single choke point every `ExecutionJob.objects.create` passes through.
2. **Claim gate** — `execution/views.py::ExecutionJobViewSet.next_job` re-runs `evaluate_dispatch_gate`
   under the row lock; a refused exposure-opening job is set FAILED and never handed out (closes the
   alternate-transport/host-bridge bypass).
3. **Final-dispatch gate** — `mt5_trade_ingest_worker.py::evaluate_job_dispatch` re-evaluates FRESH
   eligibility + WP3 health immediately before `order_send`; refusal withholds the order.

All three are **transparent when `BROKER_CONNECTIVITY_EXECUTION_GATE` is OFF** (short-circuit before any DB
read). Health-driven refusal + pause/resume additionally require `BROKER_CONNECTIVITY_HEALTH_ENABLED`.

## Definitive route inventory (all 15 exposure-opening routes — every one must be certified)

| Route (`file::function`) | Covered by |
|--------------------------|-----------|
| `execution/models.py::ExecutionJob.save` | EXE-1 |
| `execution/services.py::create_open_trade_job` | EXE-1 |
| `execution/views.py::CreateOpenTradeJobView.post` | EXE-1 |
| `strategies/signal_engine.py::create_place_order_job` | EXE-1 |
| `execution/views.py::ExecutionJobViewSet.next_job` | EXE-2 |
| `scripts/mt5_signal_bridge.py::fetch_next_job -> order_send` | EXE-2 (upstream claim gate) |
| `scripts/mt5_demo_bridge.py::fetch_next_job -> order_send` | EXE-2 (upstream claim gate) |
| `mt5_trade_ingest_worker.py::main loop dispatch (evaluate_job_dispatch -> agent_order)` | EXE-3 |
| `execution/views.py::CreateDemoTradeJobView.post` | EXE-7 |
| `strategies/management/commands/run_h1_scheduler.py::handle (_evaluate_live)` | EXE-8 |
| `strategies/management/commands/run_m5_scheduler.py::handle (_evaluate_live)` | EXE-8 |
| `strategies/management/commands/run_h4_scheduler.py::handle (main loop)` | EXE-8 |
| `execution/signal_promotion.py::_promote_plan / _validate` | EXE-9 |
| `execution/auto_router.py::route_acquired_signal / _plan_and_promote_one` | EXE-9 |
| `admin_ops/views.py::AdminExecutionJobViewSet.retry` | EXE-10 |

Non-opening routes (`NON_OPENING_EXEMPT`/`DEAD_UNREACHABLE`) — pause/resume, breakeven/protection SYNC/MODIFY/
CLOSE, provisioning START/RECOVER, arming activation, orphan reclaim — are documented as exempt in the
inventory (they open no exposure); pause/resume are still certified under EXE-4/EXE-5 for their own contract.

## Certification cases

| Case | Certifies | PASS criteria | Repo evidence |
|------|-----------|---------------|---------------|
| EXE-1 | Creation gate (all 4 create funnels) | No ineligible exposure-opening job created; `EXECUTION_GATE_REFUSED` audited + projected | `execution/tests_broker_gate.py`, `tests_wse.py` |
| EXE-2 | Claim gate (`next_job` + both host bridges upstream) | No claimer receives an ineligible exposure-opening job; FAILED under the lock | `tests_dispatch_gate.py`, `tests_wse.py`, `tests_killswitch_claim.py` |
| EXE-3 | Final-dispatch TOCTOU recheck | No `order_send` for a now-ineligible account | `tests_dispatch_gate.py` |
| EXE-4 | Pause (creation-time block) | No job created while paused | `tests_runtime_pause.py` |
| EXE-5 | Resume (explicit-only; no auto-resume) | No resume without an explicit authorised call + live eligibility | `tests_runtime_resume.py` |
| EXE-6 | Credential invalidation | No execution on invalidated eligibility (validation→NEVER, health→UNKNOWN) | `trading/tests_credential_invalidation.py` |
| EXE-7 | Test-order (`PLACE_TEST_ORDER`) gated + audited | No demo exposure opened for an ineligible account; clean 503 | `tests_e3_demo_promotion.py`, `tests_wse.py` |
| EXE-8 | Scheduler refusal parity (h1/m5/h4) | Durable refusal re-emit; parity; no unhandled exception | `tests_wse.py` |
| EXE-9 | Promotion rejection (pause pre-check) | No PLANNED-slot leak; plan voided with durable `PROMOTION_REJECTED` | `tests_wse.py`, `tests_e2a_promotion.py` |
| EXE-10 | Admin retry gate + route-inventory drift guard | Retry refused for a now-ineligible account; drift guard green (no UNKNOWN/FIX_REQUIRED) | `tests_execution_entrypoints.py` |

## Method + PASS

In the disposable environment, arm `BROKER_CONNECTIVITY_EXECUTION_GATE` (+ `_HEALTH_ENABLED` for
health-driven cases) **only for the certification run**, and for each route attempt an exposure-opening
action against an **ineligible** demo account at each boundary; then confirm an **eligible** demo account
executes unchanged. Capture the refusal audits + projections and the eligible-passes-unchanged proof.
**PASS = zero ineligible executions at any point across every inventory route, eligible flows unchanged, and
the drift guard green.** Any ineligible execution is a **NO-GO** and a SEV-1.
