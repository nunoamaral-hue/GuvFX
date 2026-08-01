# 0024 — Governed orphaned-slot reclaim + failed-runtime recovery for beta provisioning

- Date: 2026-08-01
- Status: Accepted
- Decision class: Amber (adds operator tooling on the provisioning/recovery path — `.claude/rules/architecture.md`)
- Related: [ADR-0023](0023-materialise-timeout-and-ambiguous-reconcile.md) (deployed PR#252 reconcile),
  [ADR-0014](0014-management-protocol-release-operation.md) (agent RELEASE op), [ADR-0021](0021-permanent-dedicated-runtime-onboarding.md).

## Context

The first Customer Zero provisioning failed at MATERIALISE (ADR-0023, remediated + deployed). It left a
**data-plane / control-plane divergence**: the agent slot store holds runtime `66972e0e-…` at **slot 2 /
generation 4**, fully materialised (`stage_copy COMPLETED` only — never started), while the backend
`AccountRuntime pk1` and `ProvisioningJob #1` are `FAILED`. Reclaiming the slot and re-preparing a retry has
**no governed path**:

- the backend `AgentWindowsProvisioner` has `stop()`=STOP and `teardown()`=TOMBSTONE but **no RELEASE client**,
  so the slot can be tombstoned but never returned to Available (generation never advances) — a pre-existing
  pool-leak;
- `_drive_deprovision` does TOMBSTONE→REMOVED without RELEASE; `_drive_stop` is `HELD_STATES`-gated and a
  `FAILED` runtime is not `HELD`;
- `reconcile_beta_provisioning` only re-drives `NOT_PROVISIONED`, not `FAILED`/orphans.

Verified agent contract (against `deploy/beta-agent`): for a materialised-but-never-started occupancy, STOP
confirms **ABSENT** (empty birth → `confirm_terminated` by absence); TOMBSTONE's `precheck_cleanup` observes
**live ABSENT** (it does not require a recorded `confirm_terminated`); **RELEASE must use the protocol op
`op_release`** — which sets `process_identity_verified=True` for a *proven-empty* slot — **not** the legacy
`release()` helper (which requires `confirm_launch==COMPLETED` and would permanently BLOCK a never-started
runtime). The agent keys idempotency on `(job_id, operation)`; a re-send of STOP under a **new** job_id *after*
TOMBSTONE removed the owner marker hits the integrity gate and **quarantines the slot**.

## Decision

Add **backend-only** governed tooling (no `deploy/beta-agent` change; `op_release` + RELEASE dispatch are
already deployed), split into **three separate operator gates**:

1. **RELEASE client** (`mgmt_client.release()`): `_call("RELEASE", runtime)` over the existing signed channel
   (same HMAC/nonce/timestamp/expiry, path-free, per-op read timeout, runs outside the agent per-runtime lock);
   never infers success from `outcome==ok` — requires the agent's `released` fact. Plus a read-only
   `probe_occupancy()` over the existing VERIFY op (allowlisted, non-secret fields; never a path). `verify()`
   is untouched.

2. **Phase 1 — `reclaim_beta_runtime`** (dry-run by default, `--apply`): drives signed **STOP → TOMBSTONE →
   RELEASE** through `provisioner._step` (so the deployed PR#252 in-attempt reconcile / per-op timeout apply),
   under a **STABLE `job_id`** (default the runtime's retained PROVISION job) reused across every op and every
   re-invocation — the hard safety requirement. Fail-closed guards: BETA-only, no active job, provisioner DARK
   (unless `--allow-armed`), UUID/slot/generation/running probe match. On success the backend runtime goes
   `FAILED → REMOVED`; on any proven-partial / non-retryable / budget-exhausted-ambiguous step it **quarantines
   the runtime and never marks REMOVED**. `runtime_not_assigned` ⇒ already released (idempotent). Creates **no**
   ProvisioningJob.

3. **Phase 2 — `recover_beta_runtime`** (SEPARATE gate; dry-run by default, `--apply`): pure backend, **no agent
   contact**. Moves `REMOVED → NOT_PROVISIONED` (clearing quarantine) and enqueues **exactly one** claimable
   `PROVISION` job (`record_transition` has no allowed-transition guard, so this is legal; the post-assert
   proves exactly-one). Job #1 (`FAILED`) is retained as history. Idempotent (`uniq_active_job_per_runtime_op` +
   `select_for_update` + `enqueue_op` recovery). Provisioner stays **DARK** — the job is inert until a separate
   arming.

**Phase 3 (arm + retry to RUNNING)** is a later, separately-authorised operation; nothing here arms the
provisioner or advances a job.

Shared gate-free helpers live in `terminal_provisioning/recovery.py`; the two commands carry the only `--apply`
gates.

## Consequences

- The orphaned slot 2 can be reclaimed to Available (gen 4→5) and Customer Zero re-prepared, entirely through
  the **supported signed lifecycle** — never a manual `slots.sqlite`/filesystem edit — with immutable
  RuntimeEvent evidence and honest post-states.
- **Separation of concerns**: agent-occupancy repair (Phase 1) and backend-state preparation (Phase 2) are
  distinct authorities and distinct operator gates; neither provisions; arming is a third gate.
- No migration; no agent re-stage; deploys with a backend recreate. Provisioner stays DARK throughout cleanup
  and recovery.
- **Deferred / follow-ups** (not this change): (a) the stale `win_mutations.precheck_cleanup` docstring that
  claims `no_runtime_handles` "can never hold" — the deployed Restart-Manager `open_handles` answers it (a
  doc-only agent change, deferred to avoid manifest drift); (b) the agent `put`-inside-lock hardening from
  ADR-0023.

## Evidence

- Design panel + verified agent contract; 12-lens adversarial review before merge.
- Tests: `tests_reclaim_recovery.py` (release client, guards, exactly-one-job idempotency, reclaim
  happy/mismatch/already-released/fail-closed-quarantine, recover dry-run/apply/idempotent/require-REMOVED).
- Operator runbook + non-executed dry-run plans + evidence matrix: `docs/CZ_RECLAIM_RECOVERY_RUNBOOK.md`.
