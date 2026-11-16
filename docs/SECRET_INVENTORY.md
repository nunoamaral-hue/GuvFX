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
| **`GUVFX_AGENT_TOKEN` + `GUVFX_WORKER_TOKEN` — SHARED to the node 2 bridge (⚠ TEMPORARY Closed-Beta exception, 2026-08-15)** | The node 2 (Closed-Beta) pin-enforcing order bridge on `:8789` **reuses Customer Zero's inbound + worker tokens** by `call`-ing the same `C:\GuvFX\secrets\bridge.tokens.bat`. This is the Rule-3 substitution normally forbidden; accepted here as an **explicit, recorded, time-boxed** exception approved by the Sponsor (2026-08-15) solely to let Beta User #1 complete the end-to-end acceptance journey. Contained because order routing sends a beta order only to `:8789` and a CZ order only to `:8788`, and the per-job **identity pin** is the real per-tenant order authority. | same file, read-only (via `C:\GuvFX\node2\start_node2_bridge.bat`) | node 2 bridge `C:\GuvFX\node2\mt5_signal_bridge.py` + `node2_bridge_watchdog.ps1` | **Retire with the per-node inbound-token model (Post-Beta)** — see Gap 9 + `deploy/node2-order-bridge/README.md` | Nuno | n/a (shared value; no separate secret minted) |

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

## D. Signed host-executor + beta provisioning keyrings (LIVE)

> HMAC `{key_id: secret}` maps. Rotate **without downtime** via dual-key overlap: ADD a new `key_id`
> everywhere (old stays active) → recreate/restart consumers → prove new accepted + old still accepted →
> switch the active `*_KEY_ID` to the new id → prove new live + old still accepted → REMOVE the old id
> everywhere → prove old rejected. A governed, value-blind operator rotator exists (VPS
> `_rotation/rotation_ring_op.py`, Windows `C:\GuvFX\_rotation\Rotate-GuvfxRingMachineVar.ps1`, driven by a
> hidden-input wrapper): it never places a secret in argv/log/history and only reports id/count metadata.

| Secret | Purpose | Authoritative location | Consumed by | Rotation | Owner | Last rotation |
|---|---|---|---|---|---|---|
| `HOSTED_EXECUTOR_KEYRING` / `HOSTED_EXECUTOR_KEY_ID` | HMAC keyring for the Stream-5 signed host-executor transport (backend **signs** requests + verifies signed responses; the Windows host executor **verifies** + signs responses) | **VPS:** `/home/ubuntu/guvfx-prod/hosted-executor.env` (`600`); **Windows (GFX-144 / ADR-0112):** FILE-FIRST `C:\GuvFX\hosted\secrets\hosted_executor_keyring.json` (ACL: inheritance off; read only by SYSTEM / Administrators / the `GuvFXHostedExecutor` service principal), with the machine env var (host `WIN-RD8VDS93DK7`) as the DEPRECATED migration fallback used only when the file is absent | `guvfx-backend` (signer), Windows service `GuvFXHostedExecutor` (verifier) | Dual-key overlap (above). Update the VPS env file + the Windows machine var together (same bytes); recreate `guvfx-backend`; then **write the host keyring file** with the value-blind `deploy/hosted-executor/Provision-GuvfxHostedExecutorKeyringFile.ps1` + `Restart-Service GuvFXHostedExecutor` (**NO reboot** - ADR-0112 fixes the SCM boot-time env-cache trap; a bare `Restart-Service` alone does **NOT** pick up a machine-var change on a host not rebooted since the rotation, which caused GFX-143's `unknown_key_id`). Rollback: re-provision from the reverted machine var, or `-Remove` the file to fall back to env | Nuno | **2026-11-15** (post-exposure rotation; active id `hx-20261115`) |
| `BETA_AGENT_KEYRING` / `BETA_AGENT_KEY_ID` | HMAC keyring for the signed beta provisioning channel (`key_id` supports rotation without downtime) | **VPS:** `/home/ubuntu/guvfx-prod/beta.env` (`600`) + `deploy/beta-provisioner/beta-provisioner.secret.env` (`600`); **Windows:** machine env var | `guvfx-backend`, `guvfx-beta-provisioner`, Windows service `GuvFXBetaAgent` (verifier) | Dual-key overlap (above). Update **all three** VPS files + the Windows machine var together; recreate `guvfx-backend` + `guvfx-beta-provisioner` + `Restart-Service GuvFXBetaAgent` | Nuno | **2026-11-15** (post-exposure rotation; active id `beta-20261115`) |

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
   rotating `DJANGO_SECRET_KEY` today would silently render those credentials undecryptable. Tracked as
   **[SEC-CRYPTO-001](SEC-CRYPTO-001.md)** — authorised as its own packet, explicitly out of scope for B3P-2.
8. **Cross-credential fallbacks remain in ~12 backend call sites** (see the post-incident review §7). WS1
   fixed the bridge, the validate worker and `sync_broker_instruments`; the rest are recorded, not done.
9. **⚠ TEMPORARY Closed-Beta Rule-3 exception (2026-08-15) — the node 2 order bridge (`:8789`) shares Customer
   Zero's `GUVFX_AGENT_TOKEN` + `GUVFX_WORKER_TOKEN`.** Sponsor-approved, time-boxed to the first supervised
   beta user's acceptance journey; contained by node-keyed order routing (`:8789` vs `:8788`) + the per-job
   identity pin (the real per-tenant order authority). **Post-Beta remediation:** implement the per-node
   inbound-token model (a distinct token per `TerminalNode`, threaded through the dispatch worker), mint the
   node 2 bridge its own `GUVFX_AGENT_TOKEN` + its own `GUVFX_WORKER_ID`/`SECRET`, split into its own rows,
   and retire the shared value. Then deactivate + re-activate the bridge with the distinct secret. See
   `deploy/node2-order-bridge/README.md`.

## Rotation ground rules (from the 2026-07-22 exercise)

- Generate **once**, in the owner's own secure session; install the *same* value everywhere. Never paste a
  secret into chat, argv, shell history, logs or evidence.
- Verify cross-host agreement by **length + a short SHA-256 prefix only**.
- Enumerate consumers from **this inventory** before starting (Permanent Rule 4) — the 2026-07-22 window
  initially missed the shadow worker, and found 13 more plaintext copies than expected.
- Pause signal intake for any rotation touching the execution path: a client `401` becomes a **terminal**
  `PROMOTION_REJECTED` with no replay, not a retry.
- Never roll back to a file containing the revoked value.
