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
pin + demo-only + no-credential-login + **a node-aware worker identity** all hold) with a **node-aware
`WorkerIdentity`** (`worker_permissions.authorized_nodes = [node hostname]`). We do NOT fork a second worker
— the SAME bridge only selects its auth headers by mode:

- **Legacy mode** (flag unset): `get_headers()` sends `X-Worker-Token` → the shared `legacy-worker` identity
  (no `authorized_nodes`). Byte-for-byte unchanged.
- **Hosted mode** (`MT5_HOSTED_EXECUTION` set): `get_headers()` sends `X-Worker-Id` + `X-Worker-Secret`
  (`GUVFX_WORKER_ID` / `GUVFX_WORKER_SECRET`) → the bridge's own **dedicated node-aware `WorkerIdentity`**.
  This is required because `views.next_job` only routes a hosted (non-NULL node) job to — and
  `authorize_hosted_claim` only entitles — a worker whose resolved `authorized_nodes` is non-empty, and only
  the modern id/secret path resolves such an identity. **RULE 3 (no silent substitution):** hosted mode
  requires BOTH `GUVFX_WORKER_ID` and `GUVFX_WORKER_SECRET`; if either is missing the bridge fails closed at
  startup rather than falling back to the shared legacy token (granting the shared `legacy-worker` row nodes
  would defeat the per-node isolation the `ER_WORKER_NOT_ENTITLED` check exists to enforce).

**Single-path proof (test):** `tests_hosted_capstone.SinglePathProofTests` structurally SWEEPS the hosted
backend tree (a glob of the whole `hosted_workspace` app + the `execution` hosted seam + readiness/gate — not
a hand-maintained module list) and asserts two invariants: (1) **no** backend module calls the order-mutation
surface (`order_send`/`order_check`); (2) the ONLY broker-API (`MetaTrader5`) importers are a small
**sanctioned, host-only, READ-ONLY** allow-list (the observation-certification command), each re-proven
order-free by (1). The bridge is the sole `order_send` caller + sole order-time gate. A new hosted module
that imported the broker API or called `order_send` would fail the sweep. There is no alternative hosted
execution path.

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
6. node-aware, entitled `WorkerIdentity` claims it (claim-seam) — the bridge presents it via the modern
   `X-Worker-Id`/`X-Worker-Secret` path in HOSTED mode; a shared/legacy-token worker is refused
   (`ER_WORKER_NOT_ENTITLED`)
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
2. Register the bridge's own `WorkerIdentity` (`worker_id` + a fresh secret) (operator). This is the identity
   the bridge presents in HOSTED mode via `X-Worker-Id`/`X-Worker-Secret` — dedicated, never the shared
   `legacy-worker`.
3. `manage.py provision_hosted_execution --account-id N --node-hostname H --grant-worker WID` — binds the
   workspace→node, sets the account node, grants that worker node-awareness (`authorized_nodes += [H]`).
   **Places no order.**
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
Nuno starts the bridge in HOSTED mode (`MT5_HOSTED_EXECUTION=1`, `MT5_GUARDED_ATTACH=1`,
`MT5_REQUIRE_IDENTITY_PIN=1`, and the bridge's own `GUVFX_WORKER_ID`/`GUVFX_WORKER_SECRET` — the bridge
refuses to start if any is missing) as the node-aware worker; a **single minimum-volume DEMO** market
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
path. Invariants confirmed intact: bridge is sole order-time gate; no order/attach/login; DARK/default-OFF;
no migration arms; one workspace → one node; legacy Provider-A unchanged.

### 7a. Completeness-audit remediation (P1–P5)

A final in-boundary completeness audit (5 boundary dimensions → per-finding refutation → synthesis) found the
"only the manual demo order remains" framing was **false** and produced five additive DARK fixes:

- **P1 (HIGH) — the certified bridge could not present a node-aware identity.** `get_headers()` sent only
  `X-Worker-Token` → the shared `legacy-worker` row (no `authorized_nodes`), so a hosted (non-NULL node) job
  was either excluded from the bridge's claimable set (legacy branch filters `terminal_node__isnull=True` →
  204) or refused (`ER_WORKER_NOT_ENTITLED`). The documented demo order was therefore **not claimable**.
  **Fix:** the SAME bridge, in HOSTED mode, authenticates via the modern `X-Worker-Id`/`X-Worker-Secret` path
  (`GUVFX_WORKER_ID`/`GUVFX_WORKER_SECRET`) as its own **dedicated** node-aware `WorkerIdentity`; RULE 3
  fail-closed at startup if either is missing (never falls back to the shared token — granting the shared row
  `authorized_nodes` would defeat per-node isolation). Legacy mode byte-for-byte unchanged. No fork.
