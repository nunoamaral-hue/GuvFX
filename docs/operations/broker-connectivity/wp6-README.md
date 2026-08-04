# WP6 — Multi-Tenant Certification Programme (Broker Connectivity)

**Status:** Certification **PLAN + design** only. **Nothing is executed, armed, or deployed by this
package.** All six flags remain OFF; Customer Zero and production are untouched; no live accounts; disposable
demo accounts only. WP6 *planning* is authorised; WP6 *execution* is a separate, Sponsor-gated activity that
requires the disposable environment (a Nuno-provided demo account + the Windows host — `HOST-VERIFIED /
OUTSIDE REPOSITORY CONTROL`).

**Programme:** GuvFX Broker Connectivity Capability, Sprint 6. Follows WP5.4 (the operations-readiness
package). WP6 is the **final engineering + operational verification before any Trusted-Beta arming**.

---

## What this package is

WP6 proves the complete broker-connectivity capability is **safe to arm**. This package designs the
certification programme: for every engineering area it defines the **test matrix, expected evidence, and
PASS criteria** — grounded in the merged code and the existing test suite — plus the machine-readable
artefacts and the release-gate decision matrix. It does **not** run the certification (that needs the
disposable environment) and it makes **no release recommendation** (the recommendation is null until
certification completes).

## Certification principle (no assumption, no inferred PASS)

Every certification item must produce **evidence**. A PASS requires **execution → observation → evidence →
acceptance criteria**. Nothing in this repository is marked PASS: every matrix case and gate item is
`PLANNED`/`PENDING`. The validation test `backend/operational_events/tests_wp6_certification.py` enforces
this (no case may be PASS; the release recommendation must be null; WP6 execution must not be marked
complete).

## Documents

| File | Certification area |
|------|--------------------|
| [`wp6-test-environment.md`](wp6-test-environment.md) | A — Certification environment |
| [`wp6-isolation.md`](wp6-isolation.md) | B — Isolation |
| [`wp6-concurrency.md`](wp6-concurrency.md) | C — Concurrency |
| [`wp6-execution-safety.md`](wp6-execution-safety.md) | D — Execution safety (+ definitive route inventory) |
| [`wp6-health.md`](wp6-health.md) | E — Health |
| [`wp6-operational-events.md`](wp6-operational-events.md) | F — Operational events |
| [`wp6-operator-workflow.md`](wp6-operator-workflow.md) | G — Operator workflow (17 workflows) |
| [`wp6-failure-injection.md`](wp6-failure-injection.md) | H — Failure injection |
| [`wp6-recovery.md`](wp6-recovery.md) | I — Recovery |
| [`wp6-rollback-rehearsal.md`](wp6-rollback-rehearsal.md) | J — Rollback rehearsal |
| [`wp6-capacity.md`](wp6-capacity.md) | K — Capacity (all `TO BE MEASURED`) |
| [`wp6-release-recommendation.md`](wp6-release-recommendation.md) | L — Release recommendation (decision matrix) |

Machine-readable: [`wp6-test-matrix.json`](wp6-test-matrix.json) (every case),
[`wp6-evidence.json`](wp6-evidence.json) (evidence requirements),
[`wp6-release-gate.json`](wp6-release-gate.json) (GO / GO-WITH-CONDITIONS / NO-GO decision matrix).
Validation test: `backend/operational_events/tests_wp6_certification.py`.

## Relationship to WP5.4

WP6 **consumes** the WP5.4 package: the arming runbook, rollback matrix, incident-response, support-playbook
(the 17 workflows certified under area G), monitoring-spec (the signals whose baselines area K measures), and
the readiness checklist (the entry criteria). WP6 does not restate them; it certifies against them.

## Hard boundary (this package)

Do **not** invite beta users; arm any flag; enable operational events / execution gate / customer frontend
or backend / health engine; deploy any code; modify production; mutate Customer Zero; delete data; use live
accounts; or test against production users. Repository engineering, certification planning, and
disposable-environment testing only. Every certification run is Sponsor-gated.

## Grounding + conventions

- Grounded in the merged repo — every certification case cites the existing test file(s) that already prove
  the mechanism, plus the disposable-environment method to certify it end-to-end.
- The **definitive execution route inventory** is `backend/execution/execution_entrypoints.json`; area D
  covers every exposure-opening route in it (the validation test cross-checks this).
- Host/production facts are marked **HOST-VERIFIED / OUTSIDE REPOSITORY CONTROL**.
- No secret or env-var **values** — names only.
- Capacity thresholds are **not invented** — every value is **`TO BE MEASURED`**.
