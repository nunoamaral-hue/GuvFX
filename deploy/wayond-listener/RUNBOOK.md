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
```bash
docker compose -f docker-compose.yml \
  -f deploy/wayond-listener/docker-compose.wayond-listener.yml up -d --build guvfx-wayond-listener
```
On start it connects read-only, catches up from each provider's watermark, then listens
for new + edited messages (flood-waits honoured). `restart: unless-stopped`.

## Health & observability
- **Healthcheck:** the listener writes `/tmp/wayond_health` every 30s; the container
  healthcheck (`check_wayond_listener`) marks it **unhealthy** if that goes stale >120s.
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