- **P2 (MED) — RULE-11 positive control.** The claim endpoint was only proven in the refusal/dark directions.
  Added a subsystem-ON positive control: a provisioned+armed+node-bound hosted job IS served (200/RUNNING) to
  a node-aware worker and records the STARTED provenance row; plus a companion negative (a worker with no
  `authorized_nodes` gets 204, job left PENDING).
- **P3 (MED) — structural single-path proof.** The proof was a hand-maintained 15-module allow-list whose
  "no `MetaTrader5` import in the hosted backend" predicate was already false (the read-only observation
  command imports it). Replaced with a STRUCTURAL tree sweep: (1) no hosted backend module calls
  `order_send`/`order_check`; (2) the only broker-API importers are a sanctioned host-only READ-ONLY
  allow-list — with a positive control that the order-surface regex actually fires.
- **P4 (LOW) — provision-command grant block tested** (append the correct node, preserve existing perms,
  idempotent, fail-closed on unknown worker).
- **P5 (LOW) — arm fail-closed coverage.** Asserted the 8 previously-unasserted `_arm_preconditions` reason
  codes + the disarm no-workspace branch.

A fresh 5-lens adversarial review of P1–P5 (find → refute → synthesize) returned **0 surviving HIGH / 0
MEDIUM** — the one HIGH candidate (a `--grant-worker legacy-worker` misuse) was verified and calibrated to
LOW (operator-misuse-only of a DARK command; bridge remains sole order gate; loud + reversible). All five LOW
survivors were then **closed as hardening**:

- **Grant-block isolation guard** — `provision_hosted_execution --grant-worker` now refuses the reserved
  `legacy-worker` id (`CommandError`), AND `views.next_job` forces the shared `legacy-worker` identity
  non-node-aware at the claim path (defence-in-depth; a no-op for its normal empty-perms row). So the shared
  identity can never be treated as node-aware however its perms were set. + tests.
- **Cross-node regression test** — a worker authorised for node-A is refused a correctly-provisioned node-B
  hosted job (204, job PENDING) — proving per-worker node scoping, not merely node-aware-vs-legacy.
- **Import-surface positive control** — the single-path sweep now asserts the sanctioned importer IS detected
  (`assertIn`) plus a standalone `import_re` positive/negative control, so invariant (2) can't pass vacuously.
- **Arm mutation adequacy** — the compound `RW_WORKSPACE_NOT_READY` branch is split into two single-disjunct
  tests so a mutant dropping either sub-check dies.

Verification: focused 56/56; full `execution` 908/908; `execution`+`hosted_workspace` green; `make check`
green. Still DARK/flags-OFF; no migration; no order placed; the live bridge gate remains the sole order
authority.

### 7b. Round-2/3 completeness remediation (full-boundary audit)

A comprehensive full-boundary audit (7 lenses → refute → synthesize) confirmed the production code is
behaviorally complete and closed 7 remaining test/doc/pre-arming items — all DARK, no order:

- **Completion provenance (was MEDIUM)** — an endpoint-level positive control now proves `POST /complete/`
  drives `record_hosted_completion` → the FINISHED `HostedWorkspaceExecution` row + `execution_finished`
  emission (was direct-call only; symmetric with the STARTED control).
- **Completion-half isolation** — `views.complete` now enforces hosted node entitlement by worker
  **membership** of the job's node (a claim-forbidden or wrong-node worker is refused 403 without mutating;
  the shared `legacy-worker` is forced non-node-aware). Entitlement is membership, **not** node liveness — a
  worker may still report the outcome of a job it holds after its node enters maintenance (drain-mid-flight
  test). DARK/zero-query while off; non-hosted/legacy completion unchanged.
- **Ambiguous telemetry** — `workspace.execution_ambiguous` is now a `WorkspaceEvent` member routed through
  `build_workspace_event`; a code-DERIVED emit-surface test detects any future emit drifting out of the enum.
- **Idempotency docstring** — corrected to state the key is provenance/audit only (not the order-time dedup)
  and name the real guards (single-claim + never-re-enqueue + bridge comment guard + `(job,phase)` unique).
- **Fail-safe** — raise-injection tests prove the provenance/telemetry try/except swallows a raising recorder
  or DB write without propagating (never 500s an already-committed claim/completion).
- **Readiness mutation adequacy** — the dispatch-path `RW_WORKSPACE_NOT_READY` compound is split into two
  single-disjunct tests; freshness future(`age<0`)/`None` branches independently covered.

Two adversarial reviews of the fixes (round-2 fixes + the completion gate) returned **0 surviving HIGH / 0
MEDIUM**; both self-introduced LOW defects (the completion gate's node-liveness bug + a hand-written
emit-surface literal) were then corrected. Verification: focused 90/90; full `execution`+`hosted_workspace`
1128; `make check` green (backend 3323).
