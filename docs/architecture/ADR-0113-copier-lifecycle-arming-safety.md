# ADR-0113 — Copier live-demo lifecycle-arming safety: atomic OPEN+MODIFY+FULL_CLOSE, emitter-key readiness, DARK teardown

- **Status:** Proposed (repository-only; DARK). PM owns lifecycle status in Notion.
- **Packet:** GFX-COPIER-LIFECYCLE-ARMING-SAFETY-CORRECTION-148
- **Date:** 2026-11-16
- **Supersedes:** nothing. Additive to ADR-0049 (bridge provisioning), ADR-0057/0083 (LIVE lifecycle), ADR-0094 (immediate dispatch), ADR-0097 (one-action arm), ADR-0104/0112 (emitter key / keyring).

## Context

The pkt-147 read-only pre-arm verification of the governed #46→#48 live copy-trading demo proved two safety gaps:

1. **Emitter key absent.** The standing immediate emitter (`GuvFX_CopierEmitter_46` → `run_emitter.py`) signs its
   sub-second `/event-ingest/` emissions with the ingest HMAC key named by the config (`COPIER_INGEST_HMAC_KEY`,
   key_id `copier-ingest`). A scheduled task inherits no shell env, and no standing key was provisioned (the
   one-shot attestation key is one-use-shredded), so the ≤1 s primary path could not operate.
2. **OPEN armable without an executable CLOSE.** `provision_authorized_live_copier_bridge` renders the bridge env
   (`MT5_ALLOW_LIVE`) when the OPEN flag stack is on, but the per-operation LIVE arms
   `COPIER_LIVE_MODIFY_ENABLED` / `COPIER_LIVE_FULL_CLOSE_ENABLED` (and the mutation dispatch+transport) are
   independent, default-OFF flags (ADR-0083). Granting the authorization's `modify_authority` /
   `full_close_authority` does **not** prove the execution flags are on. A copied OPEN could therefore be left
   with no executable CLOSE — a **stranded real position** on the live follower.

Execution flags in this architecture are process env (loaded from `.env`/container env at start; no runtime
mutation mechanism). A command run via `docker exec guvfx-backend` shares the web container's env, so it can
*verify* flag coherence authoritatively but cannot durably mutate flags.

## Decision

Introduce one fail-closed, single-source-of-truth contract plus an atomic arming operation and an idempotent DARK
teardown, all repository-only (DARK; no host/prod mutation in this packet).

### 1. `trade_copier.lifecycle_arm_contract` — the contract (pure, fail-closed)

- **Authority layer** (flag-independent, safe pre-arm): the authorization MUST carry `modify_authority` AND
  `full_close_authority` (so the bound position can always be closed) and MUST NOT carry partial-close; bounded
  caps (≤0.01 volume, exactly one order) and exact route/symbols. An entry-granting authorization without
  `full_close_authority` yields `OPEN_WITHOUT_CLOSE_REFUSED`.
- **Flag layer** (ARM time, flags ON): mirrors `live_execution` parity exactly — a per-op LIVE order is armable
  only when the per-op authority AND the per-op flag AND `copier_mutation_dispatch_enabled` AND
  `copier_mutation_transport_enabled` all agree. The central invariant: **OPEN executable while FULL_CLOSE is not
  ⇒ refuse.** A flag enabled without its authority is `LIFECYCLE_AUTHORITY_FLAG_MISMATCH`; partial-close flag on
  is refused.
- New blocker codes: `EMITTER_KEY_ABSENT`, `MODIFY_EXECUTION_NOT_ARMABLE`, `FULL_CLOSE_EXECUTION_NOT_ARMABLE`,
  `LIFECYCLE_AUTHORITY_FLAG_MISMATCH`, `FOLLOWER_FLAT_HUMAN_CONFIRM_REQUIRED`, `TEARDOWN_NOT_PROVEN`,
  `OPEN_WITHOUT_CLOSE_REFUSED`, plus authority/cap/route codes.

### 2. `arm_copier_live_lifecycle` — one atomic operation

Validate the COMPLETE contract first (authority + flag coherence + #46 clean at watermark + #48 flatness via the
human-confirm gate + host admission OFF + emitter task disabled + emitter-key readiness proven value-blind), then
**provision admission → enable emitter → create the arm**, each verified by read-back, with **full rollback to
DARK on any failure**. Host effects are injected (signed-host actuators / operator attestations); the backend
performs no host contact itself, and `--commit` fails closed where a host actuator is unavailable rather than
leaving a partial arm.

### 3. `teardown_copier_live_dark` — idempotent DARK teardown

Disarm → revoke → (signed) host-admission OFF (the final host execution gate) → disable emitter / shred key (host
actuators) → machine-readable DARK verdict. Works after any state (success / partial / abort); re-running changes
nothing. Host steps without an actuator are reported UNPROVEN, never "done". Backend env flags are reported as the
separate operator deploy-time revert (host admission OFF already blocks every live order).

### 4. Emitter-key readiness (secure, value-blind)

The standing key is a protected host-local file `C:\GuvFX\copier-emitter\46\ingest.key`, placed next to the
account config; `run_emitter.py` auto-loads it PROCESS-LOCAL at startup (never argv/log/persist) only when the key
env is unset. It is placed/verified/removed by the reviewed human-operated `Provision-GuvfxCopierEmitterKey.ps1`
(account-46-pinned; hidden input/STDIN only; atomic ACL'd write granting the tenant READ-ONLY — the emitter never
writes the key; value-blind `-Verify`; idempotent `-Remove`; refuses #48/Customer Zero/arbitrary; leaves the task
disabled; opens no window; enables no flag). Rotation is key_id-scoped with overlap; demo teardown removes it.

### 5. Preflight hardening

`copier_live_demo_preflight` gains a fail-closed `arm_readiness` section + `arm_ready` boolean using the same
contract (`require_flag_coherence=False` pre-arm). The legacy `blockers`/`verdict` are untouched, so
`copier_live_arm`'s control-plane preflight is unaffected.

## Consequences

- OPEN can never be armed for this demo route unless the authorized MODIFY and FULL_CLOSE lifecycle is also safely
  executable; the follower can never be stranded; the ≤1 s path cannot be enabled without proven, value-blind key
  readiness; any failure returns the whole stack to DARK.
- Repository-only: no flag/key/admission/task mutation here. A real arm/teardown is a separate, Sponsor-gated step
  that wires the signed-host actuators; this packet deploys nothing.
