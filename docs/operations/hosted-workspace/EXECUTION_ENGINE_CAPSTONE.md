# ADR-0034 Execution Engine — Capstone: worker contract, arming, failure matrix, disposable-demo cert

Repository-complete capstone for the hosted execution loop (DARK, demo-only). This document is the
authoritative contract (PART 15), the arming hierarchy (PART 14), the failure matrix (PART 12), and the
disposable-demo certification runbook (PART 16–18). **Nothing here is deployed or armed.**

## 1. The complete hosted execution path (PART 15) — one path, no alternative

```
Strategy Signal
 → ExecutionJob (created; ExecutionJob.save injects the server-derived per-job identity pin +
                 stamps hosted_workspace_uuid)              [hosted_pin.inject_identity_pin]
 → Hosted Workspace resolution (owner-bound, one workspace) [hosted_routing.resolve_hosted_route]
 → TerminalNode resolution (durable workspace->node binding; workspace==account==job node)
                                                            [HostedMt5Workspace.execution_node, capstone]
 → Worker claim (node-aware WorkerIdentity; claim-seam entitlement under the row lock)
                                                            [views.next_job + authorize_hosted_claim]
 → server-side identity pin carried into the payload        [hosted_pin.identity_pin_for]
 → Guarded Attach / read-only observation (never launch/login) [M1 + hosted_workspace.agent]
 → live account observation (fresh account_info/terminal_info) [bridge]
 → ORDER-TIME identity gate (evaluate_binding / evaluate_mutation_identity) — SOLE order authority [bridge]
 → broker mutation (order_send)                             [bridge — the ONLY MetaTrader5 caller]
 → broker result
 → completion (views.complete)
 → provenance (HostedWorkspaceExecution STARTED/FINISHED)   [hosted_execution]
 → telemetry (workspace.execution_started/finished)         [hosted_execution]
 → reconcile if ambiguous (classify + quarantine, never resend) [hosted_reconcile]
```

**The worker is the certified bridge (`scripts/mt5_signal_bridge.py`) running in HOSTED mode** (`G6:
MT5_HOSTED_EXECUTION` → `evaluate_hosted_startup_config` refuses to start unless guarded-attach + mandatory
pin + demo-only + no-credential-login all hold) with a **node-aware `WorkerIdentity`**
(`worker_permissions.authorized_nodes = [node hostname]`). We do NOT fork a second worker.

**Single-path proof (test):** `tests_hosted_capstone.SinglePathProofTests` asserts no hosted BACKEND module
imports the broker API (`MetaTrader5`) — so none can call `order_send`/`login`. The bridge is the sole
importer + sole order-time gate + sole sender. There is no alternative hosted execution path.

## 2. Arming hierarchy (PART 14) — all must be true; default OFF

A hosted order can occur only when **every** term holds; each defaults false and **no migration arms
anything**:

1. `HOSTED_PERSISTENT_MT5_ENABLED` (global) — default OFF
2. `HOSTED_MT5_EXECUTION_ENABLED` (execution feature) — default OFF
3. `account.readiness_provider == persistent_workspace`
4. `HostedMt5Workspace.execution_enabled` (explicit per-workspace arm; only `arm_hosted_workspace_execution`
   sets it, fully preconditioned + audited)
5. durable `HostedMt5Workspace.execution_node` binding present AND equal to `account.terminal_node`
   (capstone) AND equal to the job's snapshotted node
6. node-aware, entitled `WorkerIdentity` claims it (claim-seam)
7. demo-only (`account.is_demo`; the bridge refuses `trade_mode != 0`)
8. the LIVE order-time gates (guarded attach ∧ live identity match ∧ connected ∧ trade_allowed ∧ fresh ∧
   not paused) — re-proven by the bridge immediately before every `order_send`

Terms 1–5 gate creation/routing/claim (fail-closed reason codes); term 8 is the sole order authority.
Persisted `execution_ready` / observation / manager decision are **context, never authority**.

## 3. Failure matrix (PART 12) — every ambiguous path prefers STOP/RECONCILE, never RESEND

| Condition | Behaviour | Enforced by |
|-----------|-----------|-------------|
| workspace missing / disabled / wrong user | fail closed (route reason) | `resolve_hosted_route` (owner-bound) |
| wrong / missing / duplicate node, node drift | fail closed (`ER_NODE_UNBOUND`/`ER_NODE_MISMATCH`/`ER_ROUTE_MISSING`) | capstone routing + claim |
| wrong worker (legacy/shared) | fail closed (`ER_WORKER_NOT_ENTITLED`) | `authorize_hosted_claim` |
| not armed | fail closed (`ER_NOT_ARMED`) | readiness arm gate |
| terminal absent / Guarded Attach refused / IPC unavailable | fail-closed observation → not ready | M1 + agent |
| broker disconnected / account_info missing / login/server/demo mismatch / trade not allowed | order refused | bridge `evaluate_binding` |
| observation / readiness stale | not ready (freshness gate) | `_observation_fresh` |
| job cancelled / expired | not claimed / not executed | dispatch + lease |
| idempotency duplicate | at most one mutation | HWX key + provenance unique `(job,phase)` |
| broker order rejected | recorded FAILED; no resend | completion + provenance |
| **broker result ambiguous** | **quarantine + alert; never resend** | `classify_ambiguous_result` + `hosted_reconcile` |
| completion callback / telemetry / DB failure | fail-safe (order already gated; provenance idempotent) | post-commit fail-safe hooks |
| worker crash after send | reconcile from broker truth before any resend | reconcile driver (STILL_AMBIGUOUS ⇒ quarantine) |

