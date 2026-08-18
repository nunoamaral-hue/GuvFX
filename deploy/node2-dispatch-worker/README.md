# Node-2 dispatch worker — deployment runbook (Closed-Beta)

Stands up the **missing** Node-2 dispatch worker discovered at the end of Boundary B. Boundary A
commissioned the *identity* `mt5-node2-order-1` and the `:8789` bridge exists, but no **process**
authenticated as that identity to claim node-2 jobs and route them to the bridge. This package is
that process, as a durable supervised service.

> **This package alone does NOT authorize live execution.** It is deployed and heartbeat-certified in
> the **B0** (no-order) window; live claiming is only turned on later in **Boundary B** after separate
> Sponsor approval. See `docs/operations/hosted-workspace/NODE2_MAINTENANCE_WINDOW_PACKET.md`.

## Files

| File | Role |
|------|------|
| `docker-compose.node2-worker.yml` | The supervised service. Same image/binary as the node-1 worker; only the WorkerIdentity differs. No secret literals. |
| `node2-dispatch-worker.env.template` | Template for the host env-file. Copy → `node2-dispatch-worker.env` (mode 600), fill placeholders in the window. Never commit the filled file. |

## Preconditions (all must hold before starting)

1. **Token rotated.** The shared inbound agent token has been rotated (the pre-rotation value is
   compromised — public Git history `67de147`, and re-surfaced 2026-08-18). `./bridge-agent.env`
   holds the **new** token. **Never start this worker on the old token.**
2. **Identity commissioned.** `WorkerIdentity(mt5-node2-order-1)` is ACTIVE with
   `authorized_nodes == ["guvfx-beta-node-1"]` (Boundary A). Verify:
   `docker exec guvfx-backend python manage.py commission_execution_node --node-hostname guvfx-beta-node-1 --worker-id mt5-node2-order-1 --json` → `operational:true`.
3. **Secret present.** `/home/ubuntu/guvfx-prod/node2-worker-secret.env` holds the node-2 worker
   secret (mode 600). Its value becomes `MT5_WORKER_TOKEN` in `node2-dispatch-worker.env`.
4. **Bridge up.** Node-2 bridge healthy on `:8789` (Windows host, `Activate-Node2Bridge.ps1` already run).
5. **Queue clean.** `execution` PENDING/RUNNING PLACE_ORDER jobs for node 2 = **0**
   (reconcile stale pre-activation jobs first — see the maintenance-window packet). This is the
   hard gate that makes the B0 start a *no-order* start.

## Deploy (supported orchestration only — RULE 1)

```bash
cd /home/ubuntu/guvfx-prod                      # place these two files here alongside bridge-agent.env
cp deploy/node2-dispatch-worker/node2-dispatch-worker.env.template node2-dispatch-worker.env
chmod 600 node2-dispatch-worker.env
# fill node2-dispatch-worker.env placeholders (MT5_WORKER_TOKEN from node2-worker-secret.env, Django/DB from host)
docker compose -f docker-compose.node2-worker.yml up -d
```

Never run `python mt5_trade_ingest_worker.py` directly from an interactive SSH shell as the
production mechanism — a shell-bound process dies with the session (RULE 1).

## B0 heartbeat / claimability certification (NO order — queue empty AND intake paused)

> Signal intake (`guvfx-wayond-listener` + the bound assignment) MUST remain paused throughout this
> step and until claim authority is closed below — otherwise a fresh signal could enqueue a node-2 job
> that this ACTIVE worker would claim and dispatch. See `NODE2_MAINTENANCE_WINDOW_PACKET.md` §4.


```bash
# worker authenticates + heartbeats
docker logs guvfx-mt5-node2-order-worker --since 60s   # normal polling, no secret values, no crash loop
docker exec guvfx-backend python manage.py shell -c "
from execution.models import WorkerIdentity, TerminalNode
from execution.node_execution import node_execution_operational, execution_path_state
from trading.models import TradingAccount
w=WorkerIdentity.objects.get(worker_id='mt5-node2-order-1')
n=TerminalNode.objects.get(hostname='guvfx-beta-node-1')
print('last_seen', w.last_seen, 'nodes', (w.worker_permissions or {}).get('authorized_nodes'))
print('operational_live', node_execution_operational(n, require_worker_liveness=True))
print('path_state', execution_path_state(TradingAccount.objects.get(account_number='1302587')))"
```
Expect: `last_seen` fresh; `authorized_nodes == ['guvfx-beta-node-1']`; `operational_live` TRUE;
`execution_path_state` ready. Because the queue is empty, **0 jobs are claimed, 0 order_send, 0 trades.**

## Close claim authority at the B0 STOP (no silent live-order window)

The B0 packet forbids leaving a claim-capable worker running when control returns. Choose one:

```bash
# Preferred: stop the worker (identity stays ACTIVE, re-startable for Boundary B)
docker compose -f docker-compose.node2-worker.yml down
```
or, to keep the container but remove claim authority:
```bash
docker exec guvfx-backend python manage.py shell -c "
from execution.models import WorkerIdentity
w=WorkerIdentity.objects.get(worker_id='mt5-node2-order-1'); w.status='REVOKED'; w.save(update_fields=['status'])"
# re-commission (re-activate) at Boundary B start:
#   commission_execution_node --node-hostname guvfx-beta-node-1 --worker-id mt5-node2-order-1 --apply
```

## Retire

```bash
docker compose -f docker-compose.node2-worker.yml down
```
Customer Zero's node-1 worker, `:8788` bridge, and secrets are never touched by this package.
