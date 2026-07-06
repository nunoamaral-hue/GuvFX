# GFX Migration Readiness — Personal → Dedicated GFX Telegram Account

> **Packet:** GFX-PKT-GFX-MIGRATION-READINESS (readiness/preparation only — **not** the migration).
> **As of:** 2026-07-06. **Verdict: NOT READY — migration BLOCKED on GFX account aging.**
> The mechanical procedure lives in [WAYOND_LISTENER_MIGRATION.md](WAYOND_LISTENER_MIGRATION.md)
> and [deploy/wayond-listener/DEPLOY_ISOLATED.md](../deploy/wayond-listener/DEPLOY_ISOLATED.md);
> this document is the **go/no-go assessment + consolidated checklist**.
> Invariant throughout: listener stays **read-only**, provider stays **UN-ARMED**, **E3 RED**.

## 1. Current temporary-account state (production)
The Wayond listener is **live** in prod under the authorised temporary exception:
- Container `guvfx-wayond-listener` (image `guvfx-wayond-listener:latest`) — last observed
  `Up (healthy)`, `RestartCount 0`, heartbeat listening (soak packet).
- Identity: Nuno's **personal** account (id 1480569629, `@NunoRAmaral`, DC4), 2FA on, session
  in the prod secret store `/home/ubuntu/guvfx-prod/wayond-listener.env` (600) only.
- Provider `wayond` = **ONBOARDING (un-armed)** → all messages `DROPPED_NOT_ARMED`; 0
  PendingSignalApproval; acquisition read-only; no execution, no order_send.
- This is an **operational exception** ("demo urgency"); the target production identity is the
  dedicated GFX account. A fresh read-only re-check is available (soak-monitor probes) on request.

## 2. GFX account maturity assessment
GFX = id **8661920471**, `@guvfx`, home **DC5**, a **real SIM** Nuno controls, **2FA enabled**,
**subscribed to Wayond** (reads it in the official app). History (see [[project-telegram-session-deauth]]):
- 2026-07-03: multiple API logins → Telegram **de-authorised the session server-side** each time
  (`authorised=False` on reuse) + a full official-app logout — new-account anti-abuse (~3 resets that day).
- 2026-07-05: one hardened retry (device fingerprint + 2FA, ~2 days aged) — session held
  `authorised=True` for ~15 min then **de-authorised again**. Root cause = **account age/trust**,
  not code (the personal account, aged, held its session perfectly — our code is proven sound).

**Aging clock:** the clean-aging window runs from the **last API login = 2026-07-05**. Every API
login RESETS it (and raises ban risk). At 2026-07-06 GFX is only **~1 day** into aging.
- Earliest single hardened attempt: **~2026-07-12** (7 clean days).
- Ideal: **~2026-07-19** (14 clean days).
Until then: **no API logins on GFX** — official-app use only.

## 3. GFX readiness checklist (gates)
| # | Precondition | Status (2026-07-06) |
|---|---|---|
| 1 | GFX aged **7–14 days clean** from the last API login (2026-07-05) | ❌ ~1 day — **not met** |
| 2 | **No repeated API login attempts** during aging | ⏳ in progress — must stay clean |
| 3 | GFX **2FA enabled** | ✅ met |
| 4 | GFX **subscribed to Wayond** | ✅ met |
| 5 | GFX **sees Wayond messages** in the official app | ✅ met |
| 6 | GFX session **minted** with the frozen fingerprint (Desktop/Windows 10/4.16.8) | ⛔ not attempted (blocked by #1) |
| 7 | Session **persists on reload** (`authorised=True`) | ⛔ pending #6 |
| 8 | Session **persists after ~15 min** | ⛔ pending #6 (the 2026-07-05 attempt failed here) |
| 9 | Ideally **persists after ~1 hour** | ⛔ pending #6 |

**Go/no-go: NO-GO.** Gate #1 blocks; #6–9 cannot be attempted until #1 is met (attempting early
resets the clock and risks a ban). Re-assess on/after **2026-07-12** (ideally **2026-07-19**).

