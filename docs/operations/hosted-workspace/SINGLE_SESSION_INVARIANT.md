# Hosted Workspace — Single-Session Invariant (duplicate-runtime fix)

**Status:** Applied on the Customer Zero host 2026-08-10 (execution DARK). Behavioural
positive-control (reconnect rejoins) pending one Sponsor reconnect — see below.

## Symptom

For a single workspace (`guvfx_u_1`, portable runtime `C:\GuvFX\accounts\1\terminal`) the host
showed **two** `terminal64.exe` processes in **two** disconnected `guvfx_u_1` sessions (IDs 3 and
4), both bound to the **one** portable data directory. Two MT5 instances sharing one data dir is
unsupported and is the plausible source of `config\accounts.dat` / journal evidence-quality noise.

## Root cause

The RDS host had `fSingleSessionPerUser = 0` at
`HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server` (no policy override).

- Each Guacamole RemoteApp connection is a fresh RDP logon for the fixed Windows user
  `guvfx_u_1`.
- With single-session **disabled**, Windows creates a **new** session per connection instead of
  reconnecting to the existing **disconnected** one.
- Each new session auto-launches the published `terminal64` RemoteApp (`/portable`) against the
  same data dir → duplicate hosted runtimes.

The backend delivery layer already does the right thing to avoid this: it mints a **stable**
per-workspace connection id (`mt5-workspace-<workspace_uuid>`, `delivery.py`) with the fixed
Windows username, so a reconnect deep-links to the **same** session. The `unique_username` in
`guac_json.py` (PX-7B) is only the Guacamole *webapp* auth-token identity and does **not** affect
the Windows RDP session identity. The intent was defeated purely by the host setting.

## Fix (singleton guarantee)

Set `fSingleSessionPerUser = 1` (Restrict each user to a single session = **Enabled**).

- Reconnect **rejoins** the existing per-user session (rejoining the running `terminal64`).
- Per-**user**, so multi-tenant is preserved: each `guvfx_u_<id>` still gets one session.
- Better persistence/UX: the customer resumes their exact running MT5.
- Defense-in-depth: MT5 portable also holds a single-instance lock per data directory.

Reproducible artefact: `backend/terminal_provisioning/windows/Set-GuvfxSingleSession.ps1`
(`-Mode Verify|Enforce|Rollback`, ASCII-only per RULE 9, parse-validate before first host use).

### Evidence (2026-08-10, host `WIN-RD8VDS93DK7`, execution DARK)

- BEFORE: `fSingleSessionPerUser = 0`; sessions `3/Disc, 4/Disc`; portable `terminal64` pids
  `2628 (sess3)`, `8520 (sess4)`.
- APPLY: `fSingleSessionPerUser` **0 → 1** (read-back = 1).
- CLEANUP: logged off the two **disconnected** `guvfx_u_1` sessions (scoped exactly like
  `Cleanup-GuvfxSessions.ps1` — `guvfx_u_*` only, never Administrator/console/services). Logoff
  also flushes each MT5 journal.
- AFTER: `guvfx_u_1` sessions **NONE**; portable `terminal64` **gone**; only the separate legacy
  IS6 terminal (`C:\Program Files\IS6 Technologies MT5 Terminal`, Administrator session 1) remains.

**Rollback:** `Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' -Name fSingleSessionPerUser -Value 0 -Type DWord`

## Pending positive control (RULE 11)

The setting is authoritative and read-back-verified, but the end-to-end **behavioural** proof —
a fresh Sponsor connect creates exactly one session, and a disconnect/reconnect **rejoins** it
(session/terminal count stays at 1) — requires one Sponsor reconnect and is the single remaining
confirmation before resuming AppLocker Enforce + execution certification. Execution remains DARK
throughout; no order placed/closed/modified.
