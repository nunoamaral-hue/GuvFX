# WP6-C — Concurrency Certification

Prove the capability is safe under concurrent load — no unsafe interleaving, no double-action, no stale
decision winning. Matrix cases: `CON-1..9`. Gate: `GATE-C`.

The design already provides the concurrency primitives; certification exercises them in the disposable
environment under real contention. Primitives (grounded in code): the agent **global single-flight lock**
(validation), the beta worker **single-flight lease** (runtime creation), **`select_for_update`** row locks
(pause/resume, credential replace, account), **`select_for_update(skip_locked)`** due-selection (health
scheduler + `next_job` claim), and **`state_version` monotonicity** (health + resume).

| Case | Race | Expected outcome | PASS criteria | Repo evidence |
|------|------|------------------|---------------|---------------|
| CON-1 | Simultaneous validation | Global single-flight lock serialises; no interleaved probe | One-at-a-time; deterministic serialisation | `terminal_provisioning/tests_validate_login_agent.py` |
| CON-2 | Simultaneous runtime creation | Single-flight lease; one claimer per job | No double-provision | `terminal_provisioning/tests_beta_worker.py` |
| CON-3 | Simultaneous execution requests (ineligible acct) | All blocked at `ExecutionJob.save` | Zero ineligible jobs created | `execution/tests_broker_gate.py`, `tests_harden_jobs.py` |
| CON-4 | Credential replacement race | Account row-lock serialises; eligibility reset wins | No order on invalidated eligibility | `trading/tests_credential_invalidation.py` |
| CON-5 | Pause/resume race | Version-keyed under row lock; newer pause wins; stale resume ignored | No stale resume clears a newer pause | `execution/tests_runtime_pause.py`, `tests_runtime_resume.py` |
| CON-6 | Validation retry race | NEEDS_ATTENTION not auto-retried; UNAVAILABLE retryable only | No hot-loop on non-retryable faults | `terminal_provisioning/tests_broker_login_validation.py` |
| CON-7 | Scheduler concurrency (h1/m5/h4) | Each catches `ExecutionGateRefused`, re-emits durable audit + projection (bar_close-deduped) outside the rolled-back atomic | No unhandled 500; no duplicate event | `execution/tests_wse.py`, `reliability/tests_broker_health.py` |
| CON-8 | Queue pressure at the claim boundary | `next_job` `select_for_update(skip_locked)` gate under load | Zero ineligible exposure-opening dispatch under load | `execution/tests_killswitch_claim.py`, `tests_dispatch_gate.py` |
| CON-9 | State-version race | Monotonic +1 per net change; stale ignored | No skip/decrease; stale resume ignored | `reliability/tests_broker_health.py`, `tests_runtime_resume.py` |

## Method

Drive each race with concurrent workers/requests in the disposable environment against demo accounts.
Capture the ordering/decision transcript and the resulting durable state (jobs, pause rows, health
`state_version`, audit/projection rows). For scheduler cases, run h1/m5/h4 concurrently against an
ineligible account and confirm refusal parity + dedup.

## PASS

**No unsafe interleaving in any case**: no ineligible order, no double-provision, no stale resume, no
version skip/decrease, no duplicate durable event, no hot-loop. Any unsafe interleaving is a NO-GO for the
affected safety-critical path (execution/health) or a documented condition for the non-safety paths.
