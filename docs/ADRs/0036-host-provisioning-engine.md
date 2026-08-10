# ADR-0036 — Host Provisioning Engine (`prepare_hosted_slot`) + G5 per-user NTFS ACL

- **Status:** Proposed (Amber — awaiting Sponsor ratification before the flag is ever armed)
- **Date:** 2026-08-10
- **Programme:** Beta Readiness Stream 4 (Host Provisioning Engine, Phase 1)
- **Supersedes/relates:** ADR-0033 (Hosted Persistent MT5 Workspace), ADR-0034 (Onboarding), Beta Readiness
  Report; closes the ADR-0033 cross-tenant ACL hard blocker at the engineering level.

## Context

Repository orchestration for the hosted customer journey is complete and DARK (request → node allocation
→ observation scheduler → customer journey UI). The remaining engineering blocker to an unassisted beta
signup is **automated Windows provisioning**. A read-only repository audit (Stream 4 WS-A) established:

1. **No automated host-reach exists for the per-user (TX-1) path.** Every `terminal_provisioning/windows/*.ps1`
   (identity, folders, ACL, populate, RDP, single-session, AppLocker) is run **manually over SSH/RDP**. The one
   signed transport (the beta agent / `mgmt_client`) belongs to the *different* `AccountRuntime` fixed slot-pool
   model and, by the Windows-primitive boundary, cannot mint an identity or write an ACL.
2. **The G5 NTFS ACL is missing.** `C:\GuvFX\accounts\<id>` is created with folders but **no explicit ACL**, so
   it inherits `BUILTIN\Users` read — a second hosted identity could read another customer's `accounts.dat`.
   This is the ADR-0033 cross-tenant hard blocker. The full break-inheritance + 3-principal + SID-typed
   read-back pattern exists only one-off for fixed slots in `install_pool.ps1`.
3. **State runs ahead of reality.** `allocate_workspace_node` advances `PROVISIONING → WAITING_FOR_LOGIN` with
   no host slot behind it; a customer could be told "log in" with nothing to log into.

## Decision

Introduce **`hosted_workspace.slot_preparation.prepare_hosted_slot(workspace)`** — a single idempotent,
fail-closed **Django-plane orchestrator** that makes a customer's Windows hosted slot exist (identity → folders
→ **G5 NTFS ACL** → mark PROVISIONED → golden runtime → RDP → single-session → RemoteApp verify → AppLocker
AuditOnly prep → observer [deferred]) and **gates** the `PROVISIONING → WAITING_FOR_LOGIN` transition. It
composes the existing certified DB ops (`terminal_provisioning.services`) and the existing host scripts.

The **G5 NTFS ACL engine** ships as two parts: a pure, fully-tested brain
(`hosted_workspace.workspace_acl`: `build_workspace_acl_plan` + a SID-typed `verify_workspace_acl` with a
RULE-11 positive/negative self-control), and an ASCII-only host script
(`terminal_provisioning/windows/Set-GuvfxWorkspaceAcl.ps1`: snapshot → break inheritance → grant exactly
SYSTEM + Administrators + `guvfx_u_<id>` → SID-typed read-back → rollback on mismatch).

### Boundary (architecture.md, 2026-07-22 — binding)

`prepare_hosted_slot` lives **above** the Windows-primitive boundary. It legitimately knows workspace identity
/ ownership / node binding (like `delivery.py`), but delegates every host mutation to a **signed
`HostExecutor`** that receives only a *fixed slot identity* (`guvfx_u_<id>`), a *fixed `runtime_root`*, and the
node's `rdp_host` — **never** a workspace UUID, generation, or `ProvisioningJob`. The beta signed channel is
deliberately **not** reused for identity/ACL (it cannot, by design). The G5 ACL engine is a fixed-slot
host-provisioning action, **not** an agent-callable primitive.

### Darkness (two-level, plus a dark executor seam)

- Dormant unless **`HOSTED_PERSISTENT_MT5_ENABLED`** (master) **AND** the new **`HOSTED_SLOT_PREP_ENABLED`**
  are on. While off, `allocate_workspace_node` advances exactly as before (**zero behaviour change / no
  Customer-Zero regression**).
- Even armed, `resolve_host_executor()` returns **`None`** in the repository-only phase (the `_dark_observe_fn`
  pattern), so every host step **fails closed** (`host_executor_unavailable`), state is **not** advanced, and
  **no host is contacted from the repository**. A later host-certification increment supplies a real signed
  executor without touching this control flow.
- **Customer Zero (account #1 / `guvfx_u_1`) and the PRODUCTION/admin identity are refused up front.**
- The engine **never arms execution** and **never performs a broker login** (excluded entirely). AppLocker
  preparation stops at **AuditOnly**; `-Enforce` (execution enablement) is a separate Sponsor-gated op.

## Why this is Amber (requires Sponsor ratification)

Per-user, on-demand identity + ACL creation is a **new host-provisioning-layer pattern**. The primitive-boundary
invariant otherwise assumes "every OS object is created once by a human at the install gate" (the beta pool
answers this by fixed pre-provisioning). This ADR sanctions a distinct, above-the-primitive per-user provisioning
layer. It ships DARK and unarmed; **ratification + the signed host-executor + on-host certification are required
before `HOSTED_SLOT_PREP_ENABLED` is ever set.**

## Consequences

- Closes the ADR-0033 cross-tenant ACL blocker at the engineering level; the slot no longer gets "ahead of" the
  host.
- Adds one DARK flag and one Django-plane module + one host script; **no model, no migration, no host contact,
  no execution change.**
- The signed host-executor transport, the RemoteApp publisher, the observer runtime + host observe bridge, the
  AppLocker multi-tenant (`-Merge`) accumulation, the golden clean-image commissioning, and on-host
  certification remain **outstanding, Sponsor-gated** (see `docs/operations/hosted-workspace/HOST_PROVISIONING_ENGINE.md`).
