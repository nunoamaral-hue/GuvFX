# ADR-0126 — Owner-operable governed #46→#48 demonstration control

- **Status:** Accepted (repository-only; DARK; default-OFF). Not deployed, not armed.
- **Packet:** GFX-COPIER-TENANT-OWNER-DEMO-CONTROL-170
- **Date:** 2026-11-24
- **Supersedes:** nothing. Builds additively on ADR-0057 (live authorization), ADR-0097 (atomic arm),
  ADR-0101 (bounded verifier window), ADR-0112 (file-first hosted-executor keyring), ADR-0114 (lifecycle
  arming/teardown), ADR-0117 (production arming pipeline), GFX-164 (verifier-window key lifecycle).

## Context

The real #46→#48 demonstration ceremony was **staff-and-shell only**: the `CopierLiveAuthorization` needed a
staff `--approved-by`; the verifier window was opened by pasting the ingest key at a hidden prompt on the host;
and arm/teardown were management commands. The tenant **owner** (currently `support@guvfx.com`, owner of
accounts #46 and #48) must be able to drive a bounded demonstration themselves — **without staff access and
without pasting shell commands or secrets** — while every existing safety invariant is preserved.

## Decision

Add a thin, fail-closed **owner-operable control plane** (`trade_copier/owner_demo_control.py` +
`trade_copier/owner_demo_api.py`, mounted at `/api/copy-trading/owner-demo/`) with four stages: **check
readiness** (GET, read-only), **prepare** (POST), **confirm** (POST, fresh re-auth), **stop** (POST). It
orchestrates primitives that are unchanged, and it adds no authority and ORs past nothing.

### 1. Authorization is OWNERSHIP, not an email string

`resolve_governed_demo_route(user)` permits **only the verified owner of BOTH governed accounts** with the
enabled, **sole** `CopierFollower` route and a **verified** source→follower symbol map. Staff is *not*
privileged; other tenants, members, anonymous callers and any route ambiguity are denied with a stable,
value-blind reason code (no existence leak). The governed route is config-pinned
(`COPIER_DEMO_MASTER_ACCOUNT_ID`/`…FOLLOWER…`, default 46/48); the follower broker symbol comes from the
canonical verified map, never hard-coded.

### 2. Owner self-approval for this ONE governed route

For the governed route only, the **re-authenticated owner's deliberate confirmation is the approval** — the
`CopierLiveAuthorization` is created with `approved_by = the owner` instead of a staff user. This is bounded by:
ownership; caps enforced **server-side** (`max_volume = 0.01`, `max_orders = 1`, `max_concurrent = 1`,
`modify`+`full_close` authority, **partial-close excluded**, `valid_minutes = 15`); the governed operational
**acceptance lease** (`recovery_lease` KIND_RECOVERY) for mutual exclusion with deploy/recovery and a second
operator; and the fact that **execution still requires the atomic arm's host actuators + a separate Sponsor
real-money GO** (see §4). The owner cannot widen caps, change the route, or reach another tenant.

### 3. Fresh re-authentication for the consequential confirm

There was no step-up mechanism in the repo. Confirm requires the owner to **re-submit their current password**,
verified server-side with `user.check_password` (the change-password primitive). The password is never stored,
logged, echoed or placed in the audit — only the boolean verdict crosses into the orchestrator. Confirm also
requires a **signed, single-use confirm token** (`TimestampSigner`, bound to the prepared authorization + owner,
TTL = the prepare window) so confirm cannot happen without prepare and cannot be replayed (the one-engaged-arm
DB constraint + the token TTL foreclose duplicates/replay; the access-token `iat` is *not* trusted because the
frontend silently refreshes it).

### 4. Secrets: no browser secret; interim host-held key; preferred asymmetric evolution (deferred)

**The browser never receives or submits a signing secret, and no symmetric key is stored in the DB, session,
frontend, logs, Notion or audit.** The control orchestrates the EXISTING atomic arm
(`lifecycle_arming.arm_live_lifecycle`), whose host actuators/probes are **injected**. In production they are
**unprovisioned**, so confirm fails closed (DARK): the dry-run cannot prove the host gates / verifier window, and
even a provisioned-probes-only state is refused with `EXECUTION_ACTUATORS_UNPROVISIONED`. The signing material
is the **standing host ingest key** + the **file-first hosted-executor keyring** (`hx-20261115`), already
provisioned host-side (pkt-163 / pkt-166C) and delivered **host-side** — never through the browser.

**Remaining one-time provisioning decision (NOT performed here):** the preferred durable design is a **host-only
PRIVATE signing key with a backend-pinned PUBLIC verification key**, so no bounded demonstration ever re-enters a
shared secret. A full asymmetric migration touches the host emitter, the backend verifier, the verifier window
and the hosted-executor keyring — too large and host-coupled for this repository-only packet. This ADR records it
as the recommended next step; until then the interim host-held-key path (no per-demo browser secret) stands, and
the owner control is built behind the injected actuator abstraction so wiring the real (asymmetric or host-held)
actuators is a separate, Sponsor-gated step.

### 5. Mandatory arm-time safety gates (unchanged, re-proven at confirm)

The owner's "trade closed" statement is **historical attestation**. At the confirm instant the atomic arm
re-proves, fail-closed: #46 clean; #48 flat / in-flight-job-free; exact identity/route/symbol; an OPEN INGEST
verifier window whose key file is present and outlives the authorization (GFX-164 makes this reap-shred-proof);
flag coherence; caps/expiry server-side; kill switch clear; sole authorization + no conflicting arm — with **full
rollback to DARK on any failure** (no partial arm, no stranded position).

### 6. Guaranteed teardown

**Stop** calls the idempotent `teardown_copier_live_dark` (disarm + revoke + signed host OFF via the injected
actuators) and releases the acceptance lease **iff this owner holds it** (never another holder's). Safe to repeat;
touches no unrelated service or account. Host steps are reported **UNPROVEN** (never silently "done") when the
signed actuators are absent. A value-blind audit trail (`logging`: actor/tenant/route/correlation/timestamps/
state transitions only) records every stage.

## Consequences

- DARK by default: `copier_owner_demo_control_enabled()` (ANDs the master gate) is OFF, so every endpoint 503s and
  nothing is created. No migration (reuses `CopierLiveAuthorization`/`CopierLiveArm` + logging audit).
- The real #46→#48 execution still requires a **separate, explicit, head/runtime-pinned Sponsor real-money GO**
  after this control is merged, deployed DARK, and reverified — naming the route, action/symbol, 0.01 max, one-order
  cap, observation window and teardown.
- Follow-up (separate packet): the asymmetric host-private / backend-public signing-key migration (§4).

## Alternatives rejected

- **Email-allowlist as the authorization** — rejected: ownership of the governed route is the authority; an email
  string is neither necessary nor sufficient and could lock out the legitimate owner or admit a cohort member who
  does not own the route.
- **Browser-delivered signing key / symmetric key at rest on the backend** — rejected: violates ADR-0101 and the
  no-browser-secret rule; the window/keyring design already keeps the key host-side and ephemeral.
- **A new lease kind / new models** — rejected as unnecessary: the KIND_RECOVERY acceptance lease and the existing
  authorization/arm rows already provide mutual exclusion, caps and audit, keeping the change migration-free.
