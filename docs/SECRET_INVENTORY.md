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

> **⚠ Read this before rotating anything in this section.** `GUVFX_AGENT_TOKEN` and
> `GUVFX_WINDOWS_AGENT_TOKEN` are **NOT aliases**, despite currently holding the same value. The codebase
> describes them as *different services'* credentials (legacy `:8787` agent vs `:8788` bridge) and warns
> "sending the wrong token returns 401" — see `backend/strategies/signal_engine.py`,
> `backend/research/data_loader.py`, `backend/mt5/services/windows_agent.py`. They are equal in production
> only because (a) the Windows **bridge validates the env name `GUVFX_AGENT_TOKEN`** while the **backend
> sends `GUVFX_WINDOWS_AGENT_TOKEN`**, forcing them equal across that boundary, and (b) **no `:8787` agent
> is deployed** (port not listening; no file on the host references 8787). This is a recorded **Rule-3
> conflation** (Gap 6), not a licence to treat them as one secret.

| Secret | Purpose | Authoritative location | Consumed by | Rotation | Owner | Last rotation |
|---|---|---|---|---|---|---|
| **`GUVFX_AGENT_TOKEN`** (bridge-side name) | The value the **bridge** validates for inbound auth on `:8788` — gates `/health`, `/mt5/order`, `/mt5/close-position`, `/mt5/modify-position`, snapshots. Same env name is ALSO what `windows_agent.py` would send to the legacy `:8787` agent (not deployed). | **Windows:** `C:\GuvFX\secrets\bridge.tokens.bat` (SYSTEM+Administrators only) | bridge (**validates**); `bridge_watchdog.ps1` (health probe); launchers `start_signal_bridge.bat`, `guvfx_autostart.bat`, `guvfx_autostart_bridge_only.bat`, `start_signal_bridge_is6.bat` (all `call` the secrets file) | [`BRIDGE_TOKEN_ROTATION_PLAN.md`](BRIDGE_TOKEN_ROTATION_PLAN.md) — single controlled restart, intake paused | Nuno | **2026-07-22** (leak remediation) |
| **`GUVFX_WINDOWS_AGENT_TOKEN`**<br>alias: `WINDOWS_AGENT_TOKEN` | The value **clients send** to the `:8788` bridge in `X-GuvFX-Agent-Token`. **Must equal the bridge-side value above** or every call 401s. | **VPS:** `/home/ubuntu/guvfx-prod/bridge-agent.env` (`600`) | backend, trade-ingest worker, validate worker, shadow worker, wayond listener | Rotate **together with** the bridge-side value — they are one wire-level credential under two names | Nuno | **2026-07-22** |
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
6. **⚠ Rule-3 conflation — `GUVFX_AGENT_TOKEN` (legacy `:8787` agent) and `GUVFX_WINDOWS_AGENT_TOKEN`
   (`:8788` bridge) share one value.** Harmless today only because no `:8787` agent is deployed. If one is
   ever stood up it will inherit the bridge's credential, which is exactly the substitution Rule 3 forbids.
   De-conflation plan: give the `:8787` agent its own secret at the moment it is deployed, and split these
   into two independently-rotatable rows.
7. **⚠ Rule-3 violation — `backend/trading/crypto.py::_get_fernet` derives the MT5-credential encryption key
   from `DJANGO_SECRET_KEY`** when `GUVFX_FERNET_KEY` is unset (`sha256(DJANGO_SECRET_KEY)`). A signing key
   and an encryption key are different secrets with different blast radii. **Not fixed here on purpose:**
   changing it is destructive — every stored MT5 credential ciphertext must be re-encrypted first, and
   rotating `DJANGO_SECRET_KEY` today would silently render those credentials undecryptable. Needs its own
   packet (set `GUVFX_FERNET_KEY` explicitly, re-encrypt, then remove the fallback).
8. **Cross-credential fallbacks remain in ~12 backend call sites** (see the post-incident review §7). WS1
   fixed the bridge, the validate worker and `sync_broker_instruments`; the rest are recorded, not done.

## Rotation ground rules (from the 2026-07-22 exercise)

- Generate **once**, in the owner's own secure session; install the *same* value everywhere. Never paste a
  secret into chat, argv, shell history, logs or evidence.
- Verify cross-host agreement by **length + a short SHA-256 prefix only**.
- Enumerate consumers from **this inventory** before starting (Permanent Rule 4) — the 2026-07-22 window
  initially missed the shadow worker, and found 13 more plaintext copies than expected.
- Pause signal intake for any rotation touching the execution path: a client `401` becomes a **terminal**
  `PROMOTION_REJECTED` with no replay, not a retry.
- Never roll back to a file containing the revoked value.