## 4. Migration procedure (when all gates pass — a FUTURE execution packet, Nuno-gated)
This is a **secret swap + recreate**, no code change (identity is a secret, not code; same Wayond
chat_id → same provider). Detailed steps: [WAYOND_LISTENER_MIGRATION.md](WAYOND_LISTENER_MIGRATION.md).
1. **Mint the GFX session (local, Nuno's hands, requires his explicit login confirmation):**
   ```bash
   cd ~/Documents/Programming/Python/trading/guvfx/backend
   read -p "TELEGRAM_API_ID: " id; export TELEGRAM_API_ID="$id"
   read -sp "TELEGRAM_API_HASH: " h; echo; export TELEGRAM_API_HASH="$h"
   export TELEGRAM_DEVICE_MODEL="Desktop" TELEGRAM_SYSTEM_VERSION="Windows 10" TELEGRAM_APP_VERSION="4.16.8"
   /Users/nunoamaral/Documents/Programming/Python/trading/guvfx/.venv/bin/python \
     manage.py provision_telegram_session --session-out ~/.guvfx/gfx_prod.session --wayond-chat -1003842321905
   ```
   (Absolute venv python — `python` is pyenv-shimmed. Use the GFX app's api id/hash.)
2. **Verify persistence** — reuse in a separate process (`diag_session.py`, `SESSION_PATH=~/.guvfx/gfx_prod.session`):
   expect `authorised=True` on reload, again after ~15 min, ideally after ~1 hour. **Abort if it ever flips False.**
3. **Swap the secret** in `/home/ubuntu/guvfx-prod/wayond-listener.env` (600): replace only
   `TELEGRAM_STRING_SESSION` (scp the new session, `printf 'TELEGRAM_STRING_SESSION=%s\n' "$(cat …)"`),
   `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` with the GFX values; keep the **same** device fingerprint
   + DB creds. Never print the session.
4. **Recreate the container** (env change needs a recreate, not `restart`):
   `docker rm -f guvfx-wayond-listener` → re-run the Phase 4 `docker run …` block from DEPLOY_ISOLATED.md.

## 5. Verification procedure (post-migration)
- `docker ps --filter name=guvfx-wayond-listener` → `Up (healthy)`.
- `docker logs guvfx-wayond-listener` → `connected (read-only)` → catch-up → `state=listening`.
- Read-only DB check: provider still `ONBOARDING`, `AcquiredMessage` outcomes `DROPPED_NOT_ARMED`,
  **0 PendingSignalApproval**, ExecutionJob unchanged. (Same VERIFY as the go-live/soak packets.)
- Confirm the listener is connected as **GFX** (DC5), not the personal account.

## 6. Rollback procedure (if GFX fails to persist after cut-over)
Revert the three `TELEGRAM_*` lines in `wayond-listener.env` to the **personal-account** values and
recreate the container (`docker rm -f` + Phase 4 `docker run`). No code change. **Keep the personal
session in the secret store until GFX is proven to hold for ≥1 hour post-cutover** — do not
decommission it before then.

## 7. Cleanup / decommission (only after GFX is proven stable)
- Terminate the **personal** session (Telegram → Settings → Devices → end that session).
- Remove the personal `TELEGRAM_STRING_SESSION`/api values from the secret store (they were already
  overwritten in step 3; verify none linger in backups).
- Optionally delete the personal `my.telegram.org` app.
- Record the cutover in `docs/STATUS.md`; update [[project-wayond-telegram-strategy]].

## 8. Responsibilities
**Nuno (his hands — RED / credential / login actions):** age GFX (calendar days + light official-app
use, **no API logins**); confirm GFX readiness against §3; mint the GFX session (explicit login
confirmation); swap the secret; recreate; verify; decommission the personal account.
**Claude (repo/docs/assessment only):** maintain this readiness doc + the runbooks; track the aging
clock; provide the exact (secret-free) command blocks + read-only verification probes; run in-repo
checks. **Claude performs no Telegram login, handles no secret, arms no provider, changes no execution.**

## 9. Risks
- **Premature attempt** resets the aging clock and risks a ban — the single biggest risk; do not
  log GFX into any API client before ~2026-07-12.
- **GFX still fails at 7–14 days** — possible (age is probabilistic). Mitigation: extend to 14+ days;
  the personal-account exception can continue meanwhile (rollback path is proven).
- **Session/secret mishandling** — mitigated by scp + `wayond-listener.env` (600), never printed.
- **Decommissioning the personal account too early** — keep it until GFX holds ≥1 hour post-cutover.
