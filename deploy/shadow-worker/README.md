# Managed Shadow Worker (EXEC-E2b-PERSIST)

Runbook for the **managed, restart-safe** shadow worker — the persistent form of
the ad-hoc dry-run proven in `E2b-DEPLOY-D2R`.

## What it is

A dedicated worker process that claims **only** `PLACE_ORDER_SHADOW` jobs and runs
the bridge `/mt5/order_check` dry-run. It **never** places an order:

- It runs the same `mt5_trade_ingest_worker.py` as the normal worker, but with
  `MT5_SHADOW_WORKER=1`, so `claim_worker_job()` claims **only** `PLACE_ORDER_SHADOW`
  (never `PLACE_TEST_ORDER`/`PLACE_ORDER`, never the default SYNC). It therefore
  cannot win a real order and route it to the live `order_send` path.
- The worker is HTTP-only (never imports MetaTrader5). For a shadow job it calls
  the bridge `/mt5/order_check` endpoint, which validates via `mt5.order_check()`
  and never `mt5.order_send()`.
- Its poll rate is one claim/loop (~30/min at the default 2 s sleep), well under
  the 100/min API throttle.

Two independent gates protect this:
1. **Worker-side:** shadow-only claim mode (`MT5_SHADOW_WORKER=1`).
2. **Server-side:** the `next_job` endpoint only serves `PLACE_ORDER_SHADOW` to an
   identity carrying `worker_permissions.shadow_worker`.

## Files

| File | Purpose |
|------|---------|
| `docker-compose.shadow-worker.yml` | Managed service definition (extends the normal worker, overrides identity/token/flag, `restart: unless-stopped`). No secret stored. |
| `verify_shadow_dryrun.sh` | Post-deploy verification: one full dry-run job → `order_check` only → asserts no order placed. |
| (backend) `manage.py provision_shadow_worker` | Idempotent create/revoke of the distinct shadow `WorkerIdentity` + `shadow_worker` grant. Secret from env, never printed. |

## Prerequisites

- The backend already ships the shadow-only worker (`ccc92a5`) and the `next_job`
  `shadow_worker` guard.
- Choose a **distinct** shadow worker id (default `mt5-shadow-worker-1`) — it must
  differ from the normal worker id (`mt5-trade-ingest-1`).
- Generate a **distinct** secret and export it (never commit it, never pass it as a
  CLI arg):

  ```bash
  export MT5_SHADOW_WORKER_TOKEN="$(openssl rand -hex 24)"
  ```

## Deploy (does not touch the normal worker)

Run from the prod compose directory (`GUVFX_COMPOSE_BASE` defaults to the sibling
`docker-compose.yml`).

```bash
# 1. Provision the distinct identity + shadow_worker grant (idempotent, no secret printed).
MT5_SHADOW_WORKER_TOKEN="$MT5_SHADOW_WORKER_TOKEN" \
  docker compose exec -T guvfx-backend python manage.py provision_shadow_worker

# 2. Bring up ONLY the shadow service.
MT5_SHADOW_WORKER_TOKEN="$MT5_SHADOW_WORKER_TOKEN" \
  docker compose \
    -f docker-compose.yml \
    -f /path/to/repo/deploy/shadow-worker/docker-compose.shadow-worker.yml \
    up -d --no-deps guvfx-mt5-shadow-worker
```

`--no-deps` ensures no other service (backend, DB, normal worker) is recreated or
restarted.

## Verify

```bash
deploy/shadow-worker/verify_shadow_dryrun.sh
```

Expected: `== VERIFICATION PASSED ==` — one dry-run job completes SUCCESS via
`order_check`, claimed by the shadow identity, `order_send_called=False`, no
ticket/deal/order/position; the job is then cleaned up.

Also confirm the normal worker is untouched:

```bash
docker inspect guvfx-mt5-trade-ingest-worker \
  --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -c '^MT5_SHADOW_WORKER='
# expect 0  (normal worker stays default OFF)
```

## Rollback

```bash
# Stop + remove the shadow service (normal worker unaffected).
docker compose -f docker-compose.yml -f .../docker-compose.shadow-worker.yml \
  rm -sf guvfx-mt5-shadow-worker

# Revoke the identity (drops shadow_worker, sets REVOKED).
docker compose exec -T guvfx-backend python manage.py provision_shadow_worker --revoke
```

Nothing else changes: the normal worker keeps its pre-E2b 3-claim behaviour
throughout, and the bridge `/mt5/order` live path is never modified by this
service.

## Safety boundary

This service is a **dry-run** facility. It does **not** place demo or live orders.
Real demo placement (**E3**) remains gated behind broker-timezone verification,
Blueprint doc 06, and Nuno's recorded sign-off — it is out of scope here.
