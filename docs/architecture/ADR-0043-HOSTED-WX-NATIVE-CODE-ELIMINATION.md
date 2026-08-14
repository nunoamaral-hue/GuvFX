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
   *location is irrelevant*. `Dll`/`Script` need no per-tenant deny **for the UNSIGNED case**: the base is
   publisher-only / deny-by-default, so an **unsigned** planted DLL/script is already denied wherever it lands.
   (A **signed** DLL planted in a tenant-writable location is a *different* case — it passes the publisher Allow —
   and is the **reducible half (ii)** of the signed-DLL residual below, closed by a per-tenant `Dll` `Deny(*)` at
   cert time.) Think **executable allow surface**, not **writable deny surface**.
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
a code-load dir) + the terminal64 embedded `BinaryName` are host-soak-verified (RULE 11). **The full turnkey
procedure — numbered checklist, complete 8004 escape battery, evidence collection, pass/fail criteria, rollback
decision tree, CZ before/after, and the Nuno-only manual actions — is
`docs/operations/hosted-workspace/STREAM_10E_HOST_CERTIFICATION_RUNBOOK.md`, and it runs on a SEPARATE DISPOSABLE
host, never Customer Zero (Sponsor decision 2026-08-14). PowerShell payloads:
`backend/terminal_provisioning/windows/escape_battery/`.**

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
  closure. The MECHANISM now SHIPS in-repo, built + tested (STREAM 10E):** `applocker_policy
  .tenant_wx_dll_deny_fragment(account, sid, nonwritable_exec_dirs)` + `assert_wx_dll_deny_invariants`, applied
  host-side by `Set-GuvfxAppLockerTenant.ps1 -Mode MergeWx` (which now accepts an Exe **or** Dll W^X fragment). It
  fail-closes on an empty exception set (would deny every DLL incl. the OS), a `*` exception (fail-open), or an
  exception that is **under or COVERS** a tenant-writable subdir (`...\TERMINAL\*` is rejected because it covers
  `...\MQL5\Files`). Only the exact exception **DATA** is deferred, for a RULE-11 reason, not a fundamental one:
  the exact non-writable OS + MT5 DLL-load directories must be **host-soak-derived**, because `%WINDIR%`/`%SYSTEM32%`
  contain tenant-writable subdirs (`%WINDIR%\Temp`, `System32\spool\drivers\color`, …) that must be excluded or the
  writable-subdir hole re-opens — the identical reason the base `Dll` rule is publisher-only. A **blind** hardcoded
  set risks either re-opening the hole (too broad) or a **fail-closed MT5 outage** (too narrow), the RULE-11 trap.
  The mechanism is therefore applied from the soak-derived data at STREAM 10E cert time — see
  `docs/operations/hosted-workspace/STREAM_10E_HOST_CERTIFICATION_RUNBOOK.md` §5 — **not guessed in-repo now**.
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

## Addendum B — Host-level co-residency guard (coarse isolation complement) — adopted 2026-08-14

**Context.** The W^X model above isolates tenants that **share one physical host** from each other, but its
behavioural certification (`HOSTED_REMOTEAPP_ISOLATION_CERTIFIED`) is host + credential gated and still
outstanding. Meanwhile the Sponsor wants to invite a small trusted beta. The question that forced this decision:
*can beta users be isolated from Customer Zero while the in-host isolation cert completes?* The node model already
binds each hosted workspace to exactly one `TerminalNode` (`execution_node` == `workspace_node` ==
`account.terminal_node`), but the allocator (`allocate_workspace_node`) selected the first ACTIVE deliverable node
by ascending id — and **Customer Zero is node id 1** — so a new beta workspace would have been allocated onto
Customer Zero's live, money-bearing host *first*. Co-residency was the default, not the exception.

**Decision.** Add a **coarse-grained, fail-closed, flag-gated** allocation guard, orthogonal to and composable
with the in-host W^X controls: a **NON-Customer-Zero** hosted workspace may **never** be bound to a `TerminalNode`
that (a) currently serves a Customer Zero account (derived live from the DB — no host address hardcoded), or (b)
exposes an rdp_host listed in `settings.HOSTED_BETA_FORBIDDEN_RDP_HOSTS`. Customer Zero itself is unaffected (it
may occupy its own node). "Who is Customer Zero" keeps its **single** definition — `RESERVED_CUSTOMER_ZERO` — so
the AppLocker and allocation layers never diverge (security RULE 6).

**Mechanism.**
- `hosted_workspace/tenant_isolation.py` — `customer_zero_account_ids()`, `forbidden_execution_node_ids()`,
  `assert_allocation_allowed()`, `CrossTenantCoResidencyError`. `forbidden_execution_node_ids()` derives the
  Customer Zero node set from **both** `account.terminal_node` **and** the authoritative hosted-workspace
  bindings (`execution_node` / `workspace_node`) — the two are kept equal only by the allocator, and
  `account.terminal_node` can be cleared independently (e.g. `execution.views.unassign_account`) while the CZ
  workspace keeps running — plus the explicit `HOSTED_BETA_FORBIDDEN_RDP_HOSTS` belt.
- Flag `HOSTED_TENANT_NODE_ISOLATION_ENABLED` (`hosted_workspace/flags.py`), **default OFF → zero behaviour
  change**. Flipping ON is always safe: it can only *refuse* a co-residency, never create one; with no separate
  host present it fails **closed** (no allocation) rather than co-reside.
- **The execution-node single writer** `assign_workspace_execution_node` enforces the guard on the
  `workspace.execution_node` binding for every caller, raising **before** any generation bump / write. The
  **other** binding surface — `account.terminal_node` — is kept safe by the callers: the allocator pre-filters
  forbidden nodes out of candidate selection, and the `provision_hosted_execution` command **pre-checks** with
  `assert_allocation_allowed` before its own `account.terminal_node` write (so a refusal persists nothing and
  returns a clean error, not a traceback). The delivery single writer `assign_workspace_node` carries the same
  guard for the RemoteApp session host.
- The allocator additionally **skips** forbidden candidates and returns a **distinct** reason
  `ALLOC_CZ_NODE_FORBIDDEN` (operator action = "provision a separate non-CZ host"), distinct from "no capacity" /
  "no rdp_host"; the runner counts it as a distinct `cz_forbidden` outcome, not an error.

**Relationship to the W^X model.** This is the **complement, not a replacement**: W^X reduces the risk *between
tenants sharing a host*; the co-residency guard removes Customer Zero's live account from the shared-host blast
radius entirely. Together they let a supervised beta run on a **separate physical host** (ideally the same
disposable host provisioned for the STREAM 10E cert, promoted to the beta pool after it passes) with the
un-certified-but-applied W^X controls only ever needing to hold *between disposable beta tenants*, never between a
beta tenant and Customer Zero's money. It does **not** by itself emit `REMOTEAPP_ISOLATION_CERTIFIED`; that marker
still requires the on-host escape battery.

**Consequences / residual.** The guard assumes the beta pool is a **distinct host** from Customer Zero — that
host is an infrastructure (Sponsor) action, not a repository one. If no non-CZ host exists and the flag is ON,
hosted allocation fails closed (intended). Weak isolation (a separate *session* on Customer Zero's box) is
explicitly **rejected** before cert: a code-execution escape there could still reach the live terminal.
