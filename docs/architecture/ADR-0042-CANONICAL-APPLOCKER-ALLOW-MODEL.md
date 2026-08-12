# ADR-0042 — Canonical Deny-by-Default AppLocker Allow Model

- **Status:** Accepted (Sponsor decision, STREAM 10 / 10B, 2026-08-12). Notion owns the approved ADR lifecycle;
  this git-side record is the concise implementation evidence.
- **Supersedes:** the STREAM 6 "default-allow baseline + per-tenant DENY-list" model for hosted-tenant isolation.
- **Extends / anchors:** ADR-0041 (Hosted Workspace observation trust model). `REMOTEAPP_ISOLATION_CERTIFIED`
  is the root anchor of ADR-0041; this ADR defines the AppLocker model that makes it true.

## Context

STREAM 10 Phase A produced **behavioural evidence** (the OS's own `Test-AppLockerPolicy` evaluation for the
hosted tenant SID, plus a full host binary inventory) that the previous model **cannot** satisfy
`REMOTEAPP_ISOLATION_CERTIFIED`:

- The base policy granted `(Everyone) Allow %WINDIR%\*` + `(Everyone) Allow %PROGRAMFILES%\*`, and confined the
  tenant with a **12-binary per-SID DENY-list** (cmd/powershell/…).
- The host carries **61 LOLBIN/interpreter execution primitives** (`rundll32`, `regsvr32`, `regsvcs`, `regasm`,
  `installutil`, `msbuild`, `csc`/`vbc`/`jsc`/`ilasm`, `cmstp`, `mavinject`, `certutil`, `bitsadmin`, …). Only 12
  were denied, so **~49 arbitrary-code-execution primitives were Allowed** to the hosted tenant.
- A name-deny-list cannot close a 61-primitive surface (new/renamed/updated binaries). The proper fix — recorded
  in the prior review as the outstanding residual — is a **stricter allow-model, not more name-denies**.

A tenant that can execute arbitrary code can forge the observation handoff, which **directly defeats ADR-0041**.
So this is not defence-in-depth; it is the load-bearing control for the whole hosted trust chain.

## Decision

**The canonical hosted-workspace AppLocker model is deny-by-default with an explicit minimal allow-list,
generated from a single source of truth.** `backend/hosted_workspace/applocker_policy.py::generate_base_policy`
is that source; the committed XML templates
(`terminal_provisioning/windows/applocker/guvfx-hosted-{auditonly,enforce}.xml`) must always equal its output
(drift-guarded in tests).

**Allow surface (Exe / Msi / Script / Dll):**

- **Administrators** (`S-1-5-32-544`) — Allow `*` (operator recovery).
- **System / service / virtual-account principals keep unrestricted Windows execution**, so the OS, RDS and the
  desktop compositor are unaffected:
  `SYSTEM S-1-5-18`, `LOCAL SERVICE S-1-5-19`, `NETWORK SERVICE S-1-5-20`, `Service S-1-5-6`,
  **`Window Manager Group S-1-5-90-0`** (covers the dynamic per-session `DWM-N` accounts) and
  **`Font Driver Host S-1-5-96-0`** (covers `UMFD-N`/`fontdrvhost`).
- **Everyone (`S-1-1-0`) — and therefore every hosted tenant — receives ONLY:**
  1. the **MetaQuotes publisher** rule (`O=METAQUOTES LTD., …`) → portable MT5 (terminal64/metaeditor64);
  2. (Exe) a **curated minimal RemoteApp/session-infrastructure allow-list** by `%SYSTEM32%` path
     (`rdpinit`, `rdpshell`, `rdpclip`, `tstheme`, `userinit`, `sihost`, `ctfmon`, `taskhostw`, `conhost`,
     `shellappruntime`, `shellhost`, `wlrmdr`) — all system-signed, non-interpreter, non-LOLBIN;
  3. (Dll) the **Microsoft OS-component publisher** + the MetaQuotes publisher — **publisher-based, no path
     wildcard** (see below).
- **Everything else is deny-by-default** — including all 61 LOLBINs/interpreters.

