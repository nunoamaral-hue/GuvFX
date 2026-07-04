# Wayond Listener — Deploy Runbook (SKELETON, not yet deployed)

Read-only Telegram listener that feeds `signal_intake.acquire_message`. Built repo-only
in GFX-PKT-SIGNAL-ACQUISITION-LISTENER-BUILD; **not deployed** until the GFX session is
aged and survives reuse.

## Hard guarantees (verified by tests)
- **Read-only**: never sends a Telegram message, never downloads media bytes.
- Feeds **only** `acquire_message` — no execution import, no `order_send` (boundary test).
- Nothing is auto-traded: entries → `PendingSignalApproval` (human RBAC gate); updates
  are record-only. E3 remains RED.

## Preconditions (do NOT deploy until all true)
1. **Aged, authorised GFX session** that survives reuse (see memory
   `project-telegram-session-deauth`; provision with `provision_telegram_session`, age
   7–14 days, enable 2FA). Verify the saved StringSession loads with
   `is_user_authorized() == True` from a separate process.
2. Secrets staged in the deploy secret store (never committed / never on the CLI):
   `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_STRING_SESSION`, and the FROZEN
   device fingerprint (`TELEGRAM_DEVICE_MODEL/SYSTEM_VERSION/APP_VERSION`) — the SAME
   values used to mint the session.
3. Telethon available in the listener image (`pip install -r backend/requirements-telegram.txt`).
4. At least one **ARMED** `SignalProvider` whose `telegram_chat_id` is Wayond's chat id
   (provider arming is a separate, gated action — Red).

## Dry-run first (no Telegram)
Validate the pipeline on a fixture before ever connecting:
```bash
python manage.py run_wayond_listener --fixture msgs.json --dry-run   # preview only, no writes
python manage.py run_wayond_listener --fixture msgs.json             # into the real pipeline
```
Fixture = JSON `{"messages": [{message_id, chat_id, text, date, reply_to_message_id, edit_date, media}, ...]}`.

## Go live (only after preconditions)
```bash
docker compose -f docker-compose.yml \
  -f deploy/wayond-listener/docker-compose.wayond-listener.yml up -d guvfx-wayond-listener
docker logs -f guvfx-wayond-listener   # expect: connected → catch-up → 'heartbeat: state=listening'
```
On start it catches up from each provider's `watermark_last_message_id`, then listens
for new + edited messages. Flood-waits are honoured (sleeps the requested seconds).

## Rollback
```bash
docker compose -f docker-compose.yml \
  -f deploy/wayond-listener/docker-compose.wayond-listener.yml down guvfx-wayond-listener
```
Stateless service — stopping it halts ingestion; the backend/worker are untouched. To
pause ingestion without stopping the container, set the provider status to `PAUSED`
(dispatcher then drops its messages `DROPPED_NOT_ARMED`).
