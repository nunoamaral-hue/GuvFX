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

**Allow surface (Exe / Msi / Script):**

- **Administrators** (`S-1-5-32-544`) — Allow `*` (operator recovery).
- **System / service / virtual-account principals keep unrestricted Windows execution**, so the OS, RDS and the
  desktop compositor are unaffected:
  `SYSTEM S-1-5-18`, `LOCAL SERVICE S-1-5-19`, `NETWORK SERVICE S-1-5-20`, `Service S-1-5-6`,
  **`Window Manager Group S-1-5-90-0`** (covers the dynamic per-session `DWM-N` accounts) and
  **`Font Driver Host S-1-5-96-0`** (covers `UMFD-N`/`fontdrvhost`).
- **Everyone (`S-1-1-0`) — and therefore every hosted tenant — receives ONLY:**
  1. the **MetaQuotes publisher** rule (`O=METAQUOTES LTD., …`) → portable MT5 (terminal64/metaeditor64);
  2. a **curated minimal RemoteApp/session-infrastructure allow-list** by `%SYSTEM32%` path
     (`rdpinit`, `rdpshell`, `rdpclip`, `tstheme`, `userinit`, `sihost`, `ctfmon`, `taskhostw`, `conhost`,
     `shellappruntime`, `shellhost`, `wlrmdr`) — all system-signed, non-interpreter, non-LOLBIN.
- **Everything else is deny-by-default** — including all 61 LOLBINs/interpreters.

**No general-purpose interpreter is ever granted to a hosted tenant** (`python`, `cmd`, `powershell`, `wscript`,
`cscript`, `rundll32`, `msbuild`, …). This is a permanent regression guard: a hosted tenant that could run an
interpreter could forge the observation and defeat ADR-0041.

**The observer is NOT tenant-run python.** Because python is an arbitrary-code primitive, the STREAM 9E observer
is re-shipped as a signed compiled executable (**STREAM 10C**, below), allow-listed by publisher — the only
tenant-runnable observation component.

## Enforcement / regression guards (`assert_allow_model_invariants`)

Permanent, machine-checked, in CI:
- **No broad `(Everyone) Allow %WINDIR%\*` / `%PROGRAMFILES%\*` / `%SYSTEM32%\*` / `*` EXE or Script rule.**
- Every system/service/virtual-account SID keeps its Windows allow (removing one → fails).
- **No forbidden interpreter/LOLBIN** in any hosted (non-admin/non-system) allow.
- **Drift guard:** the committed AuditOnly + Enforce templates must equal the generator output.

## Host rollout (staged, reversible throughout)

`Set-GuvfxAppLocker.ps1` deploys the generated template. Order (STREAM 10B):
**CZ before-fingerprint → deploy AuditOnly (blocks nothing) → real-workload soak → behavioural validation →
refine the curated list until zero legitimate `8003` → deploy Enforce → behavioural `8004` escape battery →
CZ after-fingerprint → rollback validation.** `-Mode Rollback` clears the policy (NotConfigured) at any point.
AuditOnly is non-blocking, so the model is proven against the real workload before anything is enforced.

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
