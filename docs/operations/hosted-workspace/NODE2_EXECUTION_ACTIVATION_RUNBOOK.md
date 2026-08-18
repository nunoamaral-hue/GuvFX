# Node-2 Execution Activation & First-Fill — Production Runbook (ADR-0048)

**Posture:** every step here is EXECUTION-AUTHORITY (Red). It ARMS autonomous order dispatch and,
on the next eligible signal, PLACES A LIVE (demo) ORDER on MT5 `1302587`. It is **operator/Nuno-gated**
and is deliberately NOT performed by the code phase (this repo work is DARK). Run only with explicit
Sponsor go.

Acceptance target (re-resolve dynamically before acting): User 29 (support@) · TradingAccount 25
(`1302587`) · HostedMt5Workspace 12 · node 2 `guvfx-beta-node-1` · order bridge `:8789`.

## Two distinct authorities (do not conflate)

This runbook has two clearly separated halves. **Commissioning a node is NOT authorizing a customer.**

* **NODE COMMISSIONING** (Steps 1–3, infrastructure) makes node 2 *able* to claim and dispatch its
  own orders: reconcile stale jobs, register the dedicated node-aware worker, prove
  `node_execution_operational`. **A commissioned node authorizes NO customer and places NO order.**
  It is server-derived and identical for Node 2, 3, 4, … (`commission_execution_node`).
* **CUSTOMER EXECUTION AUTHORIZATION** (Steps 4–6, per-customer) is the ADR-0047 arm bit for account
  25 plus starting the live worker/bridge — this is what makes the *next real signal* place a live
  (demo) order. Only this half is the live-fill trigger.

## HARD ORDERING (non-negotiable)

**Reconcile the six stale jobs BEFORE node 2 is commissioned (made claimable).** `commission_execution_node`
enforces this in code: it refuses (`STALE_ORDERS_PRESENT`) while un-reconciled stale `PENDING PLACE_ORDER`
jobs remain, so a node can never become claimable and fire 6 historical XAUUSD orders. The runbook order
and the code guard agree.

---

## Step 0 — Pre-state + backup (read-only + safety)
- `git`/image parity: confirm the ADR-0048 code is deployed (backend image carries
  `execution/stale_reconcile.py`, `execution/node_execution.py`, migration
  `0031_add_workeridentity_last_seen`).
- Verified pg backup (`pg_dump | gzip`, sha256) — as per the proven deploy pipeline.
- Record: `ExecutionJob` acct-25 states (expect 6 PENDING), acct-25 Trade count (expect 0),
  the two PROMOTED plans, Customer-Zero + acct-18 structural Golden BEFORE.

## Step 1 — Reconcile the stale jobs (releases the runway + exposure cascade)
```
# dry-run first — inspect exactly what will change
docker exec guvfx-backend python manage.py reconcile_stale_preactivation_orders --account-id 25
# then apply
docker exec guvfx-backend python manage.py reconcile_stale_preactivation_orders --account-id 25 --apply
```
Expect: 6 jobs PENDING→FAILED (recovered, recovery_reason `stale_preactivation_reconcile`), 2 plans
PROMOTED→CLOSED, exposure released. **No order placed.** Verify acct-25 has 0 PENDING PLACE_ORDER
jobs and 0 Trades.

## Step 2 — COMMISSION the node (register the dedicated worker + verify operational)
Provide the worker secret via env only (never a CLI arg, never Git/compose/logs — SEC RULE 1). Dry-run
first, then apply. Server-derived and identical for any node; refuses Customer Zero, the legacy identity,
cross-node reuse, and (hard ordering) any node with stale `PENDING` orders.
```
# dry-run — shows the operational verdict the node WOULD have
docker exec -e GUVFX_NODE_WORKER_SECRET=… guvfx-backend python manage.py commission_execution_node \
    --node-hostname guvfx-beta-node-1 --worker-id mt5-node2-order-1
# apply — registers/authorizes the dedicated node-aware worker (creates NO job, arms NO customer)
docker exec -e GUVFX_NODE_WORKER_SECRET=… guvfx-backend python manage.py commission_execution_node \
    --node-hostname guvfx-beta-node-1 --worker-id mt5-node2-order-1 --apply
```
Expect `operational=True reason=NODE_OPERATIONAL` (registered node-aware worker + configured bridge).
`mt5-node2-order-1` must NOT be `mt5-trade-ingest-1` (node 1) or the legacy id. If not operational, STOP
and resolve the reason before proceeding. **This step commissions infrastructure only.**

