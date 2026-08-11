# 0040 — Hosted Executor privilege model: LocalSystem behind the signed protocol

- Date: 2026-08-11
- Status: Accepted

## Context

The signed hosted-executor daemon ([[0039-hosted-executor-daemon]], `deploy/hosted-executor/`) exists to make a
customer's Windows hosted MT5 slot EXIST on its node: it runs `prepare_hosted_slot`'s reviewed provisioning
primitives — create the per-account local Windows user, apply the per-account NTFS ACL, copy the golden runtime,
grant RDP, enforce single-session, publish/verify the per-account RemoteApp, prepare the observer.

The daemon was originally installed as the least-privilege virtual account `NT SERVICE\GuvFXHostedExecutor`
(installer `deploy/hosted-executor/install_service.ps1` enforced exactly that identity, mirroring the beta agent
[[0013-beta-agent-service-host-winsw]]).

The Stream 7D live disposable-slot certification (2026-08-11, disposable account 15) proved the whole signed
pipeline end-to-end — request signed → host-verified → X25519 envelope opened (Windows password decrypted
host-side) → reviewed `.ps1` executed → result signed back — but the very first provisioning primitive
(`Provision-GuvfxAccount.ps1` → `New-LocalUser`) failed with **`Access denied`**: the least-privilege service
account cannot create local users, set ACLs, or configure RDS. Those operations inherently require
administrative / SYSTEM capability. The reviewed least-privilege identity therefore **cannot** perform
provisioning at all — an internal contradiction the cert surfaced (both this and a separate `-Description`
length defect were latent because the tests stub `New-LocalUser`; see the review-fakes lesson).

## Verified facts

- Installer (`deploy/hosted-executor/install_service.ps1`) assigned identity post-install via `sc config obj=`
  and, for a virtual/user account, granted `SeServiceLogonRight` via LSA; it verified `SERVICE_START_NAME` and
  rolled back on mismatch.
- Live host `GuvFXHostedExecutor` ran as `NT SERVICE\GuvFXHostedExecutor` (confirmed `sc qc`), **not** a member
  of Administrators (confirmed `net localgroup Administrators`).
- `New-LocalUser` under that identity returns `Access denied` (Stream 7D `diag`, disposable account 15).
- The daemon's confinement is enforced regardless of OS token: HMAC-signed requests, single-use nonce, bounded
  time skew, an allow-list of exactly nine reviewed `.ps1` primitives resolved server-side, ParseFile startup
  gate, fixed-argument-vector subprocess (never a shell string), server-derived slot identity/paths from
  `account_id`, and Customer-Zero / reserved-id refusal at two layers (`host_agent_dispatch` + `host_executor`).
  This model was adversarially reviewed to 0 surviving HIGH/MEDIUM ([[0039-hosted-executor-daemon]],
  `HOSTED_EXECUTOR_SECURITY_REVIEW.md`).
- LocalSystem is a Windows built-in that already holds service-logon; it needs no `SeServiceLogonRight` grant.

## Assumptions

- No reviewed primitive, run as SYSTEM, escalates beyond its intended per-account scope: every primitive's paths
  and identity are server-derived from `account_id` (`C:\GuvFX\accounts\<id>`, `guvfx_u_<id>`), Customer Zero is
  refused, and no request field can express an arbitrary path, command, username, or script. (Re-checked in the
  Stream 7D LocalSystem adversarial review.)

## Decision drivers

- **Capability**: provisioning genuinely requires admin/SYSTEM; a least-privilege token cannot do the work.
- **Security boundary placement**: the daemon was purpose-built so the *protocol + allow-list* is the boundary,
  not the OS token. Elevating the token does not widen what a valid-signature caller can cause (still the nine
  reviewed primitives on derived, Customer-Zero-refused slots).
- **Reversibility**: identity is a single `sc config obj=` change, trivially reverted.
- **Governance**: this reverses an installer-enforced least-privilege posture on the production Customer-Zero
  host — an Amber/Red decision requiring explicit Sponsor approval + this ADR (both obtained 2026-08-11).

## Options considered

1. **LocalSystem (chosen).** Daemon runs as the LocalSystem built-in; the signed protocol + allow-list remain the
   security boundary. Simplest; standard identity for a Windows provisioning/management service; no new work.
2. **Delegate privileged primitives to a SYSTEM-context mechanism** (per-primitive scheduled tasks as SYSTEM,
   triggered by a least-privilege daemon). Keeps the daemon least-privilege but adds a whole delegation layer,
   more moving parts, and a second trust surface — disproportionate given the protocol is already the boundary.
3. **Grant the virtual account admin rights.** Creating local users needs Administrators-equivalent membership;
   this is functionally identical to (1) but less honest about what the identity is.

## Decision

Run the hosted-executor daemon as **LocalSystem**. The security boundary is the signed protocol + the reviewed
primitive allow-list + Customer-Zero refusal — not the OS token. The installer now assigns LocalSystem by default
(`-RunAsUser` still accepts `NT SERVICE\GuvFXHostedExecutor` for a least-privilege deployment on a host that does
not need in-process provisioning); the `SeServiceLogonRight` grant/verify is skipped for LocalSystem because the
built-in already holds it. No other installer behaviour changes (WinSW hash-pin, XML contract, ParseFile gate,
install-only verify, rollback all preserved). This ADR is Sponsor-approved.

## Consequences

- The daemon can now execute the reviewed provisioning primitives (create user, ACL, RDP, RemoteApp).
- A network-listening service runs as SYSTEM: its exposure is bounded solely by the signed protocol + allow-list,
  which must remain the single, adversarially-maintained boundary. Any new primitive or any relaxation of the
  request schema is a security-relevant change requiring re-review under this ADR.
- Reversible: `sc config GuvFXHostedExecutor obj= "NT SERVICE\GuvFXHostedExecutor"` + regrant `SeServiceLogonRight`.

## References

- [[0039-hosted-executor-daemon]], [[0037-signed-host-executor]], [[0013-beta-agent-service-host-winsw]]
- `deploy/hosted-executor/install_service.ps1`, `deploy/hosted-executor/winsw/*.xml`
- `docs/operations/hosted-workspace/HOSTED_EXECUTOR_SECURITY_REVIEW.md`
