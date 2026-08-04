# WP6-I — Recovery Certification

Prove the capability recovers cleanly from every restart/retry without a duplicate or ineligible action, and
**without any production mutation**. Matrix cases: `REC-1..8`. Safety-critical gate: `GATE-I`. Disposable
environment only.

| Case | Recovery event | Expected | PASS criteria | Repo evidence |
|------|----------------|----------|---------------|---------------|
| REC-1 | Backend restart | flags read live re-evaluate; gates intact; no state loss | consistent post-restart; no unsafe window | `execution/tests_broker_gate.py` |
| REC-2 | Agent restart | integrity re-checked at start; validation fail-closed until ready | fail-closed until `integrity_ok` | `terminal_provisioning/tests_beta_agent_service.py` |
| REC-3 | Worker restart | resumes claiming; final-dispatch gate re-applied | no duplicate/ineligible dispatch on resume | `execution/tests_dispatch_gate.py` |
| REC-4 | Projection restart | recording resumes **forward**; no backfill of the gap; authoritative unaffected | forward-only; no false rebuild | `operational_events/tests_operational_events.py` |
| REC-5 | Scheduler restart | due-selection resumes; refusal parity intact; bar_close dedup holds | no duplicate refusal event; parity intact | `execution/tests_wse.py` |
| REC-6 | Validation retry (after transient UNAVAILABLE) | retryable UNAVAILABLE converges on retry | deterministic; no hot-loop | `terminal_provisioning/tests_broker_login_validation.py` |
| REC-7 | Health convergence after recovery | `success_threshold` consecutive successes → HEALTHY; resume_eligible set | converges deterministically; resume_eligible correct | `reliability/tests_broker_health.py` |
| REC-8 | Execution restart (disarm→rearm gate) | transparent when OFF; enforcing when ON | no ineligible order across the transition | `execution/tests_broker_gate.py` |

## Method + PASS

Restart each component (backend, agent, worker, scheduler; disable/re-enable the projection; disarm/re-arm
the execution gate in the disposable env) and confirm continuity: no duplicated order/job, no ineligible
action, no lost authoritative state, and the correct fail-closed window during the restart. For the
projection, confirm it re-accretes **forward** (no backfill). **PASS = every component recovers to a
consistent, safe state with no duplicate or ineligible action.** **No production mutation occurs during any
recovery test.**
