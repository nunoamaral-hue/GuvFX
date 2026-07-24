# 0016 — PRESENT attribution of a running beta slot process (cross-account observation)

- Date: 2026-07-25
- Status: **Accepted** — Option A (Nuno, 2026-07-25: "ADR 0016 is approved in principle. Proceed with Option A."
  plus the intrinsic-ACL refinement below). Implementation authorised through the governance pipeline.
- Related: [ADR 0015](0015-unprivileged-process-observation.md) (unprivileged observation, which this completes);
  evidence `evidence/b3p2-install/wmi_attribution_service_context_2026-07-25.md` (the conclusive negative —
  lands via PR #207, a Nuno-gated evidence PR).

## Context — the boundary this ADR crosses

Unprivileged `ABSENT` observation is proven under the deployed least-privilege identity
`NT SERVICE\GuvFXBetaAgent` (Toolhelp enumeration, two-stage path normalisation, WMI session pre-filter). But
`PRESENT` — attributing a **running** slot runtime by its mandatory evidence (exact slot executable path +
owner SID) — is **conclusively impossible** for that identity via any documented unprivileged API:

- `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION / _INFORMATION)` → **denied** for a process owned by another
  account.
- WMI `Win32_Process.ExecutablePath` / `GetOwner` / `GetOwnerSid` → **denied** (`rc=2`).

The root cause is the per-slot **least-privilege isolation** itself: `GuvFXBetaAgent` (the observer) and
`guvfx_b_slot1` (the slot runtime owner) are DISTINCT low-privilege accounts, and neither may inspect the
other's process image/owner.

### The principals (stated explicitly)

- **Owns the MT5 slot process:** `guvfx_b_slot<n>` (the per-slot runtime identity; launched via the approved
  batch-logon task, Session 0, `/portable`). As the creator/owner it holds `WRITE_DAC`/`WRITE_OWNER` on its
  own process object.
- **Needs to observe it:** `NT SERVICE\GuvFXBetaAgent` (the beta agent service; already holds `Modify` on the
  slot directory and manages the runtime's lifecycle).

## Decision — Option A: a per-process access grant issued at launch by the owning slot identity

A thin, reviewed, ASCII-only launch **wrapper**, run **as `guvfx_b_slot<n>`** by the approved launch task,
(1) creates `terminal64.exe /portable` **suspended**, (2) adds ONE discretionary ACE to **that process
object's** DACL granting `NT SERVICE\GuvFXBetaAgent` `PROCESS_QUERY_LIMITED_INFORMATION (0x1000)` +
`READ_CONTROL (0x20000)`, verifies the ACE, then (3) resumes the process. The owner holds `WRITE_DAC` on its
own process, so no Windows privilege is required.

| Property | Value |
|---|---|
| Capability granted | `PROCESS_QUERY_LIMITED_INFORMATION` + `READ_CONTROL` — query-only. NOT `PROCESS_VM_READ`, terminate, suspend, set-info, token, or `PROCESS_QUERY_INFORMATION`. |
| Scope | **process-scoped** — one DACL ACE on one process object (one PID). |
| Survives restart | **No** — the ACE lives on the process object; it is destroyed when the process exits. Each launch re-applies it. |
| Weakens slot isolation | **No** — query-only visibility of the slot's OWN runtime, which the service already manages; symmetric with the existing slot-dir `Modify` ACL. No privilege, no SYSTEM, no broker, no `SeDebugPrivilege`. |

Then observe (`win_slot_ops.query_slot_process`) reads every mandatory PRESENT field without widening the
service beyond query-only: image path via `QueryFullProcessImageNameW` and owner SID via the process **object
owner** (`GetSecurityInfo(handle, SE_KERNEL_OBJECT, OWNER_SECURITY_INFORMATION)`, read from the SAME
`PROCESS_QUERY_LIMITED_INFORMATION | READ_CONTROL` handle) — never the token user (which needs the excluded
`PROCESS_QUERY_INFORMATION`). Match = image beneath slot AND object-owner SID == slot identity SID. With the
grant the slot's OWN process becomes openable; the production terminal stays unopenable (no ACE) and is
excluded by the WMI session filter exactly as today. The four fail-closed observation states (ADR-0015) are
unchanged: a missing/failed grant leaves the slot process unopenable → `UNAVAILABLE`, never a false `PRESENT`
or `ABSENT`.

Options **B** (privileged observation broker — broad standing authority + IPC trust edge) and **C** (`C1`
`SeDebugPrivilege` too broad; `C2` token default-DACL — rejected, propagates the grant to every child; `C3` job
object; `C4` redefine PRESENT evidence — rejected, relaxes the identity-binding invariant) are documented in the
option comparison below and NOT adopted. B is held in reserve only if a per-process launch grant proves
infeasible on the host.

## Refinement — the ACE is an INTRINSIC property of the process object (no revocation)

Nuno, 2026-07-25: *"Treat the process ACL as an intrinsic property of the process object. Do not build any
explicit ACL revocation step."* The lifecycle is:

```
launch → grant → observe → process exits → process object destroyed → ACE naturally disappears
```

### Proof that no explicit cleanup is required

The grant is a discretionary ACE in the **in-kernel process-object security descriptor**. It is never written
to the registry, the filesystem, the process token, or LSA (it is a DACL, not a SACL — no audit/LSA
involvement). Windows guarantees:

1. **The ACE cannot outlive the kernel object.** A process object's security descriptor is part of the object;
   when the object is destroyed at process exit, the descriptor — including our ACE — is freed with it. There
   is no persistent artefact to revoke.
2. **Not inherited by children.** Process kernel objects have no inheritable (OI/CI) ACEs; a child process's
   security descriptor comes from the creator token's *default* DACL, not a copy of the parent object's DACL.
   So `terminal64`'s children carry no grant. (This is precisely why Option A beats C2: C2 mutates the token
   default DACL, which WOULD propagate to every child — a residue path Option A does not have.)
3. **Nothing persists across restart.** Kernel objects do not survive reboot; each launch re-applies the ACE to
   a fresh PID; the launch task carries the wrapper invocation, not the ACE; the grantee is always the fixed
   service SID, so nothing accumulates.
4. **The one honest nuance:** a terminated process's object can linger as a zombie while any handle to it stays
   open, during which the (query-only, dead-process) ACE technically still exists. It is a capability of no
   consequence and self-clears when the last handle closes. The observe path closes every handle in a `finally`
   (`win_slot_ops.py` `CloseHandle`) and never duplicates one; the wrapper closes its creator handles after
   resume. And a terminated PID is never enumerated by the Toolhelp snapshot, so it can never surface as a
   candidate or a false `PRESENT`.

Therefore there is no artefact to revoke and no cleanup path to maintain. This is the intrinsic-ACL model.
Contrast: an LSA privilege grant, a file/registry ACL, or a token default-DACL WOULD persist and require
revocation.

## Fail-closed behaviour (D7) — launch suspended, grant, verify, resume

The wrapper creates `terminal64` with `CREATE_SUSPENDED`, applies **and reads back** the ACE, and only then
`ResumeThread`. On ANY grant/verify failure it calls `TerminateProcess` on the retained child handle and exits
non-zero. The runtime therefore executes **zero instructions** — never connects to a broker — until the grant
is confirmed. Rationale: a `terminal64` left running without the grant is unopenable AND in Session 0 (the
observer's session), so it is not WMI-session-excluded; observe returns `UNAVAILABLE`, which correctly blocks
the observe-gated lifecycle — but a live, un-attributable MT5 that could trade is the exact hazard the design
exists to prevent. The wrapper is the parent and holds a direct handle to the precise PID it created, so the
kill is unambiguous and can never touch the production terminal (different owner, no handle) — it never matches
by image name.

## Windows-security invariants (must hold; host-proven — see Verification)

- **Additive ACE (read-modify-write).** The wrapper reads the existing DACL, appends ONE `CommonAce`
  (`AccessAllowed`, mask `0x21000`, service SID), and writes it back. It NEVER constructs and sets a one-ACE
  DACL — a wholesale replace would strip the owner's default DACL, and the owner (`guvfx_b_slot<n>`) then loses
  its own `PROCESS_TERMINATE`, silently disabling the slot's STOP task while launch/observe stay green. A
  null/absent DACL is a **fail-closed abort**, never synthesised.
- **Object owner == account SID holds ONLY for a non-admin creator.** For a standard-user token, `TokenOwner`
  defaults to `TokenUser`, so the process object owner equals `guvfx_b_slot<n>`'s SID. This is load-bearing for
  the whole owner-evidence switch and MUST be host-proven with a positive control. It must NEVER be reused for
  an admin identity, nor if the "Default owner for objects created by members of the Administrators group"
  policy is set to Administrators.
- **`READ_CONTROL` in every requested access mask.** `_open_process` opens at `PQLI | READ_CONTROL`; a mask
  that omits `READ_CONTROL` can still open a process but then fails the object-owner read → owner `None` →
  permanent `UNAVAILABLE`. This regression is invisible off-host, so it is pinned by a source-invariant test
  and a host positive control (RULE 11).
- **Constrained Language Mode.** If the host enforces PowerShell CLM for non-admin identities (AppLocker/WDAC),
  `Add-Type` and reflection P/Invoke are both forbidden, so the wrapper's C# cannot run as `guvfx_b_slot<n>` —
  discoverable ONLY as the slot identity, not as admin. This is a host gate before commissioning; the fallback
  is a hash-pinned precompiled helper exe (the WinSW pin model), which also sidesteps RULE 9 parsing.

## Wrapper integrity + placement

- **Location/ACL (D1).** One shared wrapper at `C:\GuvFX\beta\launcher\slot_launch.ps1`, outside every slot
  tree and the golden image, ACL'd like the golden image: inheritance broken; Administrators + SYSTEM Full;
  each `guvfx_b_slot<n>` `ReadAndExecute` only. The service account is intentionally NOT granted on the
  launcher, because the wrapper runs AS the slot identity (the launch task's principal), not as the service —
  so the service never needs to read or execute it (least privilege). A slot is structurally unable to rewrite
  its own launcher. The install VERIFY reads the launcher DACL back (and re-hashes the staged wrapper) in
  both `-Apply` and `-VerifyOnly`, failing on any slot-writable ACE or a hash mismatch.
- **Contents integrity (D6).** The approved-tasks digest pins the task INVOCATION, not the wrapper bytes, and
  `manifest.py` `IMPL_MODULES` is `.py`-only. Contents are protected by (1) the admin-only-write ACL
  (prevention — tampering requires admin, the same trust level as tampering `install_pool.ps1`) and (2) an
  install-time SHA-256 pin (`$LaunchWrapperSha256`) refused on mismatch/absence before laydown and at VERIFY.
  A runtime agent-side re-hash is a documented follow-up hardening (its residual value is admin-vs-admin only,
  since only an admin can write the wrapper); building it would additionally require granting the service
  account Read on the launcher (it has none today), added at install with rationale at that time.
- **ASCII-only + RULE 9.** The wrapper is pure ASCII, no BOM, and validated by
  `[Parser]::ParseFile` under Windows PowerShell 5.1 (with a negative control) before first execution.
  `ParseFile` validates the PowerShell shell but treats the embedded C# as an opaque literal, so a host
  compile + interop self-test (GetKernelObjectSecurity on its own process) is run before launching terminal64,
  exactly like the existing LSA self-test.

## Launch-gate change (F3)

The launch task's executable changes from `terminal64.exe` (beneath the slot) to `powershell.exe` running the
wrapper — so `win_primitives.inspect_task`'s launch branch moves from "executable beneath slot" to the
terminate-branch pattern: executable is `powershell.exe`/`pwsh.exe`; the arguments invoke the fixed wrapper via
`-File`, name this slot's own `terminal64` as a path prefix (trailing-separator safe), carry a well-formed
service SID token (SHAPE only — the primitive layer must not learn the GuvFX service SID VALUE; the value is
bound above by the arguments digest + install read-back), retain a bare `/portable`, and contain no
`-Command`/`-EncodedCommand`. `/portable` stays LITERALLY in the pinned task arguments (so
`portable_switch_present` and the digest keep working) while the wrapper hard-codes `/portable` as the switch
it passes to `terminal64` (no injection surface). This change lands ATOMICALLY with the install change, or
every wrapper-based launch is judged `executable_outside_slot` and BLOCKS.

## Option comparison (unchanged from the proposal, summarised)

| Dimension | A (per-PID grant at launch) | B (privileged broker) | C4 (redefine evidence) |
|---|---|---|---|
| Least privilege | ✅ narrowest, query-only, per-PID | ❌ broad standing authority | ✅ no grant, weaker evidence |
| Attack surface | ✅ one short-lived ACE | ❌ new privileged service + IPC | ➖ none added, weaker check |
| Scope | process-scoped (one PID) | global | n/a |
| Survives restart | no (re-applied per launch) | yes (standing component) | n/a |
| Weakens isolation | no | indirectly (trust edge) | **yes** (identity binding) |
| Auditability | ✅ deterministic ACE | ➖ mediated but broad actor | ➖ weaker proof |
| Rollback / recovery | ✅ trivial, fail-closed | ❌ heavier | ✅ trivial |
| Production risk | low | higher | low but correctness risk |
| Implementation | moderate | high | low (relaxes an invariant) |

## Verification (host, authoritative — RULE 11 positive + negative controls, under `NT SERVICE\GuvFXBetaAgent`)

Before resuming the slot-1 `TOMBSTONE → RELEASE` proof, prove on the host:

1. **CLM gate** — the Add-Type ACE mechanism compiles and round-trips `GetKernelObjectSecurity` on its own
   process when run AS `guvfx_b_slot1` (not as admin). If CLM blocks it, switch to the precompiled-exe fallback.
2. **Grant control** — from the service identity, `OpenProcess(slot pid)` is DENIED **before** the grant
   (negative control proving the ACE is what flips it) and ALLOWED at `PQLI | READ_CONTROL` **after**, yielding
   the exact slot image path and an object-owner SID EQUAL to `LookupAccountName(guvfx_b_slot1)`.
3. **Owner equivalence** — object-owner SID == token-user SID for `guvfx_b_slot1`, stable across VERIFY/STOP.
4. **Additivity** — after the grant the slot user's STOP task still terminates its own runtime (launch → grant
   → STOP → observe `ABSENT`). A DACL read-back shows the service ACE present AND the owner's default ACEs
   intact.
5. **Negative controls hold** — the production terminal stays denied/WMI-session-excluded throughout; a
   grant-absent slot `terminal64` reads `UNAVAILABLE`, never `ABSENT`.
6. **No residue (D8)** — launch → PRESENT → STOP → `ABSENT` (not `UNAVAILABLE`) → relaunch (new PID,
   generation+1, grant re-created) with no residual openable PID, the slot token's default DACL unchanged
   pre/post (distinguishes A from C2), and a child of `terminal64` carrying no service-SID ACE.

## Reversal path

Additive. Reverting the observe side restores the token-based owner read (re-introducing the cross-account
PRESENT blocker); reverting the launch side restores the direct-`terminal64` task action (removing the grant).
Both revert cleanly with no persistent residue, because the ACE was never persisted.
