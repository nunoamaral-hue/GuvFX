# Engineering Validation Account — policy (TEMPORARY, non-production)

**Packet:** GFX-PKT-TELEGRAM-SESSION-VALIDATION-STRATEGY (Phase 2). Authorised use of a
**temporary engineering validation account** to validate the LIVE Telethon
listener/acquisition pipeline while the dedicated **GFX** account's session cannot yet
persist (see [[project-telegram-session-deauth]] / `docs/TELEGRAM_PROVISIONING.md`).

## What this is / is NOT
- **IS:** a stopgap to prove the live code path works (session persistence, `get_me()`,
  channel visibility, catch-up, event stream, normalisation, dispatch/classify) end to
  end — the one thing fixture mode cannot prove.
- **IS NOT:** the production signal account. Production remains **GFX** (aged, real SIM,
  2FA), retried later. Nuno's existing account is used here **only** for engineering
  validation.

## Hard guardrails
- **No secrets committed** — API creds and the StringSession never enter git/chat/logs.
- **Separate session file** — `~/.guvfx/eng_validation.session` (0600), distinct from
  the GFX session; never the production path.
- **No provider arming** — validation runs `--live --dry-run` (preview only, no writes)
  or against a NON-armed provider (messages `DROPPED_NOT_ARMED`, safe). Nothing is
  intaken/traded.
- **No permanent configuration** — no committed env, no deployed service, no armed
  provider left behind.
- **Read-only** — the listener never sends/downloads (enforced by its boundary tests).
- **No E3 / no order_send / no execution change.**

## Revoke after validation (mandatory)
When validation is done:
1. On the engineering account: **Settings → Devices → terminate** the validation
   session.
2. Delete the local file: `rm -f ~/.guvfx/eng_validation.session`.
3. Remove any throwaway validation provider row (or leave it non-armed/inactive).
4. Do **not** reuse the personal account for production.

## Residual risk (accepted, temporary)
A StringSession is a full-account credential — for the duration of validation, the
engineering account's session exists locally. Keep it 0600, never in the deploy store,
delete it immediately after. This is why it is temporary and non-production.