**The `Dll` collection is load-bearing (added after the STREAM 10B allow-surface review, HIGH; hardened after the
re-verify HIGH).** AppLocker's Exe rules check *launched images* but never a DLL loaded *into* an already-allowed
process. Because the MetaQuotes publisher EXE rule is path-free (the accounts tree is writable, so it *cannot* be
path-pinned — that is the `assert_base_invariants` writable-tree guard), a tenant could stage a genuine signed
`terminal64.exe` in a writable dir and plant a sibling `dwmapi.dll`/`version.dll` (application-directory search
order), or hijack `HKCU\…\CLSID\{…}\InprocServer32` into `sihost`/`taskhostw` — running arbitrary **native** code,
invisible to the Exe `8003/8004` soak, defeating ADR-0041. **The re-verify proved a `%WINDIR%\*` path allow does
NOT close this:** `%WINDIR%\Temp`, `\Tasks`, `\tracing`, `\Registration\CRMLog`,
`System32\{spool\drivers\color, FxsTmp, com\dmp}` and the SysWOW64 equivalents are **user-writable**, so a planted
DLL relocated there matches the wildcard and loads. Therefore the tenant-reachable DLL surface is **publisher-only**
(Microsoft OS signer + MetaQuotes): a planted unsigned/self-signed DLL matches **neither**, so it is denied
**wherever** it is planted (accounts tree, `%WINDIR%\Temp`, anywhere). OS DLLs (Microsoft-signed) and MT5 DLLs
(MetaQuotes-signed) load normally; service daemons that load non-Microsoft DLLs (e.g. `python311.dll`) run as the
**service SIDs** and get `%PROGRAMFILES%\*` (not tenant-reachable, not tenant-writable). A Microsoft-signed DLL is
trusted code, not an arbitrary-code primitive — so the Microsoft publisher is allowed for the `Dll` collection
**only** (a Microsoft-signed EXE such as `rundll32` would be a signed-LOLBIN ACE, so it is never allowed in
Exe/Msi/Script). The `Dll` collection needs its own AuditOnly soak: the exact Microsoft subject and any
non-publisher MT5/service DLL must be confirmed (`8003`) and pinned before Enforce. **Residuals, documented not silently accepted:** (a) `Appx` (packaged-app) collection is *not*
configured — the review could construct no non-admin sideload exploit (needs developer-mode/admin), so per the
no-speculative-over-enforcement rule it is a tracked residual to close only if a soak shows a packaged-app tenant
vector; (b) the MetaQuotes publisher rule is still `BinaryName=*` / version `*`–`*` (bounded to the unforgeable
MetaQuotes-signed corpus) — pinning `BinaryName`/version floor is a soak-time refinement done from the host's
actual signature metadata, before Enforce.

**No general-purpose interpreter is ever granted to a hosted tenant** (`python`, `cmd`, `powershell`, `wscript`,
`cscript`, `rundll32`, `msbuild`, …). This is a permanent regression guard: a hosted tenant that could run an
interpreter could forge the observation and defeat ADR-0041.

**The observer is NOT tenant-run python.** Because python is an arbitrary-code primitive, the STREAM 9E observer
is re-shipped as a signed compiled executable (**STREAM 10C**, below), allow-listed by publisher — the only
tenant-runnable observation component.

## Enforcement / regression guards (`assert_allow_model_invariants`)

The STREAM 10B regression review found the original guards were a **fixed blocklist over the Exe collection only**
(exact forbidden path strings + the Everyone SID + a fixed interpreter list), which a one-line generator edit could
evade (a `%WINDIR%\System32\*` alias, a broad allow to `BUILTIN\Users`, an unguarded `FilePublisherRule`, a
widening in the Msi/Script collection). The guard is therefore **rewritten as a POSITIVE ALLOWLIST over EVERY rule
collection** — permanent, machine-checked, in CI:
- **The `Exe` and `Dll` collections must be present** (Dll closes the DLL-sideload / COM-hijack native-code path).
- **Every Allow reachable by a *tenant-reachable* principal** — anything that is **not** Administrators or one of
  the system/service/virtual SIDs (so: Everyone, `BUILTIN\Users`, Authenticated Users, Interactive, any user SID)
  — must be **exactly one certified form**: the MetaQuotes publisher rule; (Dll only) the Microsoft OS publisher;
  or (Exe) a `%SYSTEM32%\<curated leaf>` path. **No tenant-reachable path allow is permitted in Dll** (publisher-
  only — a `%WINDIR%\*` wildcard is bypassable via user-writable `%WINDIR%` subdirs). Anything else (broad path,
  subdir alias, non-MetaQuotes/-Microsoft publisher, a widening in Msi/Script/Dll) **fails**.
- **Collections are iterated as a list, not keyed by Type**, so a duplicate `RuleCollection Type` cannot shadow a
  widening; **all collections must share one enforcement mode** (no silently-`NotConfigured` collection); and
  `Action` is matched case-insensitively so `Action="allow"` cannot evade the guard.
- **System/service allows stay confined to non-tenant-writable OS paths**; every system/virtual SID keeps its Exe
  Windows allow (removing one → fails).
- **No forbidden interpreter/LOLBIN** leaf granted to a tenant-reachable principal (belt-and-suspenders tripwire,
  expanded with `wsl`/`odbcconf`/`scriptrunner`/…), backed by a **frozen-set change-detector** on
  `HOSTED_SESSION_ALLOW` so any curated-list change is a visible, reviewed edit.
- **Drift guard:** the committed AuditOnly + Enforce templates must equal the generator output; a separate test
  asserts the invariants on the **committed file contents** (not just generator output), so the deployed artifact
  is independently proven.

## Host rollout (staged, reversible throughout)

