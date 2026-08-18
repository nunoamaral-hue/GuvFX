# ADR-0048 — Execution-path availability is a distinct readiness tier

**Status:** Accepted (code/architecture/test only; DARK; no production activation in this packet)
**Date:** 2026-08-18
**Context packets:** AJ#7.2.1 Node-2 forensic → P0 Node-2 Execution Activation (code phase)

## Context

Real TI signals for the enabled acceptance customer (MT5 `1302587`, account 25, node 2
`guvfx-beta-node-1`) traversed ingestion → parse → approve → route → plan → `ExecutionJob`, then
stopped at **JOB → CLAIM**: no `WorkerIdentity` was authorized for node 2, so six `PLACE_ORDER`
jobs sat `PENDING` forever with **zero fills**, and the two `PROMOTED` plans behind them
permanently saturated the per-account exposure budget (`account_exposure_exceeded` on every later
signal).

Root cause is architectural, not a one-off: the system conflated **four distinct properties** and
treated a node with operator-declared `status == ACTIVE` as executable even though nothing could
claim its jobs. A single hidden hole silently presented the platform as fully trading.

## Decision

**These four properties are distinct readiness tiers and MUST NOT be conflated. In particular,
"MT5 capability" ≠ "customer authorization" ≠ "execution-path availability" ≠ "order authorization".**

| Tier | Question | Authority (unchanged) |
|---|---|---|
| **A. MT5 / runtime readiness** | terminal up, attached, connected, correct account, AutoTrading allowed, observation fresh | canonical `EXECUTION_READY` / `PersistentWorkspaceProvider` |
| **B. customer strategy authorization** | customer explicitly enabled the strategy | ADR-0047 `execution_authorized_at` + arm bit |
| **C. execution-path availability** ← **NEW** | can a `PLACE_ORDER` for this account's node actually be **claimed and dispatched**? | `execution.node_execution` (read-only) |
| **D. order authorization** | the per-order, fail-closed, live decision | bridge `evaluate_binding` (host) — **unchanged** |

Tier C is a new **read-only, non-authoritative** surface. It NEVER places, sizes, or approves an
order and is NEVER consulted at order time; the live bridge gate (D) remains the sole order-time
authority. It is composed of existing pieces (never re-derived): `resolve_order_transport` (route),
`resolve_hosted_route` (owner-bound single armed route + node agreement), node `ACTIVE` + configured
order bridge, **a new "an ACTIVE, non-revoked, node-aware order-capable `WorkerIdentity` exists for
this node" query**, and `reliability.ComponentHealth` for bridge/worker liveness. It is fail-closed
(unknown ⇒ not ready) and DARK-safe (reports `EP_EXPECTED_DARK` while the hosted pin subsystem is
off).

### Consequences / implementation (this packet — DARK, repo only)

1. **Shared node-awareness rule.** `execution.node_execution.worker_authorized_nodes` is the single
   source of truth for "which nodes may a worker claim" (legacy force-empty + ACTIVE-only + list
   coercion). `execution.views.next_job` now derives its node filter from it, so the readiness
   surface can never drift from real claim behaviour.
2. **Worker liveness.** `WorkerIdentity.last_seen` (additive, nullable) is stamped, throttled, in the
   claim seam. NULL ⇒ never-seen ⇒ not-alive (fail-closed). This is the missing concept-C primitive.
3. **Node-operational commission gate.** `node_execution_operational(node)` — a node is NOT
   execution-operational (allocatable/armable) until it has a configured bridge AND an authorized
   node-aware order-capable worker. The static "an eligible claimant exists" query is the required
   read-only synthetic claimability check (no `ExecutionJob` is created to perform it). A future
   dynamic `NODE_PROBE` round-trip may layer on top; it is intentionally out of scope to keep the
   check order-free.
4. **Stale pre-activation reconciliation.** `execution.stale_reconcile` +
   `manage.py reconcile_stale_preactivation_orders` cancels never-claimed `PENDING` `PLACE_ORDER`
   jobs → `FAILED` (the only viable terminal job state; there is no `CANCELLED` and `SUCCESS` would
   demand a CLOSED Trade that never exists) via `select_for_update(skip_locked=True)` + compare-and-set
   on `status == PENDING` (can never race a live claim; PENDING-only since PLACE_ORDER is
   non-idempotent), then reuses `close_monitor.resolve_completed_plans(account_id=…)` to move the
   plans PROMOTED → CLOSED, which is what actually releases the exposure + concurrency budget.
   DRY-RUN by default; refuses Customer Zero (account 1) and account 18; idempotent; audited.
5. **Operational health.** `scan_execution_path_health()` surfaces the fail-closed conditions
   (`NODE_NO_ELIGIBLE_WORKER`, `NODE_WORKER_STALE`, `NODE_BRIDGE_UNHEALTHY`,
   `NODE_PENDING_NO_CLAIMANT`, `JOB_STUCK_PENDING`) so the platform can never silently present itself
   as fully executable.

### Read-model truth (documentation only — for a future UX packet)

No customer-facing field today tells the customer *your orders can actually be dispatched*.
`signal_copy_status.enabled` (assignment active) and `execution_armed` (workspace arm bit) can both
be TRUE while dispatch is dead. A future UX packet should split the single **"Enabled"** into
**"Enabled — listening for signals"** (tiers A+B) vs **"Live — placing trades"** (tier C proven via
real dispatch evidence), deriving the new signal fail-closed from tier C — never assumed. "Enabled"
may remain a statement of customer *intent*, but the read model must not imply orders execute when
the execution path is unavailable.

## Invariants preserved

* ADR-0047 (explicit customer authorization) and the live order-time gate (D) are **unchanged and
  not weakened**. Tier C is ANDed *alongside* them, never merged into them, and never authorises.
* Fail-closed node/tenant isolation: a node-1 worker can never claim node-2 jobs; the shared legacy
  worker and any revoked worker are never eligible claimants; routing is server-derived (a caller
  cannot inject an arbitrary node).
* Customer Zero and account 18 are untouched and are hard-refused by the reconciler.

## Alternatives rejected

* **Add a `CANCELLED` `ExecutionJob` status.** Rejected: Amber structural change requiring a
  migration + updates to every consumer of the 4-value status set; `FAILED` + a distinguishing
  `recovery_reason` is the existing, proven pattern (`provider_commands_engine._cancel_pending_order_jobs`).
* **Overload canonical `EXECUTION_READY` with worker/bridge availability.** Rejected: it is an
  observation of the tenant's MT5 runtime (tier A); folding tier C into it would blur the authority
  boundary and change what a green readiness snapshot means.
