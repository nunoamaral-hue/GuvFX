# Bridge agent-token rotation plan (controlled, single restart) — **rev 2**

**Status: PLAN — not executed.** Requires Nuno's maintenance-window approval, obtained **before any file is
touched**. No token value (old or new) appears in this document, in commands captured in logs, in argv/process
listings, in commit messages, or in verification evidence.

**Why rotation is the control.** The leaked agent token is in **public Git history** (`67de147`). Removing the
literals does **not** revoke it. Only rotation does.

> **Rev 2 supersedes rev 1 after an adversarial security review found 6 MUST_FIX defects in it.** The most
> serious: rev 1's rollback would have **reinstated the leaked token**, and rev 1 wrongly assumed a client 401
> is benign. Both are corrected below and called out inline as **[REV2]**.

---

## 0. Verified current state (read-only)

| Where | Holds the credential | Permissions |
|---|---|---|
| Windows | `C:\GuvFX\start_signal_bridge.bat` — `GUVFX_AGENT_TOKEN` + `GUVFX_WORKER_TOKEN` inline | **`Users: ReadAndExecute`** ⚠ |
| Windows | `start_signal_bridge.bat.bak-preIS6` — **also contains an inline agent-token line** | **`Users: ReadAndExecute`** ⚠ |
| Windows | `start_signal_bridge.bat.bak.txrdp4d` — **also contains an inline agent-token line** | **`Users: ReadAndExecute`** ⚠ |
| Windows machine env | *(none — no machine-level TOKEN variables)* | n/a |
| VPS | `docker-compose.yml` — token **inline** in 3 services | **`664` world-readable** ⚠ |
| VPS | `wayond-listener.env` | **`664` world-readable** ⚠ |

**[REV2] There are THREE plaintext copies of the leaked token on the Windows box, all readable by every local
user** — including `guvfx_u_{1,6,7}` and any future B3P-2 beta pool identity. All three must be remediated.

Bridge start path: task **`GuvFX_SignalBridge`** → `start_signal_bridge.bat`; task **`GuvFX_BridgeWatchdog`**
can restart the bridge independently.

**Deployed-vs-repo drift: NONE.** `C:\GuvFX\mt5_signal_bridge.py` is byte-identical to
`scripts/mt5_signal_bridge.py` (`sha256 2736f907…`, verified). The review suspected the host carried an
in-place `/mt5/supervision` patch; that was **checked and refuted** — the deployed file contains **0**
`supervision` matches. *Separately worth noting: this means `/mt5/supervision` is **already absent in
production**, so `probe_supervision` presumably already reports UNKNOWN. Pre-existing gap, not created here.*

> `GUVFX_WORKER_TOKEN` is a **different** credential and was not in the leak; it is covered by the permission
> hardening. Rotating it is recommended as separate follow-up, not bundled into this revocation.

## 1. Generating the new token
Generated **on the host that will hold it**: `python -c "import secrets; print(secrets.token_urlsafe(48))"`
(≥64 chars, CSPRNG). Written directly into the target file by the operator — never echoed, never in argv,
never in shell history (prefix with a space or use an editor).

## 2. Authoritative secret-storage location
- **Windows:** `C:\GuvFX\secrets\bridge.tokens.bat` — only the `set GUVFX_AGENT_TOKEN=` / `set
  GUVFX_WORKER_TOKEN=` lines; **ACL: SYSTEM + Administrators only**. `start_signal_bridge.bat` gains one
  `call` line and holds no credential.
- **VPS:** one `bridge-agent.env` (`600`, `ubuntu:ubuntu`) referenced by `env_file:`; the inline
  `docker-compose.yml` values are removed.

## 3. Secure delivery to the Windows bridge
Operator edits `bridge.tokens.bat` over the existing admin session and applies the ACL. Presence is verified
without disclosure (§6). Nothing else on the box needs the value.