Retry stance: `may_retry_after_ambiguous` is **advisory only** — nothing auto-resends; a retry is a
human-gated re-submission.

## 4. Disposable-demo certification (PART 16–18) — DARK setup + human-gated order

> **The order/close is a human action.** GuvFX policy prohibits Claude from placing/closing/modifying any
> order, including a demo order. **Nuno performs the demo order + close**; Claude does the DARK setup and all
> non-order verification. Marker at that point: `EXECUTION_ENGINE_REPOSITORY_COMPLETE — HOST_CERT_PENDING`.

**Host isolation:** disposable demo terminal on a **disposable node** — never the shared prod MT5 box
(`100.79.101.19`), never `:8788`/`:8791`, never Customer Zero / account #1 / a production strategy. Reuse
`C:\GuvFX\cert\repo` + `C:\GuvFX\cert\venv` where suitable.

### 4a. DARK setup (Claude / backend — no order)
1. Create a disposable demo `TradingAccount` (Provider-B, `is_demo=True`) + a disposable `TerminalNode`.
2. Register a node-aware `WorkerIdentity` (the bridge) + its secret (operator).
3. `manage.py provision_hosted_execution --account-id N --node-hostname H --grant-worker WID` — binds the
   workspace→node, sets the account node, grants the worker node-awareness. **Places no order.**
4. Nuno stands up the disposable demo MT5 + **logs into the demo broker** (the only broker-login step) and
   the observation chain drives the workspace to `EXECUTION_READY` (connected + account_match + trade_allowed
   + fresh).
5. `manage.py provision_hosted_execution --account-id N --arm` — explicit arm (fails closed until every
   precondition holds). Flags `HOSTED_PERSISTENT_MT5_ENABLED` + `HOSTED_MT5_EXECUTION_ENABLED` set in the
   **disposable env only**.

### 4b. BEFORE (Claude — read-only snapshots)
positions snapshot · orders snapshot · balance/equity/margin · workspace/node binding + generation ·
current broker identity (masked) · production blast-radius fence (prod MT5, `:8788`, `:8791`, Customer Zero,
account #1 untouched).

### 4c. PLACE + CLOSE (Nuno — the human-gated order)
Nuno starts the bridge in HOSTED mode as the node-aware worker; a **single minimum-volume DEMO** market
order flows through the loop (clear experiment comment/magic). Then a deterministic **close** through the
same loop. If any result is ambiguous: **STOP, do not resend, run reconciliation.**

### 4d. AFTER (Claude — verify, no order)
exact ticket/order ↔ exact workspace/account/node · `HostedWorkspaceExecution` STARTED+FINISHED rows ·
`workspace.execution_started`/`finished` events · `ExecutionJob` status · idempotency key · position absent ·
close deal present · margin released · **zero residual disposable position** · blast-radius re-proof.

## 5. Rollback
Disarm (`--disarm`), unbind (`--unbind`), set both flags OFF, revoke the disposable `WorkerIdentity`, remove
the disposable account/node/terminal. The subsystem returns to fully DARK with no residue. Migration `0004`
is additive/reversible.

## 6. Blast radius (PART 18)
The whole loop is DARK by default and demo-only by hard wall; the bridge is the only order path and it
refuses live (`trade_mode != 0`) and refuses to start without the hosted safety config. No production
service, port, ACL, firewall, RDS, or Workspace Delivery is touched by this repository work.

## 7. Adversarial review (PART 19) — record

6-lens review (routing-bypass · arming-bypass · binding-integrity · order-authority · legacy-regression ·
dark-flag) → per-finding skeptic refutation → synthesis. **0 surviving HIGH, 0 surviving MEDIUM.** One LOW
CONFIRMED + FIXED: `execution_binding_generation` was incremented in Python without a row lock (lost-update
race → non-monotonic counter under concurrent (re)assignment); fixed by wrapping `assign/clear_workspace_
execution_node` in `transaction.atomic()` + `select_for_update()` so the version is computed from the LOCKED
row (same pattern as the M3c writer). A second finding (SET_NULL node-delete bypasses the generation bump)
was REFUTED to NONE — the fail-closed NULL route gate is fully preserved and the counter gates no decision
path. Invariants confirmed intact: bridge is sole order-time gate; no order/attach/login; no MetaTrader5
import in the hosted backend; DARK/default-OFF; no migration arms; one workspace → one node; legacy
Provider-A unchanged.
