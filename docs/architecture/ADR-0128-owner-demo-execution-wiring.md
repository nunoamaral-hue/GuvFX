# ADR-0128 — Owner-demo execution wiring (fail-closed actuator provider; verifier-window opener deferred)

- **Status:** Accepted (repository-only; DARK; default-OFF). Not deployed, not armed, no host rollout.
- **Packet:** GFX-COPIER-OWNER-DEMO-EXECUTION-WIRING-171
- **Date:** 2026-11-25
- **Builds on:** ADR-0127 (owner-operable demo control), ADR-0117 (production arming pipeline /
  `live_host_actuators`), ADR-0101 + GFX-164 (bounded verifier window / key lifecycle), ADR-0112 (file-first
  hosted-executor keyring), ADR-0049 (per-tenant admission).

## Context

ADR-0127 shipped the owner-operable control plane but deliberately left `confirm`/`stop` passing `actuators=None`,
so execution could never proceed: the orchestration had no production wiring, and §4 recorded the remaining
one-time provisioning decision. This ADR adds the **smallest** wiring that lets the authenticated route owner
(`support@guvfx.com`) drive the bounded #46→#48 demonstration through the UI — **without pasting shell commands or
signing secrets** — while keeping production comprehensively DARK and fail-closed until a separate, Sponsor-gated
host rollout.

## Decision

1. **A narrow provider** (`trade_copier/owner_demo_actuators.build_demo_actuators`) builds the control's
   `DemoActuators` by reusing the existing `live_host_actuators` factories. `confirm`/`stop` consult it only when
   the caller injects nothing (tests inject fakes). No new authority, no new model, no migration.
2. **A new default-OFF execution flag** `copier_owner_demo_execution_enabled()` ANDs the master gate and the
   owner-demo control gate. It gates ONLY whether the risk-INCREASING arm actuators (`admission_provisioner`,
   `emitter_enabler`) are wired. The risk-REDUCING teardown actuators are **flag-independent** so a `stop` still
   disarms the host after the flag toggles off mid-session.
3. **The host-side verifier-window opener stays UNPROVISIONED.** `DemoActuators` gains a `verifier_window_opener`
   that `provisioned()` now **requires**; the production provider leaves it `None`. This PR adds **no standing-key
   transfer channel**, so `provisioned()` is False in every current environment ⇒ `confirm` fails closed
   (`EXECUTION_ACTUATORS_UNPROVISIONED`) even with the execution flag on. At confirm the opener (when a future
   rollout provisions it) opens the INGEST window **host-side** before the atomic arm's `covers_live_authorization`
   gate; the window is torn down (`close_all`) on **any** non-armed outcome and left open only on an armed success.
4. **Prepare stays host-free** (authorization + acceptance lease + confirm token only); **confirm is the sole
   authorize-to-execute point** (fresh re-auth + single-use token + provisioned actuators). Caps/expiry stay
   server-side (`authority_contract_blockers`, the 15-min authorization, `covers_live_authorization` expiry ≥
   authz). Teardown is idempotent and **retains the exclusion lease on failed OR unverified host teardown after an
   arm** (`host_dark` required once an arm engaged the host).

## Addendum — interim symmetric window opener vs host-private / backend-public verification

The verifier window authenticates the host→backend INGEST feed. Two designs for delivering that trust:

| | **Interim — host-held SYMMETRIC key** (what the window uses today) | **Preferred — ASYMMETRIC (host-private / backend-public)** |
|---|---|---|
| **Where the key bytes exist** | ONE shared HMAC secret exists as bytes in **two** places at arm time: the host-local standing emitter key (`...\46\ingest.key`, 0600) and the backend verifier window's per-window-unique tmpfs file (`/dev/shm/…`, 0600, shredded on close/reap). Identical bytes, delivered host↔backend. | The **private signing key exists only host-side** (`...\46\`), never leaves the host. The backend holds only the **public verification key** (pinned in config / the keyring). No secret bytes are ever equal across the trust boundary. |
| **Who could access them** | Anyone with the host-local file ACL (SYSTEM/Admins/`guvfx_u_46`) OR anyone who can read the backend container's `/dev/shm` while a window is open. A transfer channel that moves the symmetric key host→backend is a third exposure surface — **the piece this PR refuses to add.** | Host file ACL holders see the private key (host-side only); the backend (and anyone reading it) sees only the public key, which is not a secret. No transfer channel for secret material is ever required. |
| **Expiry** | The window is time-bounded (`ttl_seconds`, must cover the authz) and single-window (`max_uses is None` but one OPEN window); the key file is shredded on close / reap / teardown. The authorization self-expires at 15 min; the acceptance lease self-expires via TTL. | Same bounded window + authorization + lease expiry, PLUS the public key can carry its own validity/rotation epoch; signatures outside the epoch verify-fail without any host contact. |
| **Revocation** | Revoke by: closing the window (shreds the backend key), disabling/removing the host key (`emitter_key_remover` → `Set-GuvfxCopierEmitter.ps1 -Mode RemoveKey`), revoking the authorization, releasing the lease. Rotating the shared secret requires re-provisioning BOTH sides. | Revoke by un-pinning / rotating the backend PUBLIC key (one backend-side change) — the host private key need not move. Window/authorization/lease revocation unchanged. Strictly smaller blast radius. |
| **Blast radius if the backend is compromised** | The attacker can read an OPEN window's symmetric key and forge host→backend events for that window's lifetime. | The attacker reads only the public key and can forge **nothing** (no private key backend-side). |

**Recommendation:** adopt the asymmetric design as the durable end state (host-private signing + backend-pinned
public verification). It removes the symmetric-key transfer surface entirely, shrinks revocation to a backend-side
public-key change, and makes a backend compromise non-forging. It is deferred because it touches the host emitter,
the backend verifier, the verifier window and the hosted-executor keyring — larger and host-coupled, a separate
Sponsor-gated packet. Until then the interim host-held-symmetric path stands, with **no per-demo browser secret**
and the transfer channel **not built here**.

## Production integration still required (NOT done in this packet)

1. **The verifier-window opener** — a signed host op (added to the emitter bundle) that opens the INGEST window
   host-side by streaming the host-local standing key into `copier_verifier_window --open` over the authenticated
   channel (interim), OR the asymmetric variant above. Requires a host rollout (RULE-9 ParseFile-validated).
2. **A value-blind #48 admission read-back** for `host_admission_probe` (currently `None`).
3. **Flag + host enablement:** `COPIER_OWNER_DEMO_EXECUTION_ENABLED` + `HOSTED_HOST_EXECUTOR_ENABLED` +
   `COPIER_EMITTER_STANDING_ENABLED`, plus host provisioning (standing emitter key present, keyring currency),
   each separately Sponsor-gated.
4. **The live demonstration** remains a separate, explicit, head/runtime-pinned **Sponsor real-money GO**.

## Consequences

- DARK by default; migration-free; reuses `CopierLiveAuthorization`/`CopierLiveArm` + the signed host adapters.
- `provisioned()` is False in production (opener + admission read-back unprovisioned) ⇒ `confirm` stays fail-closed
  with `EXECUTION_ACTUATORS_UNPROVISIONED`, exactly as before this PR — the wiring exists but cannot arm.
- Follow-up (separate packet): provision the verifier-window opener (asymmetric preferred) + the admission probe.
