# WP6-K — Capacity Baselining

Design the **measurements** required to establish the Trusted-Beta operating limits. **No thresholds are
invented.** Every value is **`TO BE MEASURED`** during WP6 execution in the disposable environment. Matrix
cases: `CAP-1..10`. Gate: `GATE-K`. These measurements feed the WP5.4
[trusted-beta-readiness.md](trusted-beta-readiness.md) capacity framework (whose values are `WP6-BASELINED`)
and the [monitoring-spec.md](monitoring-spec.md) signals (whose thresholds are `TO BE BASELINED DURING WP6`).

| Case | Measurement | Bound by (grounded) | Value | Repo evidence |
|------|-------------|---------------------|-------|---------------|
| CAP-1 | Validation throughput | single isolated terminal + global single-flight lock + timeout contract | `TO BE MEASURED` | `terminal_provisioning/tests_beta_capacity.py` |
| CAP-2 | Execution throughput | gate + execution pipeline | `TO BE MEASURED` | — |
| CAP-3 | Queue depth | PENDING volume + claim gate | `TO BE MEASURED` | `execution/tests_killswitch_claim.py` |
| CAP-4 | Operator capacity | support-workflow load/day | `TO BE MEASURED` | — |
| CAP-5 | Support capacity | ticket volume/day | `TO BE MEASURED` | — |
| CAP-6 | Concurrent accounts | capacity caps | `TO BE MEASURED` | `trading/tests_beta_account_cap.py` |
| CAP-7 | Concurrent users | capacity | `TO BE MEASURED` | `terminal_provisioning/tests_beta_capacity.py` |
| CAP-8 | Health latency (convergence delay) | fold path (scheduler inert; converges on customer evidence) | `TO BE MEASURED` | `reliability/tests_broker_health.py` |
| CAP-9 | Event lag | **metric does not exist — must be ADDED then measured** | `TO BE MEASURED` | — |
| CAP-10 | Response times (operator API) + error rate | **counter does not exist — must be ADDED then measured** | `TO BE MEASURED` | `reliability/tests_operations_summary.py` |

## Honesty notes (grounded)

- **CAP-9 (event lag) and CAP-10 (operator-API error rate/response time) do not exist as metrics** in the
  repo — they must be **added**, not merely measured (consistent with [monitoring-spec.md](monitoring-spec.md)
  §13–14). Adding those metrics is engineering work outside this planning packet.
- **No numeric baseline** exists anywhere in the repository for any capacity dimension; certification
  establishes them from measurement.

## Method + PASS

In the disposable environment, drive each dimension under controlled load and record the measured value plus
the measurement method and limitations, in an evidence manifest. Derive the initial Trusted-Beta operating
limits from the measured values (never invented). **PASS (for the plan) = every capacity dimension has a
defined measurement method + an owner; the measured value is `TO BE MEASURED` until execution.** No
uncontrolled beta invitation is permitted; limits must be set from evidence before arming stage 7.