`Set-GuvfxAppLocker.ps1` deploys the generated template. Order (STREAM 10B):
**CZ before-fingerprint → deploy AuditOnly (blocks nothing) → real-workload soak (Exe AND Dll) → behavioural
validation → refine the curated list until zero legitimate `8003` → deploy Enforce → behavioural `8004` escape
battery → CZ after-fingerprint → rollback validation.** The `8004` escape battery MUST include the DLL vectors,
not only shell/EXE launches: **(a) a DLL side-load** — plant a sibling DLL next to a staged signed `terminal64.exe`
in a writable dir and confirm the load is blocked (`8004` on the `Dll` collection); **(b) an HKCU COM hijack** —
register `InprocServer32` to a tenant-writable DLL and trigger instantiation in `sihost`/`taskhostw`, confirming
the load is blocked. `-Mode Rollback` restores the exact pre-Deploy policy captured at first Deploy (not a blind
clear); run it against a **dedicated `-StateDir`** so the baseline anchors the current live model. AuditOnly is
non-blocking, so the model is proven against the real workload before anything is enforced.

## Load-bearing residual — MQL5 `#import` native-code lever (NOT closeable by AppLocker)

The STREAM 10B re-verify surfaced a **genuine isolation-escape that this AppLocker model cannot close and does not
regress** — but which **`REMOTEAPP_ISOLATION_CERTIFIED` must not be emitted over**:

- `terminal64.exe` **and** `metaeditor64.exe` are MetaQuotes-signed, so both are tenant-runnable (the Exe MetaQuotes
  publisher rule is `BinaryName=*`). MetaEditor is a full MQL5 editor+compiler.
- MQL5 `#import "kernel32.dll"` (or `ntdll.dll`) lets a compiled MQL script/EA call **arbitrary exported native
  functions** (`VirtualAlloc`+`VirtualProtect`+`CreateThread` = a shellcode runner) **inside** the MetaQuotes-signed
  `terminal64.exe`. It runs the moment the script is dropped on a chart — **no** AutoTrading, **no** broker login.
- `kernel32`/`ntdll` are **Microsoft-signed**, so the (mandatory) `(Everyone) Microsoft-signed DLL` rule allows them
  — as it must, MT5 cannot run without them. **AppLocker enforces at DLL-LOAD granularity, never at function-call
  granularity**, so its `Dll` collection structurally cannot deny this. This is a tenant arbitrary-code path that
  **defeats ADR-0041** ("a tenant that can run arbitrary code can forge the observation").
- The **only** control is MT5's `[Experts] AllowDllImport=0` (written to `config\common.ini` at launch). Today it is
  **tenant-mutable**: the tenant has NTFS **Modify** on its runtime tree `C:\GuvFX\accounts\<id>` (which contains
  `common.ini`), and the interactive `Tools > Options > Expert Advisors > Allow DLL imports` toggle persists to that
  same file. So the tenant can set `AllowDllImport=1` and relaunch.

**Closure required before `REMOTEAPP_ISOLATION_CERTIFIED` (a separate workstream — spans the workspace ACL model +
MT5 launch config + a host behavioural cert):**
1. Make `AllowDllImport=0` **tenant-immutable** — NTFS-deny the tenant write to `config\common.ini` (or the `[Experts]`
   key) while keeping Modify elsewhere (a change to the `hosted_workspace.workspace_acl` tenant-ACL pattern — an
   **Amber** decision to record).
2. Remove MQL **authoring/compilation** from the tenant surface — deny `metaeditor64.exe` (pin the MetaQuotes Exe
   publisher rule to `BinaryName=terminal64.exe`, soak-verified against the host's real signature metadata) and/or
   restrict the MQL5 source/experts directory.
3. **Behaviourally certify on the host (RULE 11):** prove (a) a tenant-set `AllowDllImport=1` does **not** persist /
   is refused after restart + UI toggle, and (b) an MQL `#import` of an OS DLL yields **no** native execution.
4. **Add to the `8004` escape battery** an MQL-`#import` shellcode attempt (with `AllowDllImport` flipped) — the
   currently-documented battery (EXE launch + DLL side-load + HKCU COM hijack) would **not** catch this.

Until this is closed and certified, the isolation marker stays **withheld**; the AppLocker allow-model below is a
**necessary but not sufficient** part of RemoteApp isolation.

## Consequences

- `REMOTEAPP_ISOLATION_CERTIFIED` becomes truthfully achievable once Enforce + the `8004` battery pass with the
  CZ fingerprint preserved.
- The model is **machine-wide + tenant-agnostic**: all hosted tenants get the identical minimal surface, so no
  per-tenant AppLocker differentiation is needed for isolation (the legacy per-tenant DENY fragments remain as
  optional defence-in-depth / back-compat but the isolation no longer depends on them).
- **STREAM 10C — Signed Observer EXE:** a fixed-purpose, Authenticode-signed executable (self-signed GuvFX cert
  for dev in `LocalMachine\TrustedPublisher`, replaceable by a commercial cert without architecture change),
  no embedded interpreter, no scripting runtime, deterministic output only, allow-listed by publisher. It
  becomes the only tenant-runnable observation component and unblocks `HOSTED_OBSERVATION_CERTIFIED`.
