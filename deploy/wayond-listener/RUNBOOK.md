# Wayond Listener — Production Deploy Runbook

Read-only Telegram listener that reads the Wayond channel into `signal_intake`'s shadow
ledger. **Acquisition-only** — it never sends a Telegram message, never downloads media
bytes, feeds `acquire_message` only, and the provider it feeds stays **UN-ARMED** (so
nothing is intaken/traded). E3 stays RED.

Current authorised operating mode (GFX-PKT-TEMPORARY-PRODUCTION-ACCOUNT-DEPLOYMENT):
**temporary operational exception** — the session is minted from Nuno's **personal**
account while GFX ages. Target architecture remains the dedicated **GFX** account (see
`WAYOND_LISTENER_MIGRATION.md`).

## Hard guarantees (verified by tests)
- Read-only: never sends / never downloads media bytes (boundary tests).
- Feeds **only** `acquire_message`; no `execution` import, no `order_send`.
- Nothing is auto-traded: provider **un-armed** → messages `DROPPED_NOT_ARMED`; even if
  armed, entries only become `PendingSignalApproval` behind the human RBAC gate.

## Pre-deploy (operator — Nuno's hands)
1. **Enable Telegram 2-Step Verification** on the account whose session you'll deploy.
2. **Mint a FRESH production session AFTER 2FA** (replaces the temporary validation one):
   ```bash
   cd backend
   export TELEGRAM_API_ID=… TELEGRAM_API_HASH=…
   export TELEGRAM_DEVICE_MODEL="Desktop" TELEGRAM_SYSTEM_VERSION="Windows 10" TELEGRAM_APP_VERSION="4.16.8"
   python manage.py provision_telegram_session --session-out ~/.guvfx/prod.session --wayond-chat <wayond>
   ```
   Verify it **persists** (`is_user_authorized()==True` on reuse from a separate process,
   still True after ~15 min).
3. **Store the session in the production secret store** as `TELEGRAM_STRING_SESSION`
   (600-perm, rsync-excluded `.env`), alongside `TELEGRAM_API_ID/HASH` and the **frozen**
   `TELEGRAM_DEVICE_MODEL/SYSTEM_VERSION/APP_VERSION` (must equal the values used to mint).
   Never commit; never on the CLI.
4. Ensure a `SignalProvider` for Wayond's chat id exists and is **NOT armed** (ONBOARDING).

## Deploy

The listener loads its **whole** environment via `env_file` from the prod secret store —
`wayond-listener.env` (DB_*, DJANGO_SECRET_KEY, the 6 TELEGRAM_*, and the functional keys
`MULTI_ACCOUNT_ROUTING_ENABLED` / `RISK_MAX_DAILY_DRAWDOWN_ABS` / `HOSTED_PERSISTENT_MT5_ENABLED`
/ `GUVFX_AGENT_URL` / `GUVFX_WINDOWS_AGENT_BASE_URL`) and `bridge-agent.env`
(`GUVFX_WINDOWS_AGENT_TOKEN` + aliases). Values live ONLY in those 600-perm files; the overlay
references them **by name**. docker compose resolves relative `env_file` paths against the
**project directory** (the dir of the first `-f` file). The base `docker-compose.yml` lives only
in `/home/ubuntu/guvfx-prod/` (not in this repo), so **pin the project directory** so the *.env
files are found:

```bash
docker compose --project-directory /home/ubuntu/guvfx-prod \
  -f /home/ubuntu/guvfx-prod/docker-compose.yml \
  -f /home/ubuntu/guvfx-app/deploy/wayond-listener/docker-compose.wayond-listener.yml \
  up -d --build guvfx-wayond-listener
```

**Pre-recreate GATE (RULE 8 — run on the host BEFORE `up`).** Prove the resolved config carries
every functional key + the Windows-agent token; a missing key means a routing/drawdown regression
or an `order_check` 401. Presence-only (prints key NAMES, never secret values):

```bash
docker compose --project-directory /home/ubuntu/guvfx-prod \
  -f /home/ubuntu/guvfx-prod/docker-compose.yml \
  -f /home/ubuntu/guvfx-app/deploy/wayond-listener/docker-compose.wayond-listener.yml config \
  | grep -oE '(MULTI_ACCOUNT_ROUTING_ENABLED|RISK_MAX_DAILY_DRAWDOWN_ABS|HOSTED_PERSISTENT_MT5_ENABLED|GUVFX_AGENT_URL|GUVFX_WINDOWS_AGENT_BASE_URL|GUVFX_WINDOWS_AGENT_TOKEN):' \
  | sort -u
# EXPECT all 6 key names. Fewer → ABORT (a source *.env is missing/misresolved).
```

The committed key contract is `wayond-listener.env.example` (names only); CI enforces it via
`backend/execution/tests_wayond_listener_deploy.py` (fails if the overlay drops an env_file or
re-introduces an `environment:` block that could shadow the file).

On start it connects read-only, catches up from each provider's watermark, then listens
for new + edited messages (flood-waits honoured). `restart: unless-stopped`.

## Health & observability
- **Healthcheck:** the listener writes `/tmp/wayond_health` every 30s; the container
  healthcheck (`check_wayond_listener`, run every 30s) marks it **unhealthy** if that
  goes stale >90s (a silent hang shows unhealthy in ~2 min).
  `docker ps` shows health; a crash exits the container → `unless-stopped` restarts it.
  (An *unhealthy-but-running* container needs an external watcher/orchestrator to restart —
  monitor `docker ps` health / add autoheal.)
- **Logs:** `docker logs -f guvfx-wayond-listener` → `connected` → catch-up count →
  `heartbeat: state=listening`, then per-message activity; flood-waits log a sleep. JSON
  logs rotate (10m × 5).
- **Ledger:** ingestion is visible as `AcquiredMessage` rows (outcomes) and the provider
  `watermark_last_message_id` / `last_signal_at` advancing. Correlation-id lifecycle
  logging (core.observability) ties each processed signal together.

## Rollback / pause
- **Stop:** `docker compose … down guvfx-wayond-listener` (stateless; halts ingestion,
  backend/worker untouched).
- **Pause ingestion without stopping:** set the provider status to `PAUSED` → dispatcher
  drops its messages `DROPPED_NOT_ARMED`.
- **Session compromise:** terminate the session on the account (Settings → Devices),
  rotate `TELEGRAM_STRING_SESSION` in the secret store, restart.
