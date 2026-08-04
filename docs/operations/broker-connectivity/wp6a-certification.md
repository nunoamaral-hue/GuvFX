# WP6A — Shared-Environment Operational Certification Record

**Outcome:** see [wp6a-pilot-recommendation.md](wp6a-pilot-recommendation.md) — **GO WITH CONDITIONS** for a
tightly-controlled Internal Pilot. **This packet does NOT authorise Trusted Beta, production arming, or
customer invitations. Everything remains DARK; all six flags OFF.**

**Scope.** Non-destructive certification that the engineered capability **behaves correctly** in the current
shared environment. It does **not** prove multi-tenant isolation under stress — that is **WP6B**, which
remains **outstanding** (see [wp6a-pilot-recommendation.md](wp6a-pilot-recommendation.md) §"Deferred WP6B").

**How this was certified (no inferred PASS).** The only non-destructive execution permitted by the packet is
running the merged automated test suite (which exercises each behaviour against fixtures/mocks — no live
accounts, no production, no service stops, no flag enablement in production) plus governance + `make check`.
Evidence was **executed on 2026-08-04** against `main` @ `b3e0bba`:

- **Backend: 387 tests across 19 broker-connectivity modules — all OK** (per-module counts below).
- **Frontend: 46 Operations-UI tests across 10 files — all OK.**
- **Full `make check` green** (backend 2571, secret-scan clean, parity OK, frontend build OK).

**Verdict vocabulary.** `PASS` = proven by executed tests + code inspection. `HOST-VERIFIED` = correct in
the repo but the live-host state cannot be certified from the repository (requires operator confirmation).
`DEFERRED-WP6B` = belongs to the deferred stress/isolation certification and is **not** claimed here.

> **Scope of a `PASS`.** An area `PASS` certifies **engineered correctness** (the behaviour is proven by the
> executed tests). It is **not** a live-readiness claim — real shared-environment readiness is additionally
> gated by that area's HOST-VERIFIED items and the pilot conditions. In particular, **WS-B is `PASS` for the
> engineered lifecycle but a live demo `VALIDATE_LOGIN` has NOT succeeded on the host** (pilot condition 1).
> Read each verdict together with its HOST-VERIFIED notes and
> [wp6a-pilot-recommendation.md](wp6a-pilot-recommendation.md).

---

## WS-A — Environment verification

| Item | Verdict | Evidence |
|------|---------|----------|
| Deployed commit | PASS (repo) / HOST-VERIFIED (prod) | `main` @ `b3e0bba`; whether prod runs this commit is HOST-VERIFIED |
| Feature flags OFF | PASS | All six default OFF in code (`feature-flags.json` + definition sites); `tests_wp54_readiness.py` asserts defaults OFF |
| build-5833 active | HOST-VERIFIED | Pinned in `deploy/beta-agent/validation_image.py:31` (`SOURCE_BUILD_TERMINAL = "5.0.0.5833"`) + source-hash allow-list; whether it is the ACTIVE host image is HOST-VERIFIED |
| Validation image healthy | PASS (governance) / HOST-VERIFIED (host) | `terminal_provisioning.tests_validation_image` — 16 OK (verify_image, source-hash drift, forbidden-artefact, run-in count); host `verify_image` is HOST-VERIFIED |
| Timeout contract | PASS | `terminal_provisioning.tests_phase2_contract` — 11 OK; asserts hold at import (175 > 165 > 120 + 30) |
| Rollback references | PASS | `rollback-matrix.md` + `wp6-rollback-rehearsal.md`; 6073 rollback baseline recorded in `validation_image_manifest.json` |
| Manifests | PASS | Agent `manifest.json` checksums; `validation_image_manifest.json`; drift tests green |
| Governed image | HOST-VERIFIED | Golden pin `.guvfx_golden_manifest`; live golden build is HOST-VERIFIED (`STATUS.md` records `5.0.0.6036` promoted on host) |
| Parity | PASS | `npm run verify:parity` green (41 routes / 46 components / 2 env vars) |
| ADR references | PASS | ADR-0026→0032 present + accurate; WP6-gate notes appended |

## WS-B — Single-user lifecycle (demo accounts only; no execution; no production mutation)

Certifies: create → test-connection → validation → history → status update → replace-credentials →
invalidation → revalidation → disconnect → tombstone → history preservation.

| Module | Tests | Result |
|--------|------:|--------|
| `trading.tests_broker_connectivity` | 16 | OK |
| `trading.tests_tb3_validation_state` | 9 | OK |
| `trading.tests_credential_invalidation` | 10 | OK |
| `trading.tests_credential_destruction` | 7 | OK |
| `terminal_provisioning.tests_broker_login_validation` | 12 | OK |
| `terminal_provisioning.tests_validate_login_agent` | 40 | OK |
| **WS-B total** | **94** | **OK** |

Verdict **PASS** for the engineered lifecycle. **Note (HOST-VERIFIED, blocking condition):** a *live* demo
`VALIDATE_LOGIN` has **not** succeeded on the host — ADR-0027 records Phase 2 as not host-certified, and the
first live demo attempt failed at `isolation_check_failed` (a validation-terminal ACL gap). The **code** is
certified; the **live host broker-login** is a pilot condition (see recommendation).

## WS-C — Execution safety (evidence only; no race/concurrency/stress)

