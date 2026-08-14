# Hosted Workspace — AppLocker Hardening

> **Update 2026-08-12 — STREAM 10B: CANONICAL deny-by-default allow model (ADR-0042). SUPERSEDES the
> default-allow + per-tenant-DENY posture below.** The 2026-08-10 Enforce model (next section) allowed
> `(Everyone) %WINDIR%\*` + `%PROGRAMFILES%\*` and confined tenants with a 12-binary DENY list — which STREAM 10
> Phase A proved leaves ~49 `%WINDIR%` LOLBIN/interpreter primitives runnable by a hosted tenant. The canonical
> model inverts this: **deny-by-default with an explicit minimal allow-list**, generated from the single source of
> truth `backend/hosted_workspace/applocker_policy.py::generate_base_policy` (drift-guarded in
> `tests_applocker_policy.py`).
>
> - **Allow surface (Exe / Msi / Script / Dll):** Administrators `*`; system/service/virtual-account SIDs keep
>   Windows execution; **Everyone gets ONLY** the MetaQuotes publisher (portable MT5) + a curated `%SYSTEM32%`
>   session-infra list (Exe) + OS DLLs from `%WINDIR%` (Dll). No general-purpose interpreter, ever.
> - **The `Dll` collection is now configured** (reversing the "intentionally NOT configured" decision recorded in
>   the original section below), and is **publisher-based** for tenants. It is load-bearing: without it a tenant
>   runs arbitrary **native** code via a DLL side-load (plant a sibling DLL next to a staged signed
>   `terminal64.exe`) or an HKCU COM `InprocServer32` hijack into `sihost`/`taskhostw` — both invisible to the Exe
>   `8003/8004` soak and both defeating ADR-0041. A `%WINDIR%\*` path allow does **not** close this (user-writable
>   `%WINDIR%\Temp`, `System32\spool\drivers\color`, … let a planted DLL match the wildcard), so the tenant DLL
>   surface is **Microsoft OS publisher + MetaQuotes publisher only** — a planted unsigned DLL matches neither and
>   is denied anywhere. Service daemons (non-MS DLLs, e.g. `python311.dll`) run as the service SIDs with
>   `%PROGRAMFILES%\*`.
> - **Escape battery (`8004`) MUST include the DLL vectors:** (a) DLL side-load blocked (`8004` on `Dll`);
>   (b) HKCU COM-hijack DLL load blocked; **(c) a planted DLL in a user-writable `%WINDIR%` subdir
>   (`%WINDIR%\Temp`) blocked** — the specific re-verify HIGH. Not only cmd/powershell/explorer EXE launches. The
>   AuditOnly Dll soak must first confirm the Microsoft publisher subject matches the host's real OS-DLL
>   signatures (no legitimate `8003`), and enumerate the actual user-writable `%WINDIR%` subdirs on the host
>   (`accesschk -w -u Users C:\Windows`) as the ground-truth target set for the plant tests (RULE 11).
> - **UPDATE 2026-08-12 — the MQL5 `#import` residual is being CLOSED by STREAM 10D / ADR-0043 (the W^X model):**
>   G5v2 inverted ACL (root Read+Execute, Modify only on data subdirs, `common.ini`+code-dirs tenant Deny-write) +
>   per-tenant AppLocker **positive execution allowlist** (a tenant-SID `Deny(*)` whose exceptions are exactly the
>   RX `terminal64` + approved `%SYSTEM32%` session binaries — so a copied signed `terminal64` runs from *nowhere*,
>   location-irrelevant) + MetaEditor `BinaryName` pin + vetted-empty golden. Repo foundation DARK behind
>   `HOSTED_WX_ISOLATION_ENABLED`; `REMOTEAPP_ISOLATION_CERTIFIED` still WITHHELD pending the on-host W^X escape
>   battery. See `docs/architecture/ADR-0043-HOSTED-WX-NATIVE-CODE-ELIMINATION.md`.
>   - **RULE 5 — keep the layers distinct.** The **G5v2 ACL + per-tenant W^X `Deny(*)`** are the per-slot,
>     provisioning half and ARE gated by `HOSTED_WX_ISOLATION_ENABLED`. The **MetaEditor `BinaryName` pin** is NOT
>     gated by that flag — it ships in the **machine-wide BASE templates** (`generate_base_policy` +
>     `guvfx-hosted-{auditonly,enforce}.xml`) as an ADR-0042-lineage deny-tightening (it only denies
>     `metaeditor64`, opens nothing), so a base redeploy applies it independent of the flag. **RULE-11 pre-Enforce
>     control:** an **Enabled**-mode base redeploy must first prove on-host that `terminal64.exe`'s embedded
>     signature `BinaryName` equals the pinned literal (else it would deny `terminal64` itself and take MT5 down for
>     every tenant incl. Customer Zero); until proven, exercise the pin in **AuditOnly only**.
>   - **Application wiring (the W^X `Deny` is not inert):** `applocker_policy.compile_effective_wx_policy` composes
>     `base + per-tenant Deny(*)` from the canonical source, and `Set-GuvfxAppLockerTenant.ps1 -Mode MergeWx`
>     applies the backend-produced fragment host-side (validated to a single Exe `Deny` bound to the tenant SID).
>     End-to-end application from `slot_preparation` is DARK behind the flag + host-executor seam, same as the NTFS
>     G5v2 plan.
> - **LOAD-BEARING RESIDUAL — MQL5 `#import` (NOT closeable by AppLocker alone; blocks the isolation marker).** A tenant
>   can run MetaEditor (MetaQuotes-signed), compile an MQL5 EA that `#import "kernel32.dll"`, and execute arbitrary
>   NATIVE code inside MetaQuotes-signed `terminal64.exe` — `kernel32` is Microsoft-signed and mandatorily allowed,
>   and AppLocker enforces at DLL-*load* not *function-call* granularity. The only control is MT5
>   `[Experts] AllowDllImport=0`, today tenant-mutable (tenant has Modify on `common.ini`; the Options UI toggle
>   persists there). Before `REMOTEAPP_ISOLATION_CERTIFIED`: (1) make `AllowDllImport=0` tenant-immutable (NTFS-deny
>   tenant write to `config\common.ini`); (2) remove `metaeditor64.exe` from the tenant surface (pin the MetaQuotes
>   Exe rule to `BinaryName=terminal64.exe`); (3) add an MQL-`#import` shellcode attempt to the `8004` battery; (4)
>   behaviourally certify (RULE 11) that a tenant-set `AllowDllImport=1` does not persist and `#import` yields no
>   native exec. See ADR-0042 "Load-bearing residual". This is a **separate workstream** (workspace ACL + MT5 launch
>   config + host behavioural cert) — the AppLocker allow-model is necessary but NOT sufficient for isolation.
> - **Rollout (staged, reversible):** CZ before-fingerprint → deploy AuditOnly → real-session soak (Exe + Dll) →
>   validate `8003` → refine → deploy Enforce → `8004` escape battery (incl. DLL) → CZ after-fingerprint →
>   rollback validation. Use `Set-GuvfxAppLocker.ps1 -Mode Deploy [-Enforce] -StateDir C:\GuvFX\_applocker_s10b`
>   (dedicated state dir so the rollback baseline anchors the current live model). `-Mode Rollback` restores the
>   exact captured baseline (no blind clear).
> - **Residuals (documented):** `Appx` collection not configured (no non-admin sideload exploit found — a tracked
>   soak item, not a silent gap); MetaQuotes publisher rule still `BinaryName=*`/version `*`–`*` (bounded to the
>   unforgeable MetaQuotes corpus — pin from host signature metadata during the soak, before Enforce).
> - **State:** repository-complete + `make check` GREEN + adversarial review closed (3 lenses); **host rollout
>   pending**; execution remains DARK. Emits `REMOTEAPP_ISOLATION_CERTIFIED` only after Enforce + the DLL-inclusive
>   `8004` battery pass with the CZ fingerprint preserved.