## 4. **[REV2] FIVE** legitimate clients (rev 1 said four and was wrong)
1. `guvfx-backend` · 2. `guvfx-mt5-trade-ingest-worker` · 3. `guvfx-mt5-validate-worker` ·
4. `guvfx-wayond-listener` · 5. **`guvfx-mt5-shadow-worker`** (`restart: unless-stopped`, verified **Up 13
days** — missed by rev 1; it would have been left presenting a revoked token).
All read the credential under the names `GUVFX_AGENT_TOKEN` / `GUVFX_WINDOWS_AGENT_TOKEN` /
`WINDOWS_AGENT_TOKEN`; all five are updated from `bridge-agent.env` and recreated **inside the window**.

## 5. File and secret permissions
- Windows: `icacls C:\GuvFX\secrets /inheritance:r /grant SYSTEM:(OI)(CI)F /grant Administrators:(OI)(CI)F`
  — **no `Users` ACE**; verify `Users` is absent via `icacls`.
- **[REV2]** The live `.bat` **and both `.bak` copies** are scrubbed of inline token lines (or deleted); verify
  with `findstr /I "GUVFX_AGENT_TOKEN=" C:\GuvFX\start_signal_bridge.bat*` returning **nothing**.
- VPS: `chmod 600 bridge-agent.env wayond-listener.env`; `chmod 640 docker-compose.yml` once credential-free.

## 6. **[REV2]** Pre-restart configuration validation (executable, non-disclosing, identical both sides)
Rev 1's `sha256sum <<<"$TOKEN"` was rejected: a here-string puts the secret in a temp file and a shell
variable, and the trailing newline would make the two sides disagree and abort a *correct* rotation. Instead
each side reads its own file and hashes the **raw value**, printing only length + a 12-hex-char prefix:

- **VPS:** `python3 -c "import hashlib;v=[l.split('=',1)[1].strip() for l in open('/home/ubuntu/guvfx-prod/bridge-agent.env') if l.startswith('GUVFX_AGENT_TOKEN=')][0];print('len',len(v),'sha12',hashlib.sha256(v.encode()).hexdigest()[:12])"`
- **Windows:** the PowerShell equivalent reading `bridge.tokens.bat`, stripping `set GUVFX_AGENT_TOKEN=`, and
  hashing the raw string as UTF-8 — same canonicalisation, so digests are directly comparable.

A 12-hex prefix (48 bits) of a 64-char CSPRNG token is not reversible and is safe to record as evidence.
**Hard gate — abort and do NOT restart if:** either side reports `ABSENT`, `len` below 60, or the digests differ.

## 7. **[REV2]** Rollback — never reinstates the leaked token (rev 1's worst defect)
Rev 1 said "keep a timestamped copy of `start_signal_bridge.bat` and restore it on failure." That file
**contains the leaked token inline**, so restoring it would have **re-armed the publicly-known credential** —
the exact outcome rev 1 claimed impossible, and nothing in rev 1's proof list would have caught it.

Corrected:
- The rollback artefact is a **scrubbed** copy: inline `set GUVFX_AGENT_TOKEN=` / `set GUVFX_WORKER_TOKEN=`
  lines **removed** and replaced with the `call C:\GuvFX\secrets\bridge.tokens.bat` line. It restores
  **behaviour only**; the credential always comes from the ACL-protected tokens file.
