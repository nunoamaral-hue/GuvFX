# ADR-0043 — Hosted Workspace W^X Native-Code Elimination (G5v2)

- **Status:** Accepted (Chief Architect decision, STREAM 10D, 2026-08-12). Notion owns the approved-ADR
  lifecycle; this git-side record is the concise implementation evidence.
- **Closes:** the ADR-0042 "load-bearing residual — MQL5 `#import`" and the portable-copy vector (V5). Anchors
  `REMOTEAPP_ISOLATION_CERTIFIED` (ADR-0041): once behaviourally certified, a hosted tenant can run **no**
  arbitrary native code, so the observation handoff is no longer forgeable.
- **Supersedes (candidate):** the G5v1 ACL contract `{SYSTEM Full, Administrators Full, tenant Modify (whole
  tree)}` **for hosted tenants**. G5v1 remains the historical/certified evidence and the LIVE contract until
  G5v2's own behavioural certification passes — it is not silently mutated.

## Context

ADR-0042 shipped the canonical deny-by-default AppLocker allow-model but documented a residual it **cannot**
close: MQL5 `#import "kernel32.dll"` runs native shellcode **inside** the MetaQuotes-signed `terminal64.exe`
(kernel32 is Microsoft-signed and mandatorily allowed; AppLocker gates DLL *loads*, not function *calls*). Five
vectors exist; the decisive one is **V5 portable-copy**: because the tenant has NTFS **Modify on the whole
runtime tree** and the Exe MetaQuotes publisher Allow is **path-free**, the tenant copies the signed
`terminal64.exe` + their own `/portable` config (`AllowDllImport=1`) + a pre-compiled `#import` EA into **any**
writable directory and launches it. No single narrow control closes V5: config-immutability alone is defeated by
relocation, and AppLocker cannot path-restrict a writable-tree binary.

## Decision — the W^X invariant

The canonical hosted-workspace security invariant is now:

> **TENANT-WRITABLE ⇒ NON-EXECUTABLE   ·   TENANT-EXECUTABLE ⇒ NON-WRITABLE**

No hosted tenant may have a location that is simultaneously writable and capable of executing code (EXE / DLL /
Script / MQL-EX5 / copied signed MetaQuotes binary / portable MT5 runtime). Enforced by **five coordinated
controls**, driven by **ONE** canonical source of truth
(`hosted_workspace.applocker_policy.HOSTED_WRITABLE_SUBDIRS` / `HOSTED_CODE_SUBDIRS`) consumed by **both** the
NTFS ACL and the AppLocker policy — no duplicate manually-maintained list.

1. **G5v2 — inverted ACL.** Runtime root + MQL5 **code** dirs: tenant **Read+Execute only** (SYSTEM/Admins
   Full). Tenant **Modify** granted back only on the enumerated **data** subdirs. `config\common.ini` + all code
   dirs carry an explicit tenant **Deny-write**. (`workspace_acl.build/verify_workspace_acl_v2`; v1 untouched.)
2. **AppLocker per-tenant POSITIVE execution allowlist (revised 2026-08-12, CA — Option A).** The first design
   (deny each enumerated writable path) was found unsound by the 10-dimension review: a path-free Everyone Exe
   Allow under a writable-*blocklist* misses tenant-writable roots outside the accounts tree (`C:\Users\Public`,
   `C:\ProgramData\<user>`, suffixed RDS profiles). Replaced with the **positive** model: for the specific
   **tenant SID** (Decision 3 — *not* `BUILTIN\Users`), a single `Deny(*)` whose **Exceptions are exactly the
   executable allow surface** — the RX managed `terminal64` + the approved `%SYSTEM32%` session binaries
   (`HOSTED_SESSION_ALLOW`). Deny > Allow, so the tenant can execute **only** the allow surface; a copied signed
   `terminal64.exe` runs from **nowhere** (Public, ProgramData, `%TEMP%`, another drive, a writable subdir) — the
   *location is irrelevant*. `Dll`/`Script` need no per-tenant deny: the base is publisher-only / deny-by-default,
   so a planted DLL/script is already denied wherever it lands. Think **executable allow surface**, not
   **writable deny surface**.
3. **MetaEditor denied.** The Exe MetaQuotes publisher rule is pinned `BinaryName=terminal64.exe` (embedded
   signature name — rename-proof); `metaeditor64.exe` and every other MetaQuotes tool are denied. No developer
   exception in this stream. **This pin lives in the MACHINE-WIDE BASE allow model** (`generate_base_policy` + the
   committed `guvfx-hosted-{auditonly,enforce}.xml`), NOT behind `HOSTED_WX_ISOLATION_ENABLED` — it is a
   deny-by-default TIGHTENING (it only DENIES metaeditor64; it opens nothing) consistent with ADR-0042, so it
   ships whenever the base policy is (re)deployed. **RULE-11 pre-Enforce control:** because the pinned literal
   must equal `terminal64.exe`'s real embedded `BinaryName`, an **Enabled-mode base redeploy must first prove
   on-host that they match** (else it would deny `terminal64.exe` itself and break MT5 for every tenant incl.
   Customer Zero); until that positive control passes the pin is exercised in **AuditOnly only**. `flags.py`
   documents the same boundary (the pin is deliberately NOT gated by the W^X flag).
