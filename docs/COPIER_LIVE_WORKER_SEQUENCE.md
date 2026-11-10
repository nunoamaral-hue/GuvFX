# Copier LIVE worker sequence — authoritative (2026-11-10, control-plane completion)

The single authoritative worker sequence for a governed LIVE copy of OPEN → MODIFY SL/TP → CLOSE (PARTIAL
is additive, not used by the #46→#48 demo). Every worker is DARK: inert unless its gates are on, re-read at
send time. File/line references are to this PR's tree.

## Contradiction resolved: `run_copier_execute` is NOT shadow-only
The prior `run_copier_execute` docstring claimed "DEMO-only / Never LIVE". That was **factually wrong** and
is corrected in this PR. The real path:

- `run_copier_execute` → `execution_producer.produce_pending` → `produce_one`
  ([execution_producer.py:181](../backend/trade_copier/execution_producer.py)).
- `produce_one` acquires `select_for_update` on the `CopierReplication` row (the **single-flight owner**),
  re-checks `copier_execute_enabled()`, re-runs `admit_open`, and then:
  - **LIVE (non-demo) follower** ([execution_producer.py:229-231](../backend/trade_copier/execution_producer.py)):
    `create_live_open_job(locked)` — a truthful LIVE `PLACE_ORDER`, **consuming one authorization allowance
    under the SAME lock** so a retry can never place a second order.
  - DEMO follower: falls through to the SHADOW/DEMO path (`PLACE_ORDER_SHADOW` unless `COPIER_EXECUTE_DEMO_ARMED`).
- `admit_open` routes a LIVE follower to `live_execution.admit_open_live`
  ([execution_producer.py:103-105](../backend/trade_copier/execution_producer.py)), the complete fail-closed
  per-pair LIVE admission (identity/pair/symbol/runtime/authorization/watermark). With any gate OFF, a LIVE
  account is refused fail-closed and no order is produced.

So `run_copier_execute` DOES reach the governed live OPEN producer when, and only when, the full live flag
stack + a valid authorization + the host admission are all present.

## Authoritative sequence

| Step | Worker (management command) | Produces / does | Gate(s) re-read |
|---|---|---|---|
| 1. Observe | `run_copier_observer` | master events → `MasterEvent` | `COPIER_ENABLED`+`COPIER_OBSERVER_ENABLED` |
| 2. Replicate | `run_copier_replication` | OPEN intent → `CopierReplication` (SHADOW_RECORDED) | `COPIER_ENABLED`+`COPIER_REPLICATION_ENABLED` |
| 3a. OPEN produce | `run_copier_execute` → `produce_one` | LIVE `PLACE_ORDER` via `create_live_open_job` (1 allowance/lock) | `copier_execute_enabled` + `admit_open_live` (authz, `COPIER_LIVE_EXECUTE_ENABLED`, `HOSTED_LIVE_EXECUTION_ENABLED`, host admission) |
| 3b. Lifecycle | `run_copier_lifecycle` | per-ticket lifecycle projection (ADR-0071) | `COPIER_ENABLED`+`COPIER_LIFECYCLE_ENABLED` |
| 4. MODIFY produce | `run_copier_execute_modifies` → `produce_pending_modifies` | LIVE `MODIFY_POSITION` via `create_live_modify_job` | `copier_live_modify_enabled` + authority |
| 5. CLOSE produce | `run_copier_execute_closes` → `produce_pending_closes` | LIVE `CLOSE_TRADE` via `create_live_full_close_job` | `copier_live_full_close_enabled` + authority |
| 6. Mutation dispatch | `run_copier_mutation_dispatch` → `run_mutation_dispatch_pass` | claim MODIFY/CLOSE job → per-tenant transport → #48 bridge | `copier_mutation_dispatch_enabled` + per-op arm + `copier_mutation_transport_enabled` (transport inert otherwise) |

> **Gaps this PR closes:** steps 4, 5 and 6 previously had **no production command** (`produce_pending_modifies/closes`
> and `run_mutation_dispatch_pass` were test-only). The OPEN `PLACE_ORDER` is dispatched to the bridge by the
> existing execution engine; the governed MODIFY/CLOSE path uses the ADR-0086 dispatcher + ADR-0087 transport.

## Watermark, single-flight, no-late-recovery (evidence)
- **OPEN single-flight**: `produce_one` holds `select_for_update` on the replication row and consumes the
  authorization allowance under that lock → a retry cannot place a second order.
- **MODIFY/CLOSE single-flight + watermark**: `mutation_dispatch` claims each job (single-flight) and advances a
  migration-free per-follower watermark (`follower_mutation_hwm`), which fails **closed** (returns `None` → the
  command refuses) on a DB error.
- **No late retry**: the transport is **one-shot**; a timeout / non-2xx / indeterminate result is
  `UNKNOWN_OUTCOME` (need-reconcile) — never success, never re-sent. There is **no late-OPEN** path: a failed
  follower OPEN is recorded failed and abandoned.
- **Flags re-read at send**: `copier_execute_enabled` (OPEN, under the lock), `admit_open_live` (authz + live
  flags), and `_governing_authority_ok` (MODIFY/CLOSE: per-op arm + execute-enabled re-read at dispatch time).
- **Fail-closed on restart/expiry/drift**: `admit_open_live` / the bridge admission re-read the authorization
  and `MT5_LIVE_EXPIRY` fresh per call; a restart clears in-memory state; an expired authorization admits nothing.

## Inert-when-OFF invariant
With all copier flags OFF (default), every worker above is a no-op and the per-tenant transport is inert — no
`ExecutionJob` is created on the live path, no broker is contacted, and the money-function demo wall is
byte-identical. This is the standing production posture.
