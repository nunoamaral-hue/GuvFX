# Hosted Workspace — AppLocker Hardening (AuditOnly deployed; Enforce pending)

Keeps four things distinct (RULE 5):

- **Intent (architecture):** confine the hosted RemoteApp customer (`guvfx_u_<id>`, non-admin) to MetaTrader 5
  only — no desktop shell, no arbitrary process — while preserving Administrator operator-recovery and not
  disturbing the legacy executor.
- **Current implementation:** an **AuditOnly** (non-blocking) candidate policy is deployed on the Customer
  Zero host. It blocks nothing; it logs what an Enforced policy *would* block (event 8003).
- **Temporary compatibility:** the legacy Administrator MT5 executor and the Administrator recovery path are
  untouched (Administrators keep Allow-* in every collection).
- **Historical:** none (first AppLocker deployment on this host; prior effective policy was empty).

## Posture

Default-allow baseline + explicit **DENY of shell/escape binaries scoped to the hosted user SID**:

- Administrators (`S-1-5-32-544`): Allow `*` in Exe/Msi/Script — recovery preserved.
- Everyone (`S-1-1-0`): Allow `%WINDIR%`, `%PROGRAMFILES%`, and the portable runtime `C:\GuvFX\accounts\*`
  — so MT5 (portable, outside Program Files) and all Windows/RDS/RemoteApp infra run.
- Hosted user SID (deny; overrides allow, hosted-scoped so Administrators are unaffected): `cmd.exe`,
  `powershell.exe`, `powershell_ise.exe`, `pwsh.exe`, `explorer.exe`, `regedit.exe`, `mmc.exe`,
  `taskmgr.exe`, `wscript.exe`, `cscript.exe`, `mshta.exe`, `control.exe`.

The **Dll** collection is intentionally NOT configured this pass (DLL-level confinement is a heavier later
refinement; Exe/Msi/Script cover process-launch confinement, the goal).

Artefacts (reproducible, ASCII-only per RULE 9):
`backend/terminal_provisioning/windows/applocker/guvfx-hosted-auditonly.xml` (template, `{{HOSTED_USER_SID}}`
substituted at deploy time) and `backend/terminal_provisioning/windows/Set-GuvfxAppLocker.ps1`
(`-Mode Deploy|Verify|Rollback|Evidence`).

## Deployment evidence (2026-08-10, host `WIN-RD8VDS93DK7`, execution DARK)

- Hosted SID resolved: `S-1-5-21-2216203845-1747098376-1637942580-1003` (`guvfx_u_1`).
- **AppIDSvc** (Application Identity): start=Auto, status=Running. (Note: AppIDSvc is a *protected* service —
  `Set-Service -StartupType` is denied even to Administrators; the start type is set via the registry
  `HKLM\SYSTEM\CurrentControlSet\Services\AppIDSvc\Start`.)
- Effective policy: **Exe=16, Msi=2, Script=4 — all `AuditOnly`** (blocks nothing).
- Audit channels **enabled + readable**: `Microsoft-Windows-AppLocker/EXE and DLL` and `.../MSI and Script`
  (`IsEnabled=true`, records present) — measurement path proven (RULE 11 mechanism control).
- **Rollback verified** by round-trip: `-Mode Rollback` cleared the effective policy to `<none>`;
  `-Mode Deploy` restored `Exe=16/Msi=2/Script=4 AuditOnly`. Baseline anchor saved at
  `C:\GuvFX\_applocker\baseline-effective-policy.xml`.

**Rollback command:** `powershell -File Set-GuvfxAppLocker.ps1 -Mode Rollback` (clears the local policy to
NotConfigured and restores AppIDSvc to its captured baseline).

## What remains before Enforce (deliberately NOT done)

1. **Sponsor AuditOnly workload (the one Sponsor step):** the customer opens MT5 (RemoteApp) and uses it
   normally for a few minutes (charts, Market Watch, menus/dialogs, reconnect). This generates the real
   allow-surface + any 8003 "would-block" events for the hosted user.
2. **Review:** classify every 8003 as *required* (add an allow rule) vs *unexpected* (confirms confinement).
   Enforce only when legitimate would-be-blocks for the certified customer journey = zero.
3. **Enforce:** switch collections to `Enabled`, then immediately verify positive controls (customer MT5
   works) + negative controls (cmd/powershell/explorer denied) and roll back instantly if legitimate MT5
   breaks.
4. **Escape certification** under Enforce.

Note: the policy-decision proof via `Test-AppLockerPolicy` (Administrators allow cmd; hosted user deny
cmd/powershell/explorer; hosted user allow `terminal64.exe`) could not be captured headless — the cmdlet
hangs over the host's SSH-stdin channel. The confinement is structurally guaranteed (Deny overrides Allow;
denies SID-scoped to the hosted user; Admin Allow-* preserved) and will be behaviourally confirmed at
Enforce-time and in the AuditOnly 8003 review. Execution remains DARK throughout; no order placed.
