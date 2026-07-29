# ADR-0021 PR B — Broker-Login Validation: Implementation + Evidence

Branch `feat/adr-0021-pr-b-broker-login` (stacked on merged PR A). Behind the default-OFF flag
`PROVISIONING_REQUIRE_BROKER_LOGIN` — the broker-INDEPENDENT phase is unchanged until the flag is ON.

## Permanent rule (implemented)

A customer runtime may reach **RUNNING** only after the assigned MT5 terminal has established a genuine
session with the **submitted** broker account and the returned account identity has been verified. When
`PROVISIONING_REQUIRE_BROKER_LOGIN=1`, `_start_and_verify` gates the RUNNING transition behind, in order:
process up → genuine login (`logged_in`) → returned **login** matches the submitted account number →
**server** identity consistent (when a normalised `broker_server` is set) → **demo/live** classification
matches the declared `is_demo`. Any failure raises a sanitised `ProvisionStepError`; the runtime never
reaches RUNNING and no Verification Report is written.

- MT5 launches from the **exact assigned beta-slot runtime path** (the Windows primitive layer acts on a
  fixed slot identity/dir — see `.claude/rules/architecture.md`; the driver never chooses a path).
- Login uses the submitted **account number**, **broker server** (`broker_server.server_name`) and the
  **decrypted password** via the sanctioned credential path (`decrypt_password` → `configure(...)` over
  the authenticated channel), with a redacted `CREDENTIAL_ACCESSED` audit at decrypt time.

A **normalised `broker_server` is required** under `require_login`: a free-text `broker_name` is not an
MT5 server, so a server-less account cannot perform a genuine login and fails closed with
`broker_server_required` (non-retryable) — it never reaches RUNNING with an unverified server leg. With a
server present, the server-consistency check **always** applies. Customer Zero's account #11 is free-text
today; before the flag is flipped ON it needs a normalised `broker_server` (surfaced truthfully as
`broker_server_required`; resubmit/no-duplicate continuity is unaffected).

## Durable failure taxonomy (10 states)

| Failure mode | Durable code | Retry policy |
|--------------|--------------|--------------|
| invalid credentials | `broker_login_failed` | non-retryable (immediate FAIL — no loop on unfixable input) |
| no MT5 server (free-text only) | `broker_server_required` | non-retryable (config error) |
| broker server unavailable | `broker_server_unavailable` | retryable (bounded by MAX_ATTEMPTS) |
| MT5 initialisation failure | `mt5_init_failed` | retryable |
| login timeout | `broker_login_timeout` | retryable |
| channel/agent timeout (ambiguous) | `op_ambiguous_timeout` | retryable → quarantine on repeat (never re-launch) |
| agent unavailable (mid-step) | sanitised step error (retryable) / negotiation `DEGRADED` | retryable |
| terminal crash / not running | `terminal_not_running` (or `terminal_crashed`) | retryable |
| account-identity mismatch (login/server) | `broker_identity_mismatch` | non-retryable (fail closed) |
| demo/live mismatch | `demo_live_mismatch` | non-retryable (fail closed) |
| unexpected technical error | sanitised `*_failed` code | retryable |

The demo/live check compares **strict booleans** (`is_demo is True`/`is False`) — a missing key, JSON
`null` (undetermined), or a non-boolean value is UNVERIFIED and fails closed (never a truthiness coercion
that could pass a live account on an undetermined classification).

## Adversarial review

A focused adversarial review found **no critical** defect (no clean path to RUNNING +
`broker_login_verified=True` without a genuine verified login) and two MEDIUMs, both resolved:
- **demo/live used `bool()` coercion** (a present-null could pass a live account) → strict-boolean compare;
- **server leg skippable while the report still claimed verified** → a real `broker_server` is now
  required under `require_login`, so the server is always verified before RUNNING.
