# Customer Telegram production activation runbook

Status: **human-gated; do not run during the DARK installation**.

This runbook activates one GuvFX-owned bot for one verified private conversation per customer. It never
uses the Wayond/provider bot, WIMs Stakeholder Review, Customer Zero, a group, or a channel. Activation,
the first pilot, and every customer message require a separate Sponsor decision.

## 1. Fixed production contract

- Webhook: `https://api.guvfx.com/api/customer-notifications/telegram/webhook/`
- Secret store: `/home/ubuntu/guvfx-prod/customer-telegram.env`, owner `ubuntu:ubuntu`, mode `0600`
- Worker definition: `/home/ubuntu/guvfx-prod/deploy/customer-notifications/docker-compose.customer-notifications.yml`
- Pilot: `beta.guvfx01@gmail.com`, or a Sponsor-approved disposable acceptance customer
- The bot token and webhook secret must never appear in Git, PR text, tickets, screenshots, command-line
  arguments, terminal transcripts, or shell history.

Authoritative environment keys:

```text
CUSTOMER_TELEGRAM_NOTIFICATIONS_ENABLED=false
CUSTOMER_TELEGRAM_WORKER_ENABLED=false
CUSTOMER_TELEGRAM_BOT_USERNAME=
CUSTOMER_TELEGRAM_BOT_TOKEN=
CUSTOMER_TELEGRAM_WEBHOOK_SECRET=
CUSTOMER_TELEGRAM_WEBHOOK_URL=https://api.guvfx.com/api/customer-notifications/telegram/webhook/
CUSTOMER_TELEGRAM_TOKEN_TTL_SECONDS=600
CUSTOMER_NOTIFICATION_MAX_ATTEMPTS=5
```

These keys and values are dedicated to the customer bot. Do not copy or alias any `TELEGRAM_CHAT_ID`,
provider listener, validation-agent, alert-sink, or WIMs credential.

## 2. Nuno: create and constrain the bot with BotFather

1. In Telegram, open the verified `@BotFather` conversation and send `/newbot`.
2. Enter a GuvFX customer-notification display name chosen by Nuno. Do not reuse an existing bot name.
3. Enter a unique username chosen by Nuno; Telegram requires the username to end in `bot`.
4. Copy the returned token directly into the approved password manager. Do not send it to an engineer or
   paste it into chat, a ticket, a screenshot, or a shell command.
5. Send `/mybots`, select the new bot, then open **Bot Settings**:
   - **Allow Groups?**: turn it off. The product accepts private chats only.
   - **Group Privacy**: leave enabled if the option remains visible; group use is still prohibited.
   - **Inline Mode**: leave disabled.
   - **Payments**, **Mini Apps**, **Business Mode**, **Domains**, and custom menu buttons: leave unset.
   - **Commands**: leave unset; this bot has no trading or strategy-control commands. `/start` is handled
     only with GuvFX's short-lived connection parameter.
6. Optionally set the description to: `Private GuvFX customer notifications. This bot never accepts trading instructions.`
7. Record the exact username (without guessing or changing case) in the password manager beside the token.

Stop here and hand the username/token to the authorized secret operator through the password manager.

## 3. Secret operator: stage credentials with both flags OFF

1. Confirm a fresh verified database backup and rollback image tags exist.
2. Open `/home/ubuntu/guvfx-prod/customer-telegram.env` using an interactive editor on the production host. Do not put a
   secret in the editor command or use `echo`, command substitution, clipboard logging, or an argv value.
3. Insert the BotFather username and token. Generate an independent 48-byte URL-safe webhook secret inside
   the password manager (preferred) and insert it directly. The permitted webhook-secret alphabet is
   `A-Z a-z 0-9 _ -`, with 1–256 characters.
4. Set the exact webhook URL from section 1. Keep both enable flags `false`.
5. The file also contains only the existing Django/database connection settings needed to start this Django
   process. It must not contain MT5, bridge, node, worker-identity, Telegram provider/WIMs, or execution secrets.
6. Save, then prove only metadata: the file is owned by `ubuntu:ubuntu`, mode `0600`, each required key occurs
   once, and no value is printed.
