# ADR-0037 — Signed Host Executor + disposable host certification

- **Status:** Proposed (Amber — ratify + on-host-certify before `HOSTED_HOST_EXECUTOR_ENABLED` is armed)
- **Date:** 2026-08-11
- **Programme:** Beta Readiness Stream 5 (Signed Host Executor + Disposable Host Certification)
- **Builds on:** ADR-0036 (Host Provisioning Engine), reuses `terminal_provisioning.mgmt_protocol`
  (signing construction) + `broker_cred_envelope` (ADR-0027 sealed box) + `core.credentials` (RULE 3).

## Context

ADR-0036 shipped `prepare_hosted_slot` with a **DARK `None` host-executor seam** — it cannot yet perform real
Windows operations. Stream 5 supplies the missing transport, under an explicit ADR ruling that the host
executor **MUST NOT** become an arbitrary shell, PowerShell runner, free-form command/file API, or general
Windows admin agent. The Windows-primitive boundary stays frozen; only allow-listed operations may be invoked.

## Decision

Introduce a **narrow, signed backend↔host provisioning contract** and its two endpoints:

- **`host_protocol.py`** — the wire contract. Mirrors the reviewed `mgmt_protocol` construction (HMAC-SHA256
  canonical body, single-use nonce, bounded skew, short expiry, rotatable `key_id`, constant-time compare) but
  carries a **hosted slot identity `account_id`** — never a runtime UUID / generation / ProvisioningJob. A
  request expresses ONLY: `operation` (fixed allow-list), `account_id` (int), typed scalar `params` (bound via
  `params_digest`), and optionally a sealed credential `payload` (bound via `payload_digest`). Responses are
  signed so a MITM cannot forge the security-relevant read-back (e.g. the G5 ACL rows).
- **`host_agent_dispatch.py`** — the host-side dispatcher. `verify → refuse Customer Zero → DERIVE identity+paths
  from account_id (no caller path) → reject non-allow-listed params → map operation to EXACTLY ONE reviewed
  primitive with server-derived args → run → sign`. There is no wire path to a command, script, path, or username.
- **`host_executor.py`** — the Django `SignedHostExecutor` implementing the `HostExecutor` protocol. Seals the
  Windows password with the ADR-0027 envelope (its **own** key registry — RULE 3/6, not conflated with broker
  creds), enforces identity↔path confinement + Customer-Zero refusal as the **first** layer, verifies the signed
  response, and fails closed on host-unavailable/timeout/malformed/forged responses.

Allow-listed operations (each maps to exactly one reviewed primitive): `PROVISION_IDENTITY`,
`APPLY_WORKSPACE_ACL`, `ROLLBACK_WORKSPACE_ACL`, `MATERIALISE_RUNTIME`, `APPLY_AUTOTRADING_CONFIG`,
`ENSURE_RDP_MEMBERSHIP`, `ENSURE_SINGLE_SESSION`, `ENSURE_REMOTEAPP`, `PREPARE_OBSERVER`,
`APPLY_APPLOCKER_AUDIT`, `VERIFY_SLOT`.

New reviewed host primitives (ASCII, RULE 9; host artefacts, executed only under host cert): `Set-GuvfxRemoteApp.ps1`
(publish/verify exactly-one `terminal64 /portable`, FilterByName — no desktop), `Set-GuvfxObserver.ps1`
(session-bound attach→read→exit observer task), `Set-GuvfxAutoTradingConfig.ps1` (write the certified
`[Experts] AllowLiveTrading=1 Enabled=1` — CAPABILITY only).

### Darkness

`resolve_signed_host_executor` returns `None` unless `HOSTED_HOST_EXECUTOR_ENABLED` is on **AND** the keyring +
base_url + envelope key are configured — so the repository-only phase contacts no host and `prepare_hosted_slot`
fails closed. Customer Zero is refused at both layers. AutoTrading config is capability-only (execution stays
gated independently, DARK); AppLocker prep is AuditOnly (never Enforce).

## Why this is Amber

The executor grants the backend a real (if narrow) host-mutation capability. It ships DARK and unarmed;
**ratification + on-host certification on a disposable non-Customer-Zero slot are required before the flag is set.**
The live certification runs the real `prepare_hosted_slot` against a disposable slot on the host, captures the
Customer-Zero before/after fingerprint, and never failure-injects the shared prod agent — it is a Nuno-gated
host step, not part of this repository PR. See `docs/operations/hosted-workspace/SIGNED_HOST_EXECUTOR_RUNBOOK.md`.

## Consequences

- Closes the "no automated host-reach for the per-user path" gap from the ADR-0036 audit, without a shell.
- Adds one DARK flag + three Django modules + three host scripts; **no model, no migration, no host contact,
  no execution change** in the repository.
- Remaining Sponsor/host-gated: deploy the dispatcher as a supported host service (RULE 1), provision the
  keyring/envelope keys, run the disposable host certification (Phases 10–16), then arm.
