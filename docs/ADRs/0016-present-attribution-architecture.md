# 0016 — PRESENT attribution of a running beta slot process (cross-account observation)

- Date: 2026-07-25
- Status: **Proposed** (for Nuno's decision; no implementation until approved)
- Related: [ADR 0015](0015-unprivileged-process-observation.md) (unprivileged observation);
  evidence `evidence/b3p2-install/wmi_attribution_service_context_2026-07-25.md` (the conclusive negative).

## Context — the boundary this ADR exists to cross

Unprivileged `ABSENT` observation is proven under the deployed least-privilege identity
`NT SERVICE\GuvFXBetaAgent` (Toolhelp enumeration, two-stage path normalisation, WMI session pre-filter). But
`PRESENT` — attributing a **running** slot runtime by its mandatory evidence (exact slot executable path +
owner SID) — is **conclusively impossible** for that identity via any documented unprivileged API:

- `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION / _INFORMATION)` → **denied** for a process owned by another
  account.
- WMI `Win32_Process.ExecutablePath` / `GetOwner` / `GetOwnerSid` → **denied** (`rc=2`) — they run under the
  caller token and need the same per-process access.

Only system-table fields (`SessionId`, `CreationDate`, `ParentProcessId`) are available. The root cause is the
per-slot **least-privilege isolation** itself: `GuvFXBetaAgent` (the observer) and `guvfx_b_slot1` (the slot
runtime owner) are DISTINCT low-privilege accounts, and neither can inspect the other's process image/owner.

### The principals (stated explicitly, per the decision request)

- **Owns the MT5 slot process:** `guvfx_b_slot1` (the per-slot runtime identity; launched via the approved
  batch-logon task, Session 0, `/portable`). As the creator/owner it holds `WRITE_DAC`/`WRITE_OWNER` on its
  own process object.
- **Needs to observe it:** `NT SERVICE\GuvFXBetaAgent` (the beta agent service; it already holds `Modify` on
  the slot directory and manages the runtime's lifecycle).

## Options

### Option A — Per-process access grant issued by the owning slot identity at launch (Nuno's preference)

A small launch wrapper, run **as `guvfx_b_slot1`** by the approved launch task, (1) starts `terminal64.exe`
and (2) immediately adds a single discretionary ACE to **that process object's** security descriptor granting
`NT SERVICE\GuvFXBetaAgent` `PROCESS_QUERY_LIMITED_INFORMATION` (plus `READ_CONTROL` so the observer can read
the object owner SID without the token — avoiding any need for the fuller `PROCESS_QUERY_INFORMATION`). The
owner has `WRITE_DAC` on its own process, so it can add the ACE without any privilege.

- **Which capability is granted:** `PROCESS_QUERY_LIMITED_INFORMATION` (+ `READ_CONTROL`) — query-only. NOT
  `PROCESS_VM_READ`, terminate, suspend, set-info, or token-impersonation. Enough for
  `QueryFullProcessImageNameW` (path), `GetProcessTimes` (start), and the object owner SID; session already
  comes from WMI/`ProcessIdToSessionId`.
- **Global or process-scoped:** **process-scoped** — a DACL ACE on one process object (one PID).
- **Survives process restart:** **No** — the ACE lives on the process object; it vanishes when the process
  exits. Each new launch (new PID) re-applies it. (Matches the requirement exactly.)
- **Weakens slot isolation:** **No** — the service gains *query-only* visibility of the slot's OWN runtime
  process, which it already manages; it cannot read memory, terminate, inject, or touch the slot identity's
  other resources. It is symmetric with the existing slot-directory `Modify` ACL.
- Security model: object-level DACL, standard Windows discretionary access control; least authority.
- Least privilege: **highest** — no Windows privilege, no SYSTEM, no broker; the narrowest possible grant on
  the narrowest possible object, for the shortest possible lifetime.
- Attack surface: minimal — one ACE on one short-lived object; no new listening component or IPC.
- Windows semantics: `WRITE_DAC` on an owned process → `SetSecurityInfo`/`SetKernelObjectSecurity`; the ACE is
  honoured by the object manager. Well-trodden, documented.
- Operational complexity: low-moderate — the launch task action changes from `terminal64.exe` directly to a
  thin wrapper (launch + grant), a launch-artefact change requiring operator re-APPLY (RULE 9 parse; the
  wrapper is a new install artefact).
- Auditability: high — the grant is a deterministic, reviewable ACE applied by a reviewed wrapper; the
  Verification Report can record it; observation itself is unchanged and already audited.
- Rollback: trivial — revert the launch task to the direct executable; no residual state (ACEs die with the
  process).
- Recovery: clean — a failed grant is fail-closed (observation stays `UNAVAILABLE`, never a false PRESENT);
  no orphan capability persists.
- Production risk: low — no production path, no privilege, no long-lived component; the change is confined to
  the beta slot launch.
- Implementation complexity: moderate — a small wrapper (ASCII-only PS or a tiny exe), a launch-task/install
  update, and the observe code reverts to `OpenProcess(LIMITED)` + object-owner SID (which now succeeds).

### Option B — Privileged observation broker

A separate, narrowly-scoped privileged helper (e.g. running as SYSTEM or holding `SeDebugPrivilege`) that the
low-privilege service calls to attribute a candidate; the service itself stays low-privilege.

- Which capability is granted: the BROKER holds broad process-query authority (SYSTEM or `SeDebugPrivilege`
  can open ANY process); the service gains an IPC channel to it.
- Global or process-scoped: the broker's authority is **global** (can inspect any process on the host).
- Survives restart: the broker is a **long-lived** privileged component.
- Weakens slot isolation: indirectly — a broadly-privileged component now exists whose compromise defeats all
  isolation; the service→broker channel is a new trust edge.
- Security model: privileged component + IPC trust boundary; larger.
- Least privilege: **worse** — concentrates broad authority in one always-present component.
- Attack surface: **highest** — a new listening/privileged service, an IPC protocol to authenticate and
  harden, and the standing broad capability.
- Windows semantics: standard but heavier (a service with a privilege, an authenticated IPC).
- Operational complexity: high — a new managed service, its own commissioning, monitoring, and rotation.
- Auditability: moderate — every attribution is now mediated; but the broker is a broad-authority actor to
  audit continuously.
- Rollback: harder — removing a deployed privileged service and its channel.
- Recovery: more failure modes (broker down, channel wedged) — though all fail-closed if designed so.
- Production risk: higher — a standing privileged component on the shared live host.
- Implementation complexity: **high** — new component + IPC + auth + review + hardening.

### Option C — Other minimally-privileged alternatives

- **C1 — grant the service `SeDebugPrivilege`.** Global, lets it open ANY process. Simplest code change but
  the **broadest** grant; flatly violates least-privilege and the isolation model. Rejected.
- **C2 — launch the slot process under a token whose DEFAULT DACL already includes the service.** A variant
  of A that bakes the grant into the process token's default DACL at creation rather than adding an ACE
  post-launch. Same end state (process-scoped, dies on exit, no privilege) but requires constructing/adjusting
  the launch token — more fragile than a post-launch `SetSecurityInfo` and harder to reason about; A is the
  cleaner expression of the same idea.
- **C3 — shared job object.** The service creates a job object granting itself query rights and the slot
  process is assigned to it at launch. Job-object process info is limited and the assignment/ownership model
  adds moving parts for no advantage over A. Not preferred.
- **C4 — redefine the PRESENT mandatory evidence** to fields the service CAN read unprivileged (session +
  image *name* + expected parent PID + creation window). Requires NO grant, but it **weakens** the identity
  binding (name is not path; parent/creation are circumstantial) and risks mis-attributing a same-named
  process. It would relax the isolation/identity invariant and needs an explicit governance decision. Not
  recommended; retained only as the no-grant fallback if any grant is refused.

## Comparison summary

| Dimension | A (per-PID grant at launch) | B (privileged broker) | C4 (redefine evidence) |
|---|---|---|---|
| Least privilege | ✅ narrowest, query-only, per-PID | ❌ broad standing authority | ✅ no grant, but weaker evidence |
| Attack surface | ✅ one short-lived ACE | ❌ new privileged service + IPC | ➖ none added, but weaker check |
| Scope | process-scoped (one PID) | global | n/a |
| Survives restart | no (re-applied per launch) | yes (standing component) | n/a |
| Weakens isolation | no | indirectly (trust edge) | **yes** (identity binding) |
| Auditability | ✅ deterministic ACE | ➖ mediated but broad actor | ➖ weaker proof |
| Rollback / recovery | ✅ trivial, fail-closed | ❌ heavier | ✅ trivial |
| Production risk | low | higher | low but correctness risk |
| Implementation | moderate | high | low (but relaxes an invariant) |

## Decision (proposed)

Adopt **Option A** — a per-process `PROCESS_QUERY_LIMITED_INFORMATION` (+ `READ_CONTROL`) grant, issued only
by the owning slot identity to `NT SERVICE\GuvFXBetaAgent` immediately after a successful launch, applying to
that PID only, vanishing when the process exits, requiring no Windows privilege, no SYSTEM component and no
broker, and preserving the existing slot isolation model. It is the narrowest capability on the narrowest
object for the shortest lifetime, and is symmetric with the slot-directory `Modify` ACL the service already
holds. Every mandatory PRESENT field (exact path via `QueryFullProcessImageNameW`, owner SID via the process
object owner, session via WMI/`ProcessIdToSessionId`, start time via `GetProcessTimes`) then becomes readable
**without** widening the service beyond query-only access to the runtimes it already manages, and the observe
layer's fail-closed semantics are unchanged (a missing grant → `UNAVAILABLE`, never a false `PRESENT`).

Option B is held in reserve for the case where a per-process launch grant proves infeasible on the host.
Option C4 is explicitly **not** adopted (it would relax the identity-binding invariant).

## Consequences / open implementation notes (for the authorised follow-up, not this ADR)

- The launch task action changes from the executable to a thin, reviewed, ASCII-only wrapper (RULE 9); it is a
  new install artefact and needs operator re-APPLY.
- The observe code reverts candidate attribution to `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` for path
  + `GetSecurityInfo`(owner) for the SID; the grant makes both succeed for the slot's own process while every
  other process stays out of reach (production `terminal64` remains excluded by owner + exact path, not
  session alone).
- Confirm on the host, under the service identity, that the granted handle yields the exact slot path and the
  `guvfx_b_slot1` owner SID for a controlled slot process, and that `ABSENT` still holds — before resuming the
  slot-1 `TOMBSTONE → RELEASE` proof.

## Revisit trigger

Approval of this ADR (to implement Option A), or host evidence that a per-process launch-time grant cannot be
applied by the slot identity (→ fall back to Option B with its own ADR).
