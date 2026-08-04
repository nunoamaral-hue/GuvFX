# WP6-F — Operational Events Certification

Certify the operational-event read model + API (WP5.1/5.2, ADR-0032). Matrix cases: `OPE-1..8`. Gate:
`GATE-F`. Armed only in the disposable environment via `OPERATIONS_EVENTS_ENABLED` (+ the frontend flag for
the operator UI).

> **The projection is a non-authoritative, rebuildable-in-principle CACHE — never business state.** There is
> **no backfill/reproject tool** in the code; recovery is disable + re-accrete **forward** (OPE-8). Do not
> invent a rebuild tool (packet WS-F).

| Case | Certifies | PASS criteria | Repo evidence |
|------|-----------|---------------|---------------|
| OPE-1 | Projection correctness (each `project_*`) | correct category/severity/reason_code/customer_visible + non-secret metadata | `operational_events/tests_broker_projection.py` |
| OPE-2 | Deduplication (partial-unique `dedup_key` + get_or_create) | keyed replay collapses to one row; empty-key events stay distinct | `tests_broker_projection.py`, `tests_operational_events.py` |
| OPE-3 | Visibility (customer/operator separation) | non-staff → customer_visible + own only; staff → all | `tests_operational_events.py` |
| OPE-4 | Timeline completeness (newest-first, paginated) | no dropped/duplicated rows across pages | `tests_operational_events.py` |
| OPE-5 | Summary correctness (hybrid live-state + aggregates, visibility-scoped) | truthful state + counts; DARK → "not observed"; no operator-only leak | `tests_operational_events.py` |
| OPE-6 | Rollback (disable → DARK) | recorder no-op, API 404; authoritative state unaffected | `tests_operational_events.py` |
| OPE-7 | Projection failure (fail-open) | authoritative path unaffected; recorder-failure logged to `guvfx.operational_events` | `tests_operational_events.py` |
| OPE-8 | Rebuild expectations (**NO rebuild tool**) | no backfill command; recovery = disable + re-accrete forward; truncation loses history | `operational_events/tests_wp54_readiness.py` |

## Method + PASS

Drive each authoritative moment (validation / health / pause / resume / gate refusal / promotion / disconnect
/ credential) against demo accounts, then query the timeline + summary as staff and non-staff. For OPE-7,
inject a projection error and confirm the authoritative action still completes (fail-open) with a
recorder-failure log line. For OPE-8, confirm **no** reproject/backfill command exists and that recovery
re-accretes forward only. Confirm event-detail never renders forbidden metadata (`state_version`, raw enums,
ids, secrets). **PASS = every projection correct + deduped + visibility-scoped + rollback-safe, fail-open
proven, and the no-rebuild-tool reality documented and demonstrated.**
