# Bridge agent-token rotation plan (controlled, single restart)

**Status: PLAN — not executed.** Requires Nuno's maintenance-window approval for the one bridge restart.
No token value (old or new) appears in this document, in any command captured in logs, in commit messages,
or in verification evidence.

**Why rotation is the control.** The leaked agent token is in **public Git history** (`67de147`). Removing the
literals from tracked docs does **not** revoke it. Only rotation invalidates it.

---

## 0. Verified current state (read-only)

| Where | What holds the credential | Permissions found |
|---|---|---|
| Windows bridge | `C:\GuvFX\start_signal_bridge.bat` — sets `GUVFX_AGENT_TOKEN` **and** `GUVFX_WORKER_TOKEN` inline | **`BUILTIN\Users:(I)(RX)` — any local user can read it** ⚠ |
| Windows machine env | *(none — no machine-level TOKEN variables exist)* | n/a |
| VPS | `/home/ubuntu/guvfx-prod/docker-compose.yml` — token **inline** in three services: `guvfx-backend`, `guvfx-mt5-trade-ingest-worker`, `guvfx-mt5-validate-worker` | **`664` (world-readable)** ⚠ |
| VPS | `/home/ubuntu/guvfx-prod/wayond-listener.env` — `GUVFX_AGENT_TOKEN`, `GUVFX_WINDOWS_AGENT_TOKEN`, `WINDOWS_AGENT_TOKEN` | **`664` (world-readable)** ⚠ |
| VPS | `telegram.env` | `600` (correct — no token keys) |

Bridge start path: scheduled task **`GuvFX_SignalBridge`** runs `start_signal_bridge.bat` (Administrator /
Interactive); a **`GuvFX_BridgeWatchdog`** task also exists and may restart the bridge — it must be accounted
for so it cannot resurrect the process with stale configuration mid-rotation.

> Note: `GUVFX_WORKER_TOKEN` (bridge → backend job polling) is a **different** credential and was **not** in the
> leak. It shares the same weak storage, so it is covered by the permission hardening below; rotating it is
> recommended as separate follow-up work, not bundled into this revocation.

## 1. Generating the new token
Generated **on the machine that will hold it**, never pasted through chat:
`python -c "import secrets; print(secrets.token_urlsafe(48))"` → ≥ 64 chars, URL-safe, CSPRNG-backed.
It is written directly into the target files by the operator; it is never echoed to a terminal, never passed as
a command-line argument (visible in process listings), and never placed in shell history (prefix commands with a
space, or use an editor).

## 2. Authoritative secret-storage location
- **Windows (authoritative for the bridge):** `C:\GuvFX\secrets\bridge.tokens.bat`, a new file containing only
  the `set GUVFX_AGENT_TOKEN=…` / `set GUVFX_WORKER_TOKEN=…` lines, **ACL-restricted to SYSTEM + Administrators
  only**. `start_signal_bridge.bat` gains a single `call C:\GuvFX\secrets\bridge.tokens.bat` and keeps **no
  credential of its own**. This removes the `BUILTIN\Users:(RX)` exposure and — importantly — means the future
  B3P-2 beta pool identities can never read the bridge's credentials.
- **VPS (authoritative for clients):** a single `bridge-agent.env` (mode `600`, `ubuntu:ubuntu`) referenced via
  `env_file:`. The **inline** `docker-compose.yml` token values are removed, so the credential lives in exactly
  one file per host rather than being duplicated across three service definitions.

## 3. Secure delivery to the Windows bridge process
Operator edits `C:\GuvFX\secrets\bridge.tokens.bat` over the existing admin SSH/RDP session, sets the ACL, and
verifies presence **without printing the value** (see §6). The bridge reads it from the environment at start-up;
nothing else on the box needs the value.

## 4. Secure delivery to every legitimate client
Exactly four consumers must be updated **before** the restart:
1. `guvfx-backend` 2. `guvfx-mt5-trade-ingest-worker` 3. `guvfx-mt5-validate-worker` — all three via the new
   `bridge-agent.env` (`GUVFX_WINDOWS_AGENT_TOKEN`, `WINDOWS_AGENT_TOKEN`, `GUVFX_AGENT_TOKEN` as the code reads
   all three names), and 4. the **wayond listener** (`wayond-listener.env`).
Containers are recreated to pick up the new env. Ordering: **clients updated and recreated first**, bridge
restarted last — the bridge is the authority, so a client presenting the new token before the bridge accepts it
simply gets 401s and retries, whereas the reverse would black-hole every client at once.