4. **Golden vetted-empty.** The golden's MQL5 code dirs ship **empty**; `Test-GoldenImage` fails on any
   unapproved EA/EX5/script/indicator or any source containing a `#import` to a non-approved library.
5. **`AllowDllImport=0` tenant-immutable** (defence-in-depth behind W^X, not relied upon): written at launch
   **and** the Deny-write ACE on `common.ini` prevents the Options toggle / on-exit rewrite from flipping it.

## Composition & application wiring (the W^X Deny is NOT inert)

The per-tenant W^X `Deny(*)` is produced by ONE definition (`tenant_wx_deny_rules`) and reaches an effective
policy through two symmetric callers, mirroring the NTFS side (`build_workspace_acl_plan_v2` +
`Set-GuvfxWorkspaceAclV2.ps1`):

- **Compose (backend, pure, tested):** `compile_effective_wx_policy(base, tenants)` = base + one per-tenant
  `Deny(*)` (from the same `HOSTED_*` source). Under W^X this Deny **supersedes** the legacy shell-binary denies
  (`cmd`/`powershell`/… are denied because they are not among the exec-allow exceptions), so the W^X composer
  emits `base + Deny(*)`, not `tenant_deny_rules`. An end-to-end test composes the effective policy and asserts a
  copied **signed** `terminal64` is denied from Public / ProgramData / another drive / a writable subdir while the
  RX `terminal64` + session binaries run — with the base MetaQuotes publisher Allow still present, proving the
  Deny is load-bearing.
- **Apply (host):** `Set-GuvfxAppLockerTenant.ps1 -Mode MergeWx -FragmentPath …` merges the **backend-produced**
  `tenant_wx_deny_fragment` (no XML built in PowerShell), after validating it is one Exe `Deny` bound to this
  account's tenant SID + `4d54` rule id. `-Mode Remove` strips it (legacy or W^X — both carry the account-tagged id).

End-to-end **application** from `slot_preparation` (calling the composer and pushing to the host applier) is gated
behind `HOSTED_WX_ISOLATION_ENABLED` + the host-executor seam (both DARK) — exactly as the G5v2 NTFS plan is.

## Enforcement / regression guards (CI)

- `assert_wx_deny_invariants` — the tenant fragment is a single Exe `Deny(*)` whose **Exceptions equal exactly**
  the executable allow surface (an extra exception = a hole; a missing one = fail-safe availability, never a
  hole); grants nothing; correct SID.
- `assert_wx_no_writable_executable_intersection` — no path in the executable allow surface is under a
  tenant-writable subdir (TENANT-EXECUTABLE ⇒ NON-WRITABLE).
- `assert_wx_subdir_lists_disjoint` — machine-checks the MINIMALITY relationship (no `HOSTED_WRITABLE_SUBDIRS`
  entry equals, contains, or is contained by a `HOSTED_CODE_SUBDIRS` entry), previously asserted only in prose.