## Step 3 — (optional) arm the execution-path allocation gate for FUTURE customers
Once nodes are commissioned up-front, set `HOSTED_EXECUTION_PATH_GATE_ENABLED=1` so new automated hosted
accounts can only be allocated to an execution-operational node (fail-closed
`ALLOC_NODE_NOT_EXECUTION_OPERATIONAL` otherwise). Leave OFF while the beta journey still activates a
node's bridge per-slot after allocation — `execution_path_state` keeps the read-model honest in the interim.
This is a node/infrastructure policy flag; it authorizes no customer.

## Step 4 — AUTHORIZE the customer (ADR-0047 arm; DARK server record; places no order)
```
docker exec guvfx-backend python manage.py provision_hosted_execution \
    --account-id 25 --node-hostname guvfx-beta-node-1 --grant-worker mt5-node2-order-1 --arm
```
`--arm` is the per-customer authorization (tier B). It fails closed unless every precondition holds and
only flips a durable boolean the live order-time gate (tier D) still sits behind. Commissioning (Step 2)
is a precondition, not a substitute.

## Step 5 — Start the node-2 order worker + bridge on the host
- On the Windows host: `Activate-Node2Bridge.ps1` (starts the `:8789` bridge + watchdog) and start
  the node-aware worker process (`mt5_trade_ingest_worker.py` with `WORKER_ID=mt5-node2-order-1`,
  its secret, bound to the hosted customer's MT5 terminal). **RULE 1**: start via the supported
  service/scheduled-task mechanism, never an interactive SSH `Start-Process`.
- Verify liveness: within a poll interval, `WorkerIdentity(mt5-node2-order-1).last_seen` updates and
  `node_execution_operational(node, require_worker_liveness=True)` → operational;
  `scan_execution_path_health()` shows no `NODE_*` findings for node 2.

## Step 6 — First-fill certification (fresh real signal only)
- Do **not** replay/manufacture. Wait for the next real TI signal. Trace read-only:
  AcquiredMessage → approval → plan → job → claim(mt5-node2-order-1) → `:8789` → order-time gate →
  `order_send` → broker retcode → MT5 deal on `1302587`.
- Confirm the XAUUSD trade appears on `1302587` and the *following* eligible signal executes with no
  manual step (proves the deterministic contract).

## Step 7 — Golden AFTER
- Customer-Zero + acct-18 structural Golden AFTER == BEFORE (byte-identical). Node 2 only.

---

## ROLLBACK
- **Disarm dispatch (fast):** `provision_hosted_execution --account-id 25 --disarm`, and stop the
  node-2 worker/bridge (supported mechanism). New signals then queue (or fail-closed) instead of
  firing. Revoke the node-2 `WorkerIdentity` (`status=REVOKED`) to make it instantly ineligible
  (enforced at auth + the readiness/claim seam).
- **Un-bind:** `provision_hosted_execution --account-id 25 --unbind` clears the workspace execution
  node.
- **Backend code rollback:** the ADR-0048 change is additive + DARK; roll back to the prior backend
  image tag if needed. The `last_seen` column is nullable/additive — safe to leave in place.
- **Stale-reconcile is not reversible** (jobs are terminal FAILED, plans CLOSED) — but it placed no
  order and only neutralised never-executed jobs; the forensic trail is preserved on each job
  (`recovered`, `recovery_reason`). This is by design (those jobs must never fire).

## Remaining decisions (Sponsor)
- Whether/when to run Step 1 in production (releases the current `account_exposure_exceeded` cascade
  even without full activation — a genuine live bug today).
- Whether/when to arm node-2 dispatch (Steps 2–6) — the live-fill trigger.