| Module | Tests | Result |
|--------|------:|--------|
| `execution.tests_broker_gate` (creation gate) | 19 | OK |
| `execution.tests_dispatch_gate` (claim + final-dispatch) | 14 | OK |
| `execution.tests_wse` (Workstream-E closure) | 11 | OK |
| `execution.tests_execution_entrypoints` (route inventory drift guard) | 5 | OK |
| `execution.tests_runtime_pause` | 16 | OK |
| `execution.tests_runtime_resume` (no auto-resume) | 17 | OK |
| `execution.tests_e3_demo_promotion` (test-order + promotion) | 18 | OK |
| **WS-C total** | **100** | **OK** |

Authoritative route inventory (`execution_entrypoints.json`) certified via the drift guard: no
UNKNOWN/FIX_REQUIRED; all 15 exposure-opening routes gated at creation + claim + dispatch. Verdict **PASS**.
Concurrency/race certification of these gates is **DEFERRED-WP6B**.

## WS-D — Health engine

`reliability.tests_broker_health` — **59 OK**: UNKNOWN / HEALTHY / DEGRADED / STALE / DISCONNECTED /
TOMBSTONED transitions, resume eligibility, pause generation, monotonic `state_version`, no automatic
resume. Verdict **PASS**. Scheduler load/stress is **DEFERRED-WP6B**.

## WS-E — Operational events

| Module | Tests | Result |
|--------|------:|--------|
| `operational_events.tests_operational_events` (timeline/summary/visibility/dedup/categories/pagination) | 41 | OK |
| `operational_events.tests_broker_projection` (projection/on_commit/metadata) | 33 | OK |
| **WS-E total** | **74** | **OK** |

Verdict **PASS**. No projection rebuild/recovery attempted (there is no rebuild tool — recovery is disable +
re-accrete forward).

## WS-F — Operations UI

Frontend vitest (Operations & Support surface) — **46 OK across 10 files**: operator access + owner denial +
flag-OFF (404, no nav, **zero API calls**) + flag-ON operator gate (non-operator → "Restricted", no API
call) + badges + filters + timeline + detail (no internal-field leak) + search + pagination + a11y smoke
(keyboard/ARIA in timeline + dialog). Verdict **PASS**. Customer exposure: none (operator-only, read-only,
flag OFF by default).

## WS-G — Support workflow validation

Every documented support workflow (the 17 in [support-playbook.md](support-playbook.md)) is certified via
the behaviour tests above — expected state / event / audit / wording per workflow:

| Workflow | Certified by |
|----------|-------------|
| cannot add account · connection-test-fails · technical-unavailable · invalid-credentials · live-detected | WS-B (`broker_connectivity`, `broker_login_validation`, `classification_crosscheck`) |
| account-disconnected · credential-replaced · credential-removal · delete-account | WS-B (`credential_invalidation`, `credential_destruction`) |
| health-degraded · health-stale | WS-D (`broker_health`) |
| runtime-paused · controlled-resume | WS-C (`runtime_pause`, `runtime_resume`) |
| execution-refused | WS-C (`broker_gate`) |
| timeline-empty · operator-not-customer · duplicate/missing-event | WS-E/F (`operational_events`, `broker_projection`, operations UI) |

Verdict **PASS** (behaviour). Manual operator walk-through against live demo accounts is **HOST-VERIFIED**
(requires the disposable/demo host environment).

## WS-H — Rollback readiness (verify only; no rollback executed)

| Rollback path | Verdict | Evidence |
|---------------|---------|----------|
| Agent rollback | PASS (defined) | `wp6-rollback-rehearsal.md` RBK-4; re-stage bundle, fail-closed protocol |
| Backend rollback | PASS (defined) | prior image tag; additive migrations safe (`rollback-matrix.md`) |
| Validation image rollback | PASS (defined) | 5833→6073 directory/config swap (`validation_image_manifest.json` rollback_identifier) |
| Feature-flag rollback | PASS | flag-OFF restores DARK instantly (backend) / DARK redeploy (frontend) |
| Database backup | HOST-VERIFIED | `OPERATIONS_DASHBOARD.md` §6 records **no automated backup deployed** — a pilot condition |
| Rollback evidence | PASS (defined) | `wp6-evidence.json`, `evidence/schema/evidence-manifest.schema.json` |

Verdict **PASS (readiness defined)**; **no rollback executed**. The absent automated DB backup is an
Internal-Pilot condition.

## WS-I — Monitoring verification (specs implementable; no baselining)

`reliability.tests_operations_summary` — **33 OK**. Every monitoring signal in
[monitoring-spec.md](monitoring-spec.md) has an owner + source + query + expected evidence.
**CAPACITY BASELINES DEFERRED TO WP6B** — no thresholds invented; event-lag + operator-API-error signals do
not yet exist and must be ADDED. Verdict **PASS (specs implementable)**.

---

## Executed-evidence summary

| Area | Backend tests | Frontend tests |
|------|--------------:|---------------:|
| A env | 27 | — |
| B lifecycle | 94 | — |
| C execution | 100 | — |
| D health | 59 | — |
| E events | 74 | — |
| F UI | — | 46 |
| I monitoring | 33 | — |
| **Total** | **387** | **46** |

**All 433 executed tests passed. Full `make check` green.** No live accounts, no production mutation, no
service stops, no failure injection, no flag enablement — consistent with the WP6A hard boundary.
