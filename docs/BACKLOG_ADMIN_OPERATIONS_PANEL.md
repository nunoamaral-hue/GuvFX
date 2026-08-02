# Backlog — Admin Operations Panel (future)

Status: **BACKLOG (not scheduled).** Recorded 2026-07-28 during the Pre-Beta Identity Baseline
cleanup. Do **not** implement in the current packet.

## Context
`a@a.com` is the dedicated **Platform Administrator** identity (superuser, staff, no trading
account, no broker credentials, no strategy assignment, no runtime). It must **never** be used as a
customer trading account. It is intended to back a future administrative frontend — the
Admin Operations Panel described below — not ad-hoc shell/SSH actions.

An `admin_ops` Django app already exists (entitlement overrides); this panel would build on it.

## Scope (future panel)
A read-first operations console for platform administration:

- **User lookup** — find a user, see role/admission/subscription and linked accounts.
- **Account status** — per-account validation state, broker/runtime binding, health.
- **Stuck-account recovery** — surface accounts wedged in onboarding/validation and re-drive them.
- **Provisioning recovery** — inspect/re-drive stuck `ProvisioningJob`s and quarantined runtimes.
- **Runtime and slot health** — per-`(slot, generation)` occupancy, beta pool state, golden pin.
- **VPS capacity** — host capacity vs. beta caps (`BETA_MAX_TESTERS`, pool size), headroom.
- **Agent and bridge health** — Windows agent (:8788) + signed management channel reachability.
- **Execution suspension** — per-account kill / global kill-switch controls (with confirmation).
- **Residual-exposure alerts** — open positions / pending order-jobs that should not exist.
- **Audit history** — searchable `AuditEvent` view (credential lifecycle, provisioning, exec).
- **Credential-safe offboarding** — the sanctioned sequence proven in this packet
  (disable → deprovision → clear session/provisioning → P3-D credential destruction →
  host-artefact removal → supported user delete → SET_NULL audit preservation).

## Non-functional requirements (mandatory before build)
- **Every administrative action MUST be audited** (append-only `AuditEvent`, actor recorded).
- **2FA required** for administrative sign-in and for any state-changing action.
- **Least privilege** — the panel operates through supported services/lifecycle paths, never raw
  SQL or ad-hoc host mutation.
- Destructive/irreversible actions require explicit confirmation and a captured reason.

## Reference
Sanctioned offboarding lifecycle demonstrated 2026-07-28 (TX1 test-fixture removal): see the
`terminal_provisioning` services (`disable`/`retire`), `SessionAssignment`/`AccountProvisioning`
teardown, `trading.credential_lifecycle.destroy_customer_credential` (P3-D), and the Windows
host cleanup guards.
