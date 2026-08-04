# WP6-H — Failure Injection Certification

Inject controlled failures in the disposable environment and prove the capability **fails closed** (or
**fails open** where that is the design, i.e. the operational-event projection). Matrix cases: `FAIL-1..11`.
Safety-critical gate: `GATE-H`. **No production failure injection** — disposable environment only.

| Case | Injected failure | Expected state | Rollback / recovery | PASS criteria | Repo evidence |
|------|------------------|----------------|---------------------|---------------|---------------|
| FAIL-1 | Backend unavailable | fail-closed; customer-safe UNAVAILABLE; no partial state | backend restart (REC-1) | no unsafe state; recoverable | `terminal_provisioning/tests_broker_login_validation.py` |
| FAIL-2 | Agent unavailable | validation UNAVAILABLE (retryable); customer not asked to re-enter correct creds | agent restart (REC-2) | fail-closed; no order | `terminal_provisioning/tests_mgmt_channel.py` |
| FAIL-3 | Validation unavailable (dirty baseline / isolation fault) | UNAVAILABLE; probe never runs against an unproven path | rebuild image / 6073 swap (RBK-5) | fail-closed; operator-alertable | `terminal_provisioning/tests_validation_image.py` |
| FAIL-4 | Worker unavailable | jobs remain PENDING; no dispatch; final-dispatch gate intact on resume | worker restart (REC-3) | no order while down; clean resume | `execution/tests_dispatch_gate.py` |
| FAIL-5 | Timeout (validation transport) | AMBIGUOUS timeout reconciled, never treated as failure; contract 175>165>120+30 holds | reconcile | fail-closed reconcile; contract asserts pass | `tests_beta_worker_timeouts.py`, `tests_phase2_contract.py` |
| FAIL-6 | Bridge unavailable | no dispatch via bridge; upstream claim gate still refuses ineligible jobs | bridge restart | no order; no ineligible job served | `execution/tests_bridge_auth.py` |
| FAIL-7 | Credential failure (unsealable / scrub-unverified) | UNAVAILABLE; HEALTHY never written before a verified scrub | retry | no HEALTHY-with-credential; fail-closed | `tests_broker_login_validation.py` |
| FAIL-8 | Health failure (read error at dispatch) | fail-closed (`SR_HEALTH_STATE_CHANGED`); order withheld | — | health read error fails closed | `execution/tests_dispatch_gate.py` |
| FAIL-9 | Projection failure | **fail-open**: authoritative path unaffected; logged to `guvfx.operational_events` | disable + re-accrete forward | no caller break; authoritative correct | `operational_events/tests_operational_events.py` |
| FAIL-10 | Database rollback | `on_commit` projection discarded → no phantom event | — | no phantom event; authoritative rollback clean | `operational_events/tests_broker_projection.py` |
| FAIL-11 | Network interruption (management channel) | fail-closed / reconciled; no partial provisioning or order | reconcile | no unsafe partial state | `terminal_provisioning/tests_mgmt_channel.py` |

## Method + PASS

For each case, inject the fault against a demo account (stop a service, corrupt the baseline, force a
timeout, roll back a transaction, sever the channel) and observe the resulting state, events, and rollback
path. Every case except FAIL-9/FAIL-10 must **fail closed** (no order, no unsafe partial state); FAIL-9 must
**fail open** (authoritative action unaffected); FAIL-10 must produce **no phantom event**. **PASS = every
injected fault yields the expected safe state with the expected events and a working recovery path.** Any
fault that opens exposure, leaks a secret, or leaves an unrecoverable state is a **NO-GO**.