7. Recreate the backend with the new environment while both flags remain false. Do not start the dedicated
   worker. Verify Settings still says unavailable and has no Connect action.

## 4. Webhook tool (separate Sponsor activation only)

The management command reads credentials from the container environment and never prints them. From
`/home/ubuntu/guvfx-prod`, use the existing production Compose files:

```bash
docker compose run --rm --no-deps guvfx-backend python manage.py customer_telegram_webhook --register
docker compose run --rm --no-deps guvfx-backend python manage.py customer_telegram_webhook --status
```

Registration permits only Telegram `message` updates and discards pre-activation pending updates. Status
prints only whether a webhook exists, whether its URL matches the configured URL, pending count, and the
last error timestamp. It prints no URL, token, secret, chat ID, or customer identity.

After an explicit Sponsor activation decision: register the webhook, set both flags true in the protected
environment file, recreate the backend, then start only the dedicated notification worker with the overlay.
Prove a fresh ACTIVE heartbeat before exposing Connect. Never test by manufacturing a trade.

## 5. Pilot acceptance sequence

1. Sign in as `beta.guvfx01@gmail.com` (or the approved disposable acceptance customer).
2. Open Settings → Telegram and select **Connect Telegram**.
3. Open the GuvFX bot deep link and press **Start**.
4. Prove the one-use token is redeemed and the private numeric chat binding is active.
5. Prove Settings says **Connected** without exposing numeric `chat.id`.
6. Prove the connection confirmation arrives only in the pilot chat.
7. Observe one already-occurring, durable, customer-safe event; do not create a trade for the test.
8. Prove the message arrives only in the pilot chat, has the correct owner-derived account number and the
   selected EN/JA language, and contains no node, bridge, worker, job, Windows user, UUID, exception, or trace.
9. Disconnect in Settings and prove the binding is inactive.
10. Prove queued unsent delivery rechecks the binding and is suppressed after disconnect.
11. Prove the old token cannot reconnect; reconnect requires a fresh token and verified handshake.
12. Prove a new private chat can replace the binding only through that handshake, and changing a Telegram
    username does not change numeric routing.

Never use Customer Zero, support/WIMs stakeholder destinations, or a shared/global chat for this pilot.

## 6. Monitoring and beta thresholds

Aggregate health includes both flags, total/active binding counts, queue pending/processing/retrying,
delivered, definite failed, ambiguous/operator-review, retry exhaustion, oldest pending age, and the worker
heartbeat/state. It contains no raw chat ID.

- DARK (either flag false): a stopped worker and absent/stale heartbeat are expected.
- ACTIVE: worker heartbeat older than 120 seconds, or absent, is an alert.
- Oldest pending older than 900 seconds is a warning requiring notification-plane investigation.
- Any definite failure or retry exhaustion is an alert; do not restart execution.
- Any ambiguous delivery or stranded `PROCESSING` row requires operator review and must never be replayed.
- Binding count is informational; a surprise increase is investigated against authenticated connections.

## 7. Unregister, rollback, and credential rotation

For rollback: set worker flag false, set master flag false, recreate the backend, stop the dedicated worker,
then remove the webhook:

```bash
docker compose run --rm --no-deps guvfx-backend python manage.py customer_telegram_webhook --unregister
docker compose run --rm --no-deps guvfx-backend python manage.py customer_telegram_webhook --status
```

Keep the Settings Connect action unavailable. Preserve outbox/attempt evidence and the database backup. Do
not reverse the migration, replay ambiguous sends, or touch execution workers, nodes, bridges, MT5,
authorization, assignments, sizing, or trading state.

For token rotation: first perform the rollback sequence; in `@BotFather`, choose the bot and revoke/regenerate
its token; place the new token and a newly generated webhook secret into the protected environment file using
the interactive-editor procedure; recreate the backend while flags remain false; register and verify the new
webhook; then require a new Sponsor activation decision before setting flags true or starting the worker.
The revoked token must be removed from the password manager's active entry but retained only according to the
approved credential-audit policy. Existing customer bindings remain numeric and do not authorize execution.
