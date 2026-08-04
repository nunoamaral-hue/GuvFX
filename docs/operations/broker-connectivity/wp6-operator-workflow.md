# WP6-G — Operator Workflow Certification

Exercise **every one of the 17 WP5.4 support-playbook workflows** end-to-end in the disposable environment.
Matrix cases: `WFL-1..17` (the WP6 validation test asserts all 17 are covered). Gate: `GATE-G`.

For each workflow the certification records: **steps · expected UI · expected events · expected audit ·
expected operator actions · PASS criteria.** The authoritative state to inspect and the customer-safe
wording are defined in [support-playbook.md](support-playbook.md); WP6 certifies that the live behaviour
matches it. **Operational events are treated as a non-authoritative projection throughout.**

| Case | Workflow | Authoritative state to inspect | Expected events | PASS criteria | Repo evidence |
|------|----------|--------------------------------|-----------------|---------------|---------------|
| WFL-1 | User cannot add an account | flag/arming state; `brokeridentity_present` | CONNECTIVITY (if any) | matches playbook; no prohibited action | `trading/tests_broker_connectivity.py` |
| WFL-2 | Connection test fails | latest `BrokerAccountValidationAttempt`; `validation_status` | VALIDATION (severity by status) | correct NEEDS_ATTENTION vs UNAVAILABLE; customer-safe wording | `terminal_provisioning/tests_broker_login_validation.py` |
| WFL-3 | Technical validation unavailable | attempt `reason_code` (UNAVAILABLE set) | VALIDATION ERROR | UNAVAILABLE retryable, platform fault not credential fault | `tests_broker_login_validation.py` |
| WFL-4 | Invalid credentials | attempt (retryable=false); `validation_status=CONNECTION_FAILED` | VALIDATION WARNING | NEEDS_ATTENTION not retryable | `tests_broker_login_validation.py` |
| WFL-5 | Live account where demo required | attempt `is_demo=false`, status HEALTHY | VALIDATION INFO | `live_detected` = connected classification, not a failure; Sponsor-gated exception | `trading/tests_classification_crosscheck.py` |
| WFL-6 | Account disconnected | `disconnected_at` + `is_active=false` + row retained | CONNECTIVITY disconnect | tombstone, not delete; credential destroyed | `trading/tests_credential_destruction.py` |
| WFL-7 | Credential replaced | `validation_status→NEVER`, health→UNKNOWN | CREDENTIAL replaced + invalidated | eligibility reset; re-validation required | `trading/tests_credential_invalidation.py` |
| WFL-8 | Broker health degraded | `get_contract` DEGRADED | HEALTH | no execution effect unless the gate is armed | `reliability/tests_broker_health.py` |
| WFL-9 | Broker health stale | `get_contract` STALE | HEALTH | STALE = no fresh evidence, not disconnected | `reliability/tests_broker_health.py` |
| WFL-10 | Runtime paused | `BrokerRuntimePause` | RUNTIME paused | never auto-resumes | `execution/tests_runtime_pause.py` |
| WFL-11 | Controlled resume requested | live contract via `get_contract` | RUNTIME resume | explicit-only; live re-check; Sponsor-gated | `execution/tests_runtime_resume.py` |
| WFL-12 | Execution refused | `validation_status` + health contract | EXECUTION refused | refusal = gate working (ineligible); eligible-account refusal is a defect | `execution/tests_broker_gate.py` |
| WFL-13 | Operational timeline empty | flag state; recorder logs; authoritative state | none | empty ≠ nothing happened (DARK/cache/recorder-failure) | `operational_events/tests_operational_events.py` |
| WFL-14 | Event visible to operator not customer | event `customer_visible` | operator-only event | intended visibility split; cross-owner would be the leak | `tests_operational_events.py` |
| WFL-15 | Duplicate or missing operational event | `dedup_key`; authoritative model/audit | dedup / gap | dedup enforced; gap ≠ data loss | `operational_events/tests_broker_projection.py` |
| WFL-16 | User asks to delete broker account | tombstone state; destruction audit | CONNECTIVITY + CREDENTIAL | soft-disconnect + destruction + tombstone, never row-delete | `trading/tests_credential_destruction.py` |
| WFL-17 | User requests credential removal | destruction audit | CREDENTIAL | verified secure clear + audit; never read back | `trading/tests_credential_destruction.py` |

## Method + PASS

In the disposable environment, drive each workflow against a demo account (and, where relevant, a second
tenant to prove visibility). Capture the steps, the operator-visible UI, the projected events, the
authoritative audit, and the operator actions taken vs the playbook's permitted/prohibited lists. **PASS =
each workflow behaves exactly as the support-playbook documents, with no prohibited action and no
operational-event misused as authoritative state.**