Confirmed sound: exact login compare; retry safety (no-loop / bounded / idempotent reuse); `broker_login_verified`
originates only from the post-gate path (single caller, atomic with RUNNING); no order/ExecutionJob/`order_send`
reachable; estate safety (`_require_beta`, cohort scoping). Tests were added to pin the two fixes.

Every code is recorded on the immutable `RuntimeEvent`, on `job.last_error`, and on
`runtime.last_failure_reason` — all sanitised (≤64 chars, no raw strings, no secrets).

## Retry safety

- **Bad credentials never loop** — `broker_login_failed` is non-retryable → the job FAILS after exactly
  one attempt; a further advance is a no-op on the terminal job.
- **Transient failures are bounded** — retryable failures re-queue and are re-attempted up to
  `MAX_ATTEMPTS`, then FAIL truthfully.
- **A successful retry reaches RUNNING** and leaves **exactly one** runtime and **one** job.
- **Resubmit/resume is idempotent** — `reserve_beta_slot` returns the held runtime; a duplicate
  `enqueue_op` returns the existing active job (PR-A `uniq_active_job_per_runtime_op`). No second runtime,
  no second active job.

## Credential safety

- The plaintext password is decrypted transiently and passed to `configure(...)` over the channel — never
  a command-line argument, never persisted by the provisioner.
- Proven durable-evidence-clean: across `RuntimeEvent` (reason/detail/state), `job.last_error`,
  `runtime.last_failure_reason` and the Verification Report fields, the synthetic secret appears **nowhere**
  — asserted on both the success and the failure path.
- A redacted `CREDENTIAL_ACCESSED` audit (`purpose="runtime-configure"`) is emitted; no secret in it.

## No-order proof

Broker validation performs **no trading action**. After a full successful validation:
`Trade.objects.count() == 0`, `ExecutionJob.objects.count() == 0`; the provisioner's `calls` contain no
order op; the provisioner interface exposes **no** `order_send`/`place_order` capability. (Order execution
is a separate plane; provisioning never touches it.)

## Host isolation

Validation acts only on the **assigned disposable beta slot**. It does not touch Nuno's legacy runtime,
account #1, `guvfx_u_1`, the Golden Execution Reference assignments, or other beta slots (read-only
comparison excepted). The slot identity/dir/tasks are fixed at the Windows-primitive layer.

## Customer Zero #11 continuity

Re-submitting or resuming an existing account returns the same account (PR-A idempotency), reuses exactly
one runtime and one active provisioning job, and continues the state-driven frontend journey. Proven at the
provisioning layer here; the account-row no-duplicate guarantee is PR-A (`tests_beta_provisioning_wiring`).

## Tests

`backend/terminal_provisioning/tests_broker_login.py` — happy path (RUNNING only after login verified +
report), the full 9-state failure taxonomy, retry safety (no-loop / bounded / resume-to-RUNNING /
idempotent reuse), credential safety (no plaintext in evidence + audit), no-order proof, and continuity.

## Host certification — PLAN (BLOCKED on a Sponsor-supplied disposable demo account)

The repository implementation and unit/integration proofs are complete. **Isolated disposable-demo host
certification is BLOCKED** and requires the Sponsor to:
1. Create a **disposable demo** MT5 account (NOT Customer Zero's, NOT any live account) and submit it
   through the public UI for a fresh non-admin beta identity.
2. Confirm the target disposable beta slot is free.

Then the certification run (flag `PROVISIONING_REQUIRE_BROKER_LOGIN=1`, single slot) proves on the host:
terminal launches from the exact slot path; genuine login to the submitted demo account; returned
identity + server + demo classification match; runtime reaches RUNNING with a Verification Report; **zero
orders/deals/positions/pending, no `order_send`**; Nuno's estate untouched (before/after Golden-Reference
STOP-check); with rollback evidence. I cannot create accounts or enter credentials — the Sponsor performs
the credentialed steps; I drive the provisioning + capture evidence.