- `verify_workspace_acl_v2` also rejects any **foreign Allow principal** on a writable/code subdir (not just the
  tenant's own ACEs), and the host applier snapshots + restores **every** mutated path's ACL on rollback.
- `assert_allow_model_invariants` now also requires the Exe MetaQuotes rule be `BinaryName`-pinned.
- `verify_workspace_acl_v2` — RULE-11 positive/negative self-control; fail-closed on a tenant-writable root/code
  dir, a missing path, or a missing Deny-write. A test **monkeypatches the classifier** to prove the self-control
  actually fires (a test that only checks a good read-back would pass even if the self-check call were deleted).
- **Golden gate (`Test-GuvfxGoldenMql.ps1`) is fail-closed (RULE 11):** a missing code dir is an offender
  (`expected_code_dir_absent`), enumeration errors are captured (`code_dir_unscannable`, never swallowed), a
  reparse-point code dir is rejected, and a **runtime positive control** (a seeded `#import` source + stray `.ex5`
  that the same detector must flag) runs before any clean `vetted_empty` is emitted. A Python static guard asserts
  these guards remain in the script (CI has no PowerShell).
- The canonical `HOSTED_WRITABLE_SUBDIRS`/`HOSTED_CODE_SUBDIRS` are imported by `workspace_acl` from
  `applocker_policy` (same objects — tested) so NTFS and AppLocker can never diverge.

## Rollout

Everything ships behind **`HOSTED_WX_ISOLATION_ENABLED`** (default OFF); the host-executor seam is `None` in-repo
(fail closed, no host contact). **`REMOTEAPP_ISOLATION_CERTIFIED` stays WITHHELD** until a separate on-host
behavioural certification (CZ before-fingerprint → apply G5v2 to a disposable tenant → normal-MT5 validation →
W^X escape battery [portable-copy, MetaEditor, `common.ini` mutation, `#import`, writable EXE/DLL/Script] →
restart persistence → rollback → CZ after-fingerprint) demonstrates the invariant with Customer Zero preserved.
The `HOSTED_WRITABLE_SUBDIRS` completeness (MT5 needs no unlisted writable path) + minimality (no writable path is
a code-load dir) + the terminal64 embedded `BinaryName` are host-soak-verified (RULE 11).

## Consequences

- Once certified, the ADR-0041 chain becomes truthfully achievable: `REMOTEAPP_ISOLATION_CERTIFIED` →
  `HOSTED_OBSERVATION_CERTIFIED` (via the STREAM 10C signed observer EXE) → `WORKSPACE_READY` →
  `AUTONOMOUS_ONBOARDING_CERTIFIED` → `FIRST_UNASSISTED_USER_CERTIFIED`.
- A future developer/strategy-authoring tier that re-admits MetaEditor/MQL authoring requires its **own** threat
  model and certification — explicitly out of this stream.
- Out of scope for STREAM 10D (tracked elsewhere): MT5 Market-product network egress; OS-level 0-day local
  privilege escalation (host patch posture).

### Residual — signed-DLL side-load (split into a reducible half and an irreducible half)

The `Dll` collection allows Everyone the **Microsoft** and **MetaQuotes** publishers with `BinaryName=*` /
version `*`–`*` (unlike the Exe MetaQuotes rule, which is `BinaryName`-pinned to `terminal64.exe`). The per-tenant
W^X `Deny(*)` is currently **Exe-only** and does not gate DLL loads. So while an **unsigned** planted DLL is fully
denied (publisher-only), a tenant could obtain a **genuinely signed** MS/MetaQuotes DLL (signed-but-vulnerable or
signed-proxy) and force its load via an HKCU COM `InprocServer32` hijack against a tenant-context host
(`sihost`/`taskhostw`) — a **signed-downgrade / bring-your-own-vulnerable-DLL** primitive. This residual is **two
distinct sub-cases that must NOT be conflated** (an earlier revision wrongly declared the whole thing
"un-closeable"):

- **(ii) REDUCIBLE — a planted signed DLL LOADED from a tenant-writable location.** This is a DLL *load*, which the
  `Dll` collection **does** evaluate by path + exceptions; it currently passes only because the publisher rule is
  `BinaryName=*` and **location-agnostic**. Because the attacker must plant the DLL somewhere **tenant-writable**,
  it is closeable in code by the **same location technique already built for Exe**: a **per-tenant `Dll` `Deny(*)`
  whose exceptions are exactly the non-tenant-writable RX DLL-load locations** (symmetric to the Exe W^X
  `Deny(*)`), which denies the planted load **regardless of publisher/signature**; plus the MetaQuotes `Dll`
  `BinaryName`/version pin (shrinks the MetaQuotes half). **This half is NOT accepted — it is a committed code
  closure, deferred only for a RULE-11 data reason, not a fundamental one:** the exception set (the exact
  non-writable OS + MT5 DLL-load directories) must be **host-soak-derived**, because `%WINDIR%`/`%SYSTEM32%`
  contain tenant-writable subdirs (`%WINDIR%\Temp`, `System32\spool\drivers\color`, …) that must be excluded from
  the exceptions or the writable-subdir hole re-opens — the identical reason the base `Dll` rule is publisher-only.
  A **blind** hardcoded exception set risks either re-opening the hole (too broad) or a **fail-closed MT5 outage**
  (too narrow — legitimate signed OS / side-by-side DLL loads denied), which is exactly the RULE-11 trap. It is
  therefore built + applied at STREAM 10E cert time from the soak, **not guessed in-repo now**.
- **(i) IRREDUCIBLE — in-process *use* of a legitimately-signed, mandatorily-allowed OS DLL already resident in a
  non-writable path** (e.g. abusing a `kernel32` that must load for MT5/the OS to run). AppLocker gates DLL *loads*,
  not function *calls*; no repository control can distinguish legitimate signed-DLL use from abuse — only on-host
  **behaviour** can. This half is genuinely **OUTSIDE the repository security boundary**, the SAME architectural
  limitation as the `#import`-of-a-signed-OS-DLL class the Chief Architect already accepted. It is a
  **formally-justified residual accepted in the architecture** (the Sponsor's second acceptance path), parallel to
  the ADR-0042 `%WINDIR%` LOLBIN residual.

**Disposition (hard, not advisory) — the `8004` battery must not rubber-stamp:** before
`REMOTEAPP_ISOLATION_CERTIFIED`, sub-case **(ii) must be REDUCED IN CODE** — the soak-derived per-tenant `Dll`
`Deny(*)`-with-non-writable-exceptions + the MetaQuotes `Dll` `BinaryName`/version pin **applied**, not merely a
behavioural "looks-blocked" result. The `8004` escape battery **must include a signed-DLL COM-hijack-from-a-
writable-location case** and demonstrate it **blocked by that applied control** (belt-and-suspenders, not a
substitute for the code closure). Sub-case **(i)** remains the accepted residual, bounded by the same battery to
trusted-signer code reaching no attacker-controlled native execution. Neither is ever waived.
