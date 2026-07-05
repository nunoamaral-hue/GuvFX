# Wayond Listener — Migration: Personal → Dedicated GFX Account

The listener runs on Nuno's **personal** account as a **temporary operational
exception** (GFX-PKT-TEMPORARY-PRODUCTION-ACCOUNT-DEPLOYMENT) while the dedicated **GFX**
account ages. The target production architecture is the dedicated GFX account. This
migration is designed to require **no code changes** — only a new session, a secret
replacement, and a service restart.

## Why it's code-free
The listener reads its session from `TELEGRAM_STRING_SESSION` (+ the frozen device
fingerprint env) — the account identity is entirely a **secret**, not code. So swapping
the account = swapping the secret. The provider, parser, dispatcher, and listener code
are account-agnostic. The provider's `telegram_chat_id` is the same (the **same** Wayond
channel), so no provider change is needed either.

## Prerequisites (before migrating)
- **GFX session persists**: GFX aged 7–14 days (clean, un-hammered), 2FA enabled, and a
  freshly-minted GFX `StringSession` verified to survive reuse (`is_user_authorized()==True`
  on reload and still True after ~15 min) — the same persistence bar the personal account
  passed. Do NOT migrate until GFX holds a session.

## Migration steps (operator — Nuno)
1. **Mint the GFX production session** (2FA on, frozen fingerprint):
   ```bash
   cd backend
   export TELEGRAM_API_ID=<GFX app id> TELEGRAM_API_HASH=<GFX app hash>
   export TELEGRAM_DEVICE_MODEL="Desktop" TELEGRAM_SYSTEM_VERSION="Windows 10" TELEGRAM_APP_VERSION="4.16.8"
   python manage.py provision_telegram_session --session-out ~/.guvfx/gfx_prod.session --wayond-chat <wayond>
   ```
   Verify persistence (reuse + ~15 min).
2. **Replace the secret** in the listener env file (the as-deployed secret store is
   `/home/ubuntu/guvfx-prod/wayond-listener.env`, 600): swap `TELEGRAM_STRING_SESSION`
   (via scp, as in `deploy/wayond-listener/DEPLOY_ISOLATED.md` Phase 3) and
   `TELEGRAM_API_ID/HASH` to the GFX app's, keeping the **same** device fingerprint values.
   Personal-account values are removed.
3. **Recreate the container** (env changes require a recreate, not a `restart`, which
   reuses the old env — no rebuild needed):
   ```bash
   docker rm -f guvfx-wayond-listener
   # then re-run the Phase 4 `docker run …` block from DEPLOY_ISOLATED.md
   ```
4. **Verify**: `docker logs -f guvfx-wayond-listener` → `connected` → catch-up →
   `heartbeat: state=listening`; container health goes healthy within `start_period`.
5. **Decommission the personal account exception**: terminate the personal session
   (Settings → Devices), remove it from the secret store, and (optionally) delete the
   personal `my.telegram.org` app. Record the migration in `docs/STATUS.md`.

## Rollback of the migration
If the GFX session fails to persist after cut-over: revert `TELEGRAM_STRING_SESSION`
(and api id/hash) in `wayond-listener.env` to the personal-account values and recreate the
container. No code change either way. Keep the personal session only until GFX is proven,
then remove it.

## Invariant
Across the exception and the migration, the listener stays **read-only** and the provider
stays **UN-ARMED** — acquisition-only, no execution, no order, E3 RED.
