# 0023 — Per-operation transport timeout + in-attempt ambiguous reconcile for beta provisioning

- Date: 2026-08-01
- Status: Accepted
- Decision class: Amber (touches the provisioning execution / retry path — `.claude/rules/architecture.md`)

## Context

The first genuine controlled Customer Zero provisioning (2026-08-01) drove `ProvisioningJob #1` (runtime
`66972e0e-…`) into `FAILED` at **MATERIALISE**, even though the golden copy **completed on the host**. Two
client-side (backend) defects combined:

1. **One blanket transport timeout.** `beta_worker.make_http_transport()` applied a single
   `DEFAULT_TRANSPORT_TIMEOUT = 20s` to *every* agent operation. MATERIALISE copies the ~380 MB golden into a
   slot (measured **~41s** on the beta host) — legitimately far longer than a handshake — so the client's
   `requests.post(timeout=20)` raised `op_ambiguous_timeout` while the copy was still running.
2. **Blind re-POST + mis-classification.** On the ambiguous timeout the driver immediately re-POSTed the same
   MATERIALISE. The agent holds the per-runtime lock for the whole copy and stores its idempotent
   `(job_id, op)` result only *after* the copy completes, so the resends returned `runtime_busy`.
   `provisioner._step`'s generic `except Exception` mapped `runtime_busy` to a plain `materialise_failed`,
   which — on the exhausting attempt — routed to the hard-`FAILED` branch. `MAX_ATTEMPTS = 3` burned in ~0.3s.

Verified against the code (not the incident narrative): the agent's `idempotency_store.put` runs **outside**
the runtime lock (`mgmt_agent_core.handle`), so a resend arriving in the microseconds between lock-release and
`put` could re-enter the impl — but this window is **benign** for MATERIALISE (`SlotResolver.assign` is
idempotent → same slot/generation; `stage_copy` is destination-guarded → returns `ALREADY_COMPLETED` with no
physical re-copy, and refuses a partial) and was **not** the incident's cause (the incident's `runtime_busy`
came from resends *during* the copy, lock held).

MetaTrader/agent constraints ruled out several tempting fixes: raising every timeout to a huge value (masks a
genuinely hung agent), and an asynchronous submit/poll protocol (a signed-contract + manifest change to the
agent, high-cost and out of scope). The agent primitives are checksum-pinned in `deploy/beta-agent/manifest.json`;
any agent-side change requires a manifest regen + host re-stage + restart.

## Decision

Fix the incident **entirely on the backend (client) side** — deployable with a single backend recreate, **no
migration, no agent change**:

1. **Per-operation `(connect, read)` transport timeout** (`beta_worker.OP_TRANSPORT_TIMEOUTS`,
   `_op_read_timeout`). CONNECT stays short (10s) for every op so an unreachable agent fast-fails regardless of
   the read budget; the READ budget is per-op: NEGOTIATE 10, VERIFY 15, RELEASE 30, START 60, STOP 90,
   TOMBSTONE 120, **MATERIALISE 300** (bounded ~7× the measured 41s copy). Centrally governed, overridable via
   `settings.BETA_AGENT_OP_TIMEOUTS` / the env, every value **clamped** to `MAX_TRANSPORT_READ_TIMEOUT = 600`
   so no configuration produces an unbounded wait. The transport only *reads* the already-signed `operation`
   field — signature, nonce and body are untouched.

2. **In-attempt reconcile — poll-not-repost** (`provisioner._reconcile`). A `ManagementChannelTimeout` **or** a
   `runtime_busy`/`agent_busy`/`agent_stopping` reply is reconciled *inside the same attempt*: **wait**
   (exponential backoff 5→30s), fire the liveness heartbeat, then re-send the **same** idempotent `(job_id, op)`.
   The agent returns its stored result once the op completes. `runtime_busy` is treated as **positive evidence
   the original op still holds the per-runtime lock**, never a failure — it can no longer exhaust the retry
   budget. Bounded by `PROVISIONING_MATERIALISE_MAX_WAIT_SECONDS = 300` wall-clock; on exhaustion it raises a
   single **ambiguous** error → the runtime is **quarantined** (a deterministic terminal outcome, never a "safe
   to re-launch" `FAILED`).

3. **Fail-closed on a proven partial** (`PARTIAL_REASONS` in `_step`/`_fail_step`). A `stage_copy_*` /
   integrity refusal is non-retryable and additionally **quarantines** the runtime (`partial_materialise`), so a
   half-materialised slot is never silently re-driven as success.

4. **Lease/timeout coupling.** `LEASE_TTL_SECONDS` raised 300 → **1200** so a job whose first POST is
   legitimately in flight is never re-claimed by a second worker (which would fire a concurrent MATERIALISE).
   `assert_lease_covers_op_timeouts()` fails closed at worker startup (and in CI) if a future change breaks
   `LEASE > materialise_read + reconcile_budget`.

5. **Heartbeat coupling.** `BETA_PROVISIONER_HEARTBEAT_TTL_SECONDS` default raised 120 → **900** (a single
   blocking MATERIALISE POST emits no mid-copy heartbeat; at 120 a healthy long copy would false-trip
   `provisioner_unhealthy` and refuse a concurrent *new* reservation — fail-closed either way).

## Deferred (explicitly, with justification)

- **Agent `put`-inside-lock hardening.** Moving `idempotency_store.put` inside the runtime lock in
  `mgmt_agent_core.handle` closes the (benign) post-completion re-execution window structurally. It is **not
  required** to fix the incident and is **high-cost** (manifest regen + host re-stage + agent restart, behind
  the RULE 9/11 ASCII-only + `Parser::ParseFile` gate). Scheduled for the next agent re-stage. **Binding
  invariant until it ships:** *every `_MUTATING` agent op must be internally re-entrant / idempotent* (true
  today: `assign` idempotent, `stage_copy` `ALREADY_COMPLETED`, START observes-first, STOP short-circuits on
  ABSENT). A future non-idempotent op would reopen a real hazard and must not land before this change.

- **Backend RELEASE driver + orphaned-slot reclaim command.** These are recovery tooling, not incident
  remediation; specified in the Customer Zero recovery plan and built under the separate cleanup authorisation.

## Consequences

- The exact incident (MATERIALISE timeout → `runtime_busy` → false `FAILED`) now **recovers to RUNNING**
  (regression test `tests_beta_worker.test_runtime_busy_during_copy_is_reconciled_then_reaches_running`).
- A single legitimately long copy blocks the (single) beta provisioner for up to ~600s (first POST + reconcile).
  Acceptable for DARK single-tenant Customer Zero; a real async job-state machine is the eventual scaling fix.
- All governing invariants are preserved unchanged: HMAC auth, nonce/replay, path-free requests, slot-pool
  isolation, runtime-lock exclusivity, golden digest + manifest verification, generation monotonicity, Variant-A
  LiveUpdate containment, production isolation, DARK-by-default, fail-closed, never-partial-as-success.
- No database migration; no agent-side change; deployable with a backend recreate.

## Evidence

- Incident + host forensics: `docs/POST_INCIDENT_CZ_MATERIALISE_TIMEOUT.md`.
- Tests: `tests_beta_worker_timeouts.py` (13), `tests_beta_worker.py` reconcile/quarantine/partial cases;
  full `terminal_provisioning` suite green (858).
