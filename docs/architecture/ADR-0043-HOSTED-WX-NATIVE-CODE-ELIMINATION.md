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
   exception in this stream.
4. **Golden vetted-empty.** The golden's MQL5 code dirs ship **empty**; `Test-GoldenImage` fails on any
   unapproved EA/EX5/script/indicator or any source containing a `#import` to a non-approved library.
5. **`AllowDllImport=0` tenant-immutable** (defence-in-depth behind W^X, not relied upon): written at launch
   **and** the Deny-write ACE on `common.ini` prevents the Options toggle / on-exit rewrite from flipping it.

## Enforcement / regression guards (CI)

- `assert_wx_deny_invariants` — the tenant fragment is a single Exe `Deny(*)` whose **Exceptions equal exactly**
  the executable allow surface (an extra exception = a hole; a missing one = fail-safe availability, never a
  hole); grants nothing; correct SID.
- `assert_wx_no_writable_executable_intersection` — no path in the executable allow surface is under a
  tenant-writable subdir (TENANT-EXECUTABLE ⇒ NON-WRITABLE).
- `verify_workspace_acl_v2` also rejects any **foreign Allow principal** on a writable/code subdir (not just the
  tenant's own ACEs), and the host applier snapshots + restores **every** mutated path's ACL on rollback.
- `assert_allow_model_invariants` now also requires the Exe MetaQuotes rule be `BinaryName`-pinned.
- `verify_workspace_acl_v2` — RULE-11 positive/negative self-control; fail-closed on a tenant-writable root/code
  dir, a missing path, or a missing Deny-write.
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

### Accepted residual — signed-DLL side-load (MEDIUM, must be behaviourally certified)

The `Dll` collection allows Everyone the **Microsoft** and **MetaQuotes** publishers with `BinaryName=*` /
version `*`–`*` (unlike the Exe MetaQuotes rule, which is `BinaryName`-pinned to `terminal64.exe`). The per-tenant
W^X `Deny(*)` is **Exe-only** and does not gate DLL loads. So while an **unsigned** planted DLL is fully denied
(publisher-only), a tenant could copy a **genuinely signed** MS/MetaQuotes DLL of their choosing into a writable
location and force its load via an HKCU COM `InprocServer32` hijack against a tenant-context host
(`sihost`/`taskhostw`) — a **signed-downgrade / bring-your-own-vulnerable-DLL** primitive (a *higher bar* than the
closed unsigned path: it needs a signed-but-vulnerable or signed-proxy DLL to reach attacker-controlled native
code). The broad Microsoft OS-DLL rule cannot be safely `BinaryName`-pinned without breaking OS/MT5 updates.
**Disposition (parallel to the ADR-0042 `%WINDIR%` LOLBIN residual):** recorded as an accepted **MEDIUM**
residual; where feasible the MetaQuotes DLL rule should be `BinaryName`/version-pinned to the specific MT5 DLLs
(soak-derived), and the on-host `8004` escape battery **must include a signed-DLL COM-hijack case** — a tenant
COM-hijacking a signed DLL from a writable location — that must be shown blocked (or bounded to trusted-signer
code with no native-exec) **before `REMOTEAPP_ISOLATION_CERTIFIED` is emitted**.