---

# (superseded 2026-08-12) Hosted Workspace — AppLocker Hardening (ENFORCE active)

> **Update 2026-08-10 — ENFORCE enabled after a clean AuditOnly review.** The Sponsor ran a full
> customer MT5 session under AuditOnly; the hosted user generated **29 `8002` "allowed" events and
> zero `8003` would-be-blocks** (RULE 11 positive control satisfied — the audit path provably captured
> the hosted user's real activity, so "0 blocks" is a true clean result). Enforce was then enabled via
> `Set-GuvfxAppLocker.ps1 -Mode Deploy -Enforce`: effective **Exe=Enabled / Msi=Enabled / Script=Enabled**.
> Verified under Enforce: Administrator recovery intact (admin commands run); the hosted MT5 (`terminal64`,
> portable) runs in an **Active** `guvfx_u_1` session with the broker **authorized** (`'1302561': authorized
> on IS6Technologies-Demo`, terminal synchronized, trading enabled); session-3 processes are MT5 +
> RemoteApp infra ONLY (no `explorer`/`cmd`/`powershell`); hosted `8004` blocks = 0 (no denied binary
> attempted). Rollback to empty (`-Mode Rollback`) remains verified. **Behavioural escape-attempt
> certification (actively invoking cmd/powershell/explorer/Run/file-dialog escapes as the customer and
> confirming an `8004` block) is the one residual Sponsor step** — structurally the confinement is in place
> (Enforce active, SID-scoped denies, no shell present in the session). Execution remains DARK.

---

# (original) Hosted Workspace — AppLocker Hardening (AuditOnly deployed; Enforce pending)

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
