# Node-2 Execution Activation & First-Fill — Production Runbook (ADR-0048)

**Posture:** every step here is EXECUTION-AUTHORITY (Red). It ARMS autonomous order dispatch and,
on the next eligible signal, PLACES A LIVE (demo) ORDER on MT5 `1302587`. It is **operator/Nuno-gated**
and is deliberately NOT performed by the code phase (this repo work is DARK). Run only with explicit
Sponsor go.

Acceptance target (re-resolve dynamically before acting): User 29 (support@) · TradingAccount 25
(`1302587`) · HostedMt5Workspace 12 · node 2 `guvfx-beta-node-1` · order bridge `:8789`.

## HARD ORDERING (non-negotiable)

**Reconcile the six stale jobs BEFORE any node-2 worker may claim.** Activating claims while the
stale `PENDING` jobs remain executable would immediately fire 6 historical XAUUSD orders.

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

## Step 2 — Register a DEDICATED node-2 worker identity (+secret)
- Register a NEW `WorkerIdentity` (e.g. `mt5-node2-order-1`) with its own secret — NOT
  `mt5-trade-ingest-1` (node 1), NOT `legacy-worker` (the grant command refuses it).
- Keep the secret in the host worker's env only (never in Git / compose / logs — SEC RULE 1).

## Step 3 — Bind + grant + arm (DARK server records; places no order)
```
docker exec guvfx-backend python manage.py provision_hosted_execution \
    --account-id 25 --node-hostname guvfx-beta-node-1 --grant-worker mt5-node2-order-1 --arm
```
`--arm` fails closed unless every precondition holds; it only flips a durable boolean the live gate
still sits behind.

## Step 4 — Commission gate (read-only GO/NO-GO)
```
# node_execution_operational(node) must return operational before starting the worker
docker exec guvfx-backend python manage.py shell -c \
 "from execution.models import TerminalNode; from execution.node_execution import node_execution_operational; \
  n=TerminalNode.objects.get(hostname='guvfx-beta-node-1'); print(node_execution_operational(n))"
```
Expect `NODE_OPERATIONAL` (registered node-aware worker + configured bridge). If not, STOP and fix.

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
