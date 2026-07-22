# GuvFX secret inventory (canonical)

**Authoritative operational reference. All future rotations MUST begin here (Permanent Rule 4).**
No secret VALUE appears in this file, ever — only names, locations, owners and procedures.

Compiled 2026-07-22 from the code (`getenv`/`env(` call sites), the VPS deployment files and the Windows
host, after the bridge-token rotation. "Last rotation" is recorded only where known.

> **How to read "Authoritative location".** That is the ONE place the value is edited. Anything else is a
> consumer that reads it from there. If a secret appears in more than one authoritative place, that is a
> defect — record it in Gaps below rather than treating it as normal.

---

## A. Bridge / worker credentials (execution path)

| Secret | Purpose | Authoritative location | Consumed by | Rotation | Owner | Last rotation |
|---|---|---|---|---|---|---|
| **`GUVFX_AGENT_TOKEN`**<br>aliases: `GUVFX_WINDOWS_AGENT_TOKEN`, `WINDOWS_AGENT_TOKEN` | Inbound auth for the MT5 bridge HTTP API (`:8788`) — gates `/health`, `/mt5/order`, `/mt5/close-position`, `/mt5/modify-position`, snapshots | **Windows:** `C:\GuvFX\secrets\bridge.tokens.bat` (SYSTEM+Administrators only)<br>**VPS:** `/home/ubuntu/guvfx-prod/bridge-agent.env` (`600`) | bridge (validates); backend, trade-ingest worker, validate worker, shadow worker, wayond listener (present) | [`BRIDGE_TOKEN_ROTATION_PLAN.md`](BRIDGE_TOKEN_ROTATION_PLAN.md) — single controlled restart, intake paused | Nuno | **2026-07-22** (leak remediation) |
| **`MT5_WORKER_TOKEN`**<br>alias: `GUVFX_WORKER_TOKEN` | Worker→backend auth (`X-Worker-Token`): job polling, completion, `/api/reliability/heartbeat/` | **VPS:** `.env` + inline in `docker-compose.yml` ⚠ (see Gaps)<br>**Windows:** `C:\GuvFX\secrets\bridge.tokens.bat` | bridge (job polling), validate worker, trade-ingest worker, backend (validates) | Same shape as the agent token; bridge + all workers must be updated together | Nuno | not rotated |
| **`MT5_SHADOW_WORKER_TOKEN`** | Distinct identity for the shadow (dry-run) worker so it can never be mistaken for the live worker | **VPS:** `.env` (`600`), interpolated by the shadow compose | shadow worker only | Recreate the shadow worker after change; no bridge restart needed | Nuno | not rotated |

## B. Application / platform secrets

| Secret | Purpose | Authoritative location | Consumed by | Rotation | Owner | Last rotation |
|---|---|---|---|---|---|---|
| `DJANGO_SECRET_KEY` | Django signing (sessions, CSRF, tokens) | VPS `.env`, `wayond-listener.env`, inline compose ⚠ | backend, listener | Rotating invalidates sessions/CSRF — schedule a window | Nuno | unknown |
| `JWT_SECRET_KEY` | JWT signing for cookie auth | VPS `.env`, inline compose ⚠ | backend | Rotating logs every user out | Nuno | unknown |
| `DB_PASSWORD` / `POSTGRES_PASSWORD` | PostgreSQL auth | VPS `.env`, `wayond-listener.env`, inline compose ⚠ | backend, listener, postgres | Change in DB + every consumer atomically | Nuno | unknown |
| `MT5_CRED_FERNET_KEY` | Encrypts stored MT5 broker credentials at rest | VPS `.env`, inline compose ⚠ | backend | **Destructive** — existing ciphertexts must be re-encrypted first | Nuno | unknown |
| `GUVFX_FERNET_KEY` | Fernet key (general encryption) | inline compose ⚠ | backend | As above | Nuno | unknown |
| `GUAC_JSON_SECRET_KEY_HEX` | Guacamole JSON-auth signing (MT5 remote desktop) | VPS `.env`, inline compose ⚠ | backend, Guacamole stack | Must change on both sides together | Nuno | unknown |

## C. Messaging / provider secrets

| Secret | Purpose | Authoritative location | Consumed by | Rotation | Owner | Last rotation |
|---|---|---|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Outbound notification bot | VPS `telegram.env` (`600`), `.env` | backend, reliability alerts | Reissue via BotFather; update both files | Nuno | unknown |
| `RELIABILITY_TELEGRAM_BOT_TOKEN` | Ops/alert bot (may equal the above) | VPS env | reliability alerting | As above | Nuno | unknown |
| `TELEGRAM_API_HASH` | Telethon app credential for the signal listener | VPS `wayond-listener.env` (`600`) | wayond listener | Reissue at my.telegram.org | Nuno | unknown |
| `TELEGRAM_STRING_SESSION` | Telethon **session** (equivalent to a logged-in account) | VPS `wayond-listener.env` (`600`) | wayond listener | Re-provision via `provision_telegram_session`; see [[telegram de-auth]] history | Nuno | unknown |

## D. Beta provisioning (dark — not yet installed)

| Secret | Purpose | Authoritative location | Consumed by | Rotation | Owner | Last rotation |
|---|---|---|---|---|---|---|
| `BETA_AGENT_KEYRING` / `BETA_AGENT_KEY_ID` | HMAC keyring for the signed beta provisioning channel (`key_id` supports rotation without downtime) | Not yet provisioned — B3 install only | beta provisioning agent (Windows), backend beta worker | Add a new `key_id`, roll clients, retire the old id | Nuno | n/a |

---

## Gaps and follow-ups (recorded, not silently accepted)

1. **⚠ Secrets inline in `docker-compose.yml`** (`DB_PASSWORD`, `DJANGO_SECRET_KEY`, `JWT_SECRET_KEY`,
   `MT5_CRED_FERNET_KEY`, `GUVFX_FERNET_KEY`, `GUAC_JSON_SECRET_KEY_HEX`, `MT5_WORKER_TOKEN`,
   `POSTGRES_PASSWORD`). The agent token was moved to `bridge-agent.env` during the 2026-07-22 rotation;
   the rest still live in the compose file, so each has **two** locations. Recommend consolidating each into
   a `600` env file referenced by `env_file:`.
2. **`backend/.env` holds a stale 40-char `GUVFX_AGENT_TOKEN`** that is not the current value and not the
   leaked one. It is inert (`load_dotenv` does not override container env) but should be removed.
3. **Duplicate authoritative locations** for `DJANGO_SECRET_KEY` / `DB_PASSWORD` (`.env`,
   `wayond-listener.env`, compose). A rotation that misses one will half-break the estate.
4. **"Last rotation" is unknown for most secrets** — populate as each is next rotated.
5. `MT5_WORKER_TOKEN` was **not** rotated on 2026-07-22 (it was not leaked) but shares the storage that was
   hardened; rotating it is recommended follow-up work.

## Rotation ground rules (from the 2026-07-22 exercise)

- Generate **once**, in the owner's own secure session; install the *same* value everywhere. Never paste a
  secret into chat, argv, shell history, logs or evidence.
- Verify cross-host agreement by **length + a short SHA-256 prefix only**.
- Enumerate consumers from **this inventory** before starting (Permanent Rule 4) — the 2026-07-22 window
  initially missed the shadow worker, and found 13 more plaintext copies than expected.
- Pause signal intake for any rotation touching the execution path: a client `401` becomes a **terminal**
  `PROMOTION_REJECTED` with no replay, not a retry.
- Never roll back to a file containing the revoked value.
