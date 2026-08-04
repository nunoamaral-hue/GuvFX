# WP6-E — Health Certification

Certify the continuous broker-health engine (WP3, ADR-0030). Matrix cases: `HEA-1..11`. Safety-critical
gate: `GATE-E`. Armed only in the disposable environment via `BROKER_CONNECTIVITY_HEALTH_ENABLED`; health
converges on **customer validation evidence** (the scheduler stays an inert framework — no live validator).

## The six-state machine + contract

States: `UNKNOWN / HEALTHY / DEGRADED / STALE / DISCONNECTED / TOMBSTONED` (`reliability/models.py`).
Contract (`contract()`): `eligible == (state==HEALTHY)`; `pause_required == state ∈ PAUSE_STATES`;
`resume_eligible` set true only on a genuine net pause→HEALTHY recovery; `state_version` +1 per net change,
monotonic. `TOMBSTONED` is terminal.

| Case | Certifies | PASS criteria | Repo evidence |
|------|-----------|---------------|---------------|
| HEA-1 | `UNKNOWN → HEALTHY` on first success | eligible=true; resume_eligible=false (first time); version +1 | `reliability/tests_broker_health.py` |
| HEA-2 | `DEGRADED` at failure_threshold soft failures | eligible=false, pause_required=true; anti-flap latch | `tests_broker_health.py` |
| HEA-3 | `STALE` at stale_timeout | HEALTHY→STALE by time; recoverable | `tests_broker_health.py` |
| HEA-4 | `DISCONNECTED` at failure_threshold hard failures | eligible=false; latch holds | `tests_broker_health.py` |
| HEA-5 | `TOMBSTONED` terminal on disconnect | terminal; no transition out | `tests_broker_health.py` |
| HEA-6 | Transition timing / thresholds | recovery needs success_threshold consecutive successes (no single-success bounce) | `tests_broker_health.py` |
| HEA-7 | Convergence (net signalling) | transient dip resolved within one fold → no version churn/signal; evidence folded once (attempt-id watermark) | `tests_broker_health.py` |
| HEA-8 | Pause generation from PAUSE_STATES | version-keyed pause recorded; never resumes | `execution/tests_runtime_pause.py` |
| HEA-9 | Resume eligibility level | resume_eligible only on a true pause→HEALTHY net recovery, bound to that version | `tests_broker_health.py` |
| HEA-10 | **No automatic resume** | zero automatic resume paths; resume_eligible is a signal, not an action | `execution/tests_runtime_resume.py`, `tests_broker_health.py` |
| HEA-11 | State-version monotonicity | +1 per net change; never decreases/skips | `tests_broker_health.py` |

## Method + PASS

In the disposable environment, drive each demo account through the state machine by feeding validation
outcomes (success / soft-fail / hard-fail) and advancing time for staleness; capture the `get_contract`
series and `state_version` progression. Confirm the scheduler stays inert (`run_cycle` → `{ran:false}`),
that **nothing auto-resumes**, and that health emits **signals only** (no order/login/pause/resume side
effect from WP3 itself). **PASS = every transition deterministic, thresholds honoured, `state_version`
strictly monotonic, no automatic resume.**