## 5. File and secret permissions (fixes the findings above)
- Windows: `icacls C:\GuvFX\secrets /inheritance:r /grant SYSTEM:(OI)(CI)F /grant Administrators:(OI)(CI)F`
  — **no `BUILTIN\Users` ACE**. Verify with `icacls` that Users is absent.
- VPS: `chmod 600 bridge-agent.env wayond-listener.env`; `chmod 640 docker-compose.yml` once it no longer
  contains a credential. Verify with `stat -c %a`.

## 6. Configuration-presence validation BEFORE restart
Prove configuration is present and non-empty **without revealing it** — length + digest only:
- Windows: `powershell -c "$v=$env:GUVFX_AGENT_TOKEN; if(!$v){'ABSENT'}else{'len='+$v.Length}"` evaluated in a
  shell that has sourced the tokens file (never `echo $env:GUVFX_AGENT_TOKEN`).
- VPS: `awk -F= '/^GUVFX_AGENT_TOKEN=/{print "len=" length($2)}' bridge-agent.env`.
- **Match check without disclosure:** compare a SHA-256 of the value on each side (e.g.
  `sha256sum <<<"$TOKEN" | cut -c1-12`) and confirm the truncated digests are identical across the bridge file
  and every client file. Digests are recorded as evidence; the value never is.
- Hard gate: if any side reports `ABSENT`, `len=0`, or a mismatched digest → **abort, do not restart.**

## 7. Rollback — using the NEW configuration only
If post-restart verification fails, roll back to the **previous known-good bridge script and the *new* token**,
never to the leaked value:
- Keep a timestamped copy of `start_signal_bridge.bat` (and the prior `mt5_signal_bridge.py`) taken immediately
  before the change; restoring those restores *behaviour*, while the tokens file continues to supply the **new**
  credential. **The leaked token is never reinstated under any failure mode** — that would un-revoke it.
- If the bridge will not start with the new configuration, the correct action is to fix the configuration and
  restart again, not to revert the credential.

## 8. No process may start with an absent or empty token
Guaranteed in code by the merged fail-closed change (`scripts/mt5_signal_bridge.py`):
- `HTTP_AUTH_TOKEN = AGENT_TOKEN or WORKER_TOKEN`, both `.strip()`ed → missing/empty/whitespace all normalise
  to "not configured".
- `validate_config()` returns False when it is unset, and `main()` exits `1` — the bridge **refuses to start**.
- `_validate_token()` denies every request if it is unset, and uses `hmac.compare_digest` otherwise.
- Regression-locked by `backend/execution/tests_bridge_auth.py` (8 tests), including a guard that no
  `return True` can reappear in the validator.

## 9. Execution order (single restart)
1. Merge this branch (fail-closed code + repo remediation) — CI green.
2. **Deploy the exact merged `mt5_signal_bridge.py` to disk on the box — do NOT restart.**
3. Create the Windows tokens file + ACLs; update the VPS env file + compose; set permissions.
4. Run §6 presence/digest validation on **every** host. Abort on any failure.
5. Recreate the four client containers (they begin presenting the new token; 401s until step 7 — expected).
6. **Obtain Nuno's maintenance-window approval.** Disable `GuvFX_BridgeWatchdog` for the window so it cannot
   restart the bridge with stale config mid-rotation.
7. **One controlled bridge restart** — picks up the fail-closed code *and* the new token together.
8. Re-enable the watchdog. Run §10 proof.

## 10. Post-rotation proof (all must pass)
1. Bridge returns healthy with the **new** credential (`200 {"ok": true, …}`).
2. **Missing** credential → `401`.
3. **Incorrect** credential → `401`.
4. **The leaked credential → `401`** (revocation proven).
5. Protected `POST` routes (`/mt5/order`, `/mt5/close-position`, `/mt5/modify-position`) still require auth —
   verified by an **unauthenticated** probe returning `401`. *No order is placed: authentication is rejected
   before any MT5 call, and no authenticated order request is issued as part of this proof.*
6. Bridge process, wayond listener and trade-ingest worker healthy (no auth errors in logs after the window).
7. Nuno's MT5 terminal and **open positions unaffected** — position count/tickets identical before and after.
8. No unauthenticated fallback: with the token deliberately unset in a **scratch** process (never the live one),
   the bridge refuses to start — evidence recorded from the test suite, not from the production process.

## 11. Out of scope
**Git history is not rewritten** (per Nuno). Rotation is the revocation; history hygiene may be considered
separately and must not delay it.