- Stored **inside `C:\GuvFX\secrets\`** under the §5 ACL — never in a `Users`-readable directory.
- **Pre-restart assertion:** `findstr /I "GUVFX_AGENT_TOKEN=" <backup>` must return nothing.
- Pre-existing `.bak` files containing the leaked value are deleted/scrubbed **before** the window.
- **Any rollback is followed by re-running proof §10.4 (leaked credential → 401) before the window closes.**
- If the bridge will not start on the new configuration, the fix is to correct the configuration and restart —
  **never** to revert the credential.

## 8. No process may start with an absent or empty token
Guaranteed in merged code: `HTTP_AUTH_TOKEN = AGENT_TOKEN or WORKER_TOKEN` (both `.strip()`ed);
`_validate_token()` denies when unconfigured, denies an absent credential, and otherwise compares **bytes**
with `hmac.compare_digest` (byte comparison so a non-ASCII credential returns 401 rather than raising);
`validate_config()` refuses to start and `main()` exits `1`. Locked by 10 tests including an **AST-based**
guard that no unconditional permissive `return` can reappear.

## 9. **[REV2]** Execution order — approval and watchdog FIRST, everything inside one bounded window
Rev 1 disabled the watchdog at step 6 and sought approval at step 6 — *after* config edits and *after*
recreating clients. That left an armed watchdog able to crash-loop the bridge (new fail-closed code + a
tokenless `.bat` → `exit 1` → restart → repeat) **outside** any approved window, and an **unbounded**
client-401 window. Corrected order:

0. **Obtain Nuno's maintenance-window approval.** State the expected duration and the bounded worst case.
1. **Disable `GuvFX_BridgeWatchdog`** — before any file on any host is modified.
2. **Pause signal intake** for the window (stop `guvfx-wayond-listener` / pause the bound assignment) so
   signals are **not consumed and terminally rejected** (see §10.9).
3. **Record the baseline:** open positions (tickets + SL/TP), protection-ladder state, container health.
4. Deploy the exact merged `mt5_signal_bridge.py` to disk — **no restart**. (Diff against the deployed file
   first; drift was verified NONE, but re-verify at execution time rather than trusting this document.)
   **4a. LAUNCHER GATE (added after review).** The bridge now requires `GUVFX_AGENT_TOKEN` with **no
   fallback**, so any launcher that supplies only a worker token will hard-fail `validate_config()` at its
   next start — and the watchdog would restart-loop it. Before restarting ANY bridge, prove every launcher
   supplies the agent token:
   `findstr /I /C:"bridge.tokens.bat" C:\GuvFX\*.bat` must match **every** file that launches
   `mt5_signal_bridge.py` (`start_signal_bridge.bat`, `guvfx_autostart.bat`,
   `guvfx_autostart_bridge_only.bat`, `start_signal_bridge_is6.bat`), and
   `findstr /I /C:"GUVFX_AGENT_TOKEN=" C:\GuvFX\*.bat` must return **nothing** (no stale inline copies).
   These launchers are **not in Git**, so this cannot be verified from the repo — it must be checked on the
   host every time.
5. Create + ACL `C:\GuvFX\secrets\bridge.tokens.bat` **fully, before** editing `start_signal_bridge.bat`, so
   the `.bat` is never in a tokenless state. Then add the `call` line and scrub the `.bat` + both `.bak`s.
6. Update the VPS `bridge-agent.env` + compose; set permissions.
7. Run §6 validation on **every** host. Abort on any failure.
8. Recreate **all five** client containers.
9. **One controlled bridge restart.**
10. Run §10 proof. Re-enable the watchdog. Resume signal intake.

**Abort trigger:** if the bridge is observed exiting non-zero more than once, stop and restore the *scrubbed*
prior script (§7).

## 10. Post-rotation proof (all must pass)
1. Bridge healthy with the **new** credential (`200`).
2. **Missing** credential → `401`. 3. **Incorrect** credential → `401`.
4. **The leaked credential → `401`** — revocation proven.
5. Protected `POST` routes (`/mt5/order`, `/mt5/close-position`, `/mt5/modify-position`) still require auth,
   verified by an **unauthenticated** probe returning `401`. **No order is placed:** auth is rejected before
   any MT5 call and no authenticated order request is issued.
6. **[REV2]** No auth failures after the window — now genuinely observable, because the merged code logs
   `HTTP auth denied: …` on every rejection (rev 1 asserted this proof against code that logged nothing).
7. Bridge process + all five clients healthy.
8. **Nuno's MT5 terminal and open positions unaffected** — tickets/SL/TP identical to the step-3 baseline.
9. **[REV2]** `PromotionAuditEvent` shows **zero** `PROMOTION_REJECTED` with reason `margin_unverifiable`
   during the window, and **no** failed `MODIFY_POSITION` job against an open position. *Rationale: a client
   401 is NOT benign — `_margin_guard_reason` converts any bridge failure into `margin_unverifiable`, which
   `signal_promotion` writes as a **terminal** rejection with no replay path. This is the same failure shape as
   the earlier drawdown-gate incident, and it fires for exactly the live `ti_signals` sizing (1.20/signal).*
10. No unauthenticated fallback: evidenced by the test suite, not by unsetting the live process's token.

## 11. Out of scope
**Git history is not rewritten.** Rotation is the revocation; history hygiene is separate and must not delay it.
