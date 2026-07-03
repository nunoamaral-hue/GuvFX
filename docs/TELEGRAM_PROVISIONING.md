# Telegram Account Provisioning (GFX-PKT-TELEGRAM-ACCOUNT-PROVISIONING)

Operator runbook for generating + verifying the Telethon **StringSession** for the
dedicated GuvFX Telegram account (the PH-number **GFX** account — **never** Nuno's
personal UK account). This is a **RED credential action — Nuno's hands only.** It
places no order, ingests nothing, arms no provider, and deploys nothing.

## Prerequisites (Nuno)

- The **GFX** Telegram account exists on the PH number, has joined Wayond, and is
  separate from the personal UK account. ✅ (done)
- 2FA is intentionally **off** for now (enable it *after* verification).
- `API_ID` + `API_HASH` from <https://my.telegram.org> → *API development tools*.
  These are credentials — keep them out of git/chat.

## Install (only where you run this — your machine or a scratch venv)

```bash
cd backend
pip install -r requirements-telegram.txt      # Telethon; kept out of the backend image
```

## Run

```bash
cd backend
export TELEGRAM_API_ID=...        # from my.telegram.org  (not committed, not logged)
export TELEGRAM_API_HASH=...
python manage.py provision_telegram_session \
    --session-out ~/.guvfx/telegram_gfx.session \
    --wayond-chat <@username | chat_id | t.me link for Wayond>
```

## What you type

1. **Phone**: the GFX account's PH number (`+63…`) — or set `TELEGRAM_PHONE` first.
2. **Login code**: the code Telegram sends to the GFX account (in the Telegram app).
3. (No 2FA password — it's off. If you ever enable it, Telethon will also ask for it.)

## What it prints (safe metadata only — never the session, never the phone)

- `telegram_user_id`, `display_name`, `username`
- `chat_title`, `chat_id`, `latest_message_id` (Wayond verification — read-only, one
  message id, no content)
- `session written to <path> (mode 600) — NOT printed to stdout`

## Where the session is stored

A **600-mode** file at `--session-out` (default `~/.guvfx/telegram_gfx.session`).
Treat it as a full credential:

- **Do NOT** commit it, paste it into chat/logs, or print it.
- Move it into the deploy secret store as `TELEGRAM_STRING_SESSION` in the
  600-perm `.env` (rsync-excluded — same handling as `MT5_SHADOW_WORKER_TOKEN`, see
  `docs/CREDENTIAL_ROTATION.md`).
- If you *must* view it, re-run with `--print-secret` — it prints between two loud
  warning banners; clear your terminal + shell history afterwards.

## Cleanup / revoke

- To invalidate a session: on the GFX account, **Settings → Devices/Active Sessions
  → terminate** the relevant session, then re-run this command to mint a fresh one.
- Rotate the session per the credential-rotation framework if leakage is suspected.
- **Enable 2FA on the GFX account after verification** (deferred until the session
  is confirmed working).

## Boundary

This helper only logs in + verifies + writes the session. It does **not** run the
listener, ingest messages, arm a provider, deploy, or touch execution — those are
the separate `GFX-PKT-SIGNAL-ACQUISITION-LISTENER-DEPLOY` and provider-onboarding
steps. E3 is unaffected (RED).
