# ADR-0021 PR A — Governance Evidence

Branch `feat/adr-0021-permanent-onboarding`. This document is the governance evidence pack the Sponsor
required before Windows-host (PR B) work: fixture-sweep evidence, migration determinism/reversibility,
and the adversarial-review findings + resolutions.

## 1. Test-fixture sweep — evidence

The new `brokeridentity_present` CheckConstraint (every `TradingAccount` must carry a broker identity)
surfaced pre-existing test fixtures that created bare accounts (no `broker_server`, empty `broker_name`).
Those fixtures were corrected by adding `broker_name="DemoBroker"`.

Changed test files split into three provable categories (extractable from history by path):

- **45 MECHANICAL-fixture files** — the *only* change is an added `broker_name` kwarg (same-line or an
  adjacent added line). Verified: across all 45, **every added code line is a `broker_name` addition** and
  **no removed line is an assertion/`return`/`raise`** — the removed lines are exclusively the
  `create(...)` lines being modified in place (e.g. `terminal_node=node,` → `terminal_node=node,
  broker_name="DemoBroker",`, or `is_demo=True)` → `is_demo=True,` with a new `broker_name=...` line).
  No test logic or assertion was touched.
- **11 SEMANTIC-rewrite / new-test files** — assertions intentionally changed to reflect the **removed
  admission** behaviour, or brand-new test files for the new fail-closed/idempotency behaviour
  (§3 below). Each is justified against a real production change (never weakened to obtain green):
  - `trading/tests_beta_provisioning_wiring.py` — admission-gated wiring → dedicated-runtime-default for
    **any** customer; adds idempotency / concurrent / IntegrityError-recovery / missing-broker tests.
  - `trading/tests_beta_account_cap.py` — a test asserting a `SELECT … FOR UPDATE` on the user row (that
    lock was removed as the idempotency mechanism) → a test asserting duplicate submissions are idempotent
    (the *stronger* no-duplicate guarantee).
  - `trading/tests_brokers.py` — `test_missing_server_rejected` builds an **unsaved** instance (the
    constraint now forbids persisting a no-identity row); the validator assertion is unchanged.
  - `terminal_provisioning/tests_beta_activation.py` — "non-admitted denied" → "plain user allowed"
    (admission removed); helper sets `email_verified`+entitlement explicitly (admission no longer does).
  - `terminal_provisioning/tests_tb5_reconcile.py` — "skips non-admitted owner" → "reconciles any owner"
    (behaviour inverted by admission removal).
  - `terminal_provisioning/tests_account_runtime.py` — the provision-failure test now uses a **staff**
    user (non-staff now routes to the state-driven path, not the legacy provisioner the test targets).
  - `onboarding/tests_beta_admission.py` — admission no longer sets `email_verified`/entitlement; the
    allowlist model is retained-but-inert; docstring + two tests updated to pin that.
  - `billing/tests_beta.py` — `OnboardingGateTests` → non-staff `account_connected` is state-driven
    (blocked until the runtime is ready; the legacy flag does not drive it); staff legacy path unchanged.
  - `strategies/tests_tb2_arm.py` — `test_non_beta_user_forbidden` (403 `not_beta`) →
    `test_plain_user_can_arm_admission_removed` (200 `armed`) — the arm gate removed the admission check.
  - `terminal_provisioning/tests_provisioner_heartbeat.py` — **new** (Correction-1 busy-worker heartbeat).
  - `billing/tests_provisioner_health.py` — **new** (Correction-1 health-predicate + host-probe matrix).
- **15 NON-TEST (production) files changed** — exactly the intended PR-A set, no unrelated files:
  `billing/beta.py`, `onboarding/services.py`, `strategies/views.py`,
  `terminal_provisioning/{beta_activation,beta_worker,models,provisioner}.py`,
  `terminal_provisioning/management/commands/reconcile_beta_provisioning.py`,
  `terminal_provisioning/migrations/{0009,0010}`, `trading/{models,views}.py`,
  `trading/migrations/0013`, `docs/ADRs/0021-…md`,
  `frontend/src/components/onboarding/steps/AccountConnectionStep.tsx`.

Sponsor conditions confirmed: (a) mechanical test-data ✅; (b) no production assertion weakened solely to
get green ✅ (every assertion change maps to a real behaviour change); (c) no unrelated production
behaviour changed ✅; (d) separately identifiable ✅ (mechanical set is isolated to test files and is a
pure `broker_name`-only diff; the 11 semantic files are enumerated above).

Prod data was audited **clean** (authorised read-only preflight: 2 accounts, 0 incompatible rows,
account #11 compatible), so there is **no production remediation** — only test data.

## 2. Migrations — deterministic + reversible + no drift

Three new migrations: `trading/0013` (CheckConstraint `brokeridentity_present` + RunPython abort
pre-check), `terminal_provisioning/0009` (`ProvisionerHeartbeat` model),
`terminal_provisioning/0010` (partial-unique `uniq_active_job_per_runtime_op` + RunPython abort pre-check).

- **No drift:** `manage.py makemigrations --check --dry-run trading terminal_provisioning` → "No changes
  detected".
- **Reversible + deterministic (scratch-DB round-trip):** on a throwaway database — migrate FORWARD to
  head (OK) → reverse `terminal_provisioning 0010→0008` (unapplied 0010 then 0009, OK) → reverse
  `trading 0013→0012` (unapplied 0013, OK) → re-apply FORWARD (0009, 0010, 0013 re-applied, OK) →
  `migrate --check` reports no pending. Both RunPython pre-checks match their CHECK/partial-unique
  conditions exactly (no over-strict Trim/NULL divergence) and each reverse is `RunPython.noop`.

## 3. Adversarial review — findings + resolutions

A 5-dimension adversarial review (concurrency/idempotency, fail-closed health, migration safety,
frontend orchestration, estate safety) with independent verification produced 12 findings, **8
confirmed**, all resolved (commit `3f74966`):

| # | Sev | Area | Finding | Resolution |
|---|-----|------|---------|-----------|
| 1 | HIGH | health | Heartbeat failed **open** vs a hung agent: PROCESSING written before the up-to-20s negotiation, and a top-of-loop IDLE_READY erased the prior DEGRADED | PROCESSING only **after** negotiation succeeds; IDLE_READY only when genuinely idle; DEGRADED/ERROR persist. New test: repeated negotiation failure stays DEGRADED across loops |
| 2 | LOW | health | TTL parsed outside `try/except` (malformed env → 500) | Moved inside → fails closed |
| 3 | LOW | migration | `brokeridentity_present` pre-check stricter than the CHECK (Trim/NULL) → could spuriously abort | Aligned to `broker_server IS NULL AND broker_name=''` exactly |
| 4 | HIGH | frontend | RUNNING (status panel) is weaker than the backend readiness gate → misleading green "ready" + repeated complete-step POST | RUNNING shows a "finishing" progress message (not green); the real completion signal is the step advancing (backend-authoritative) |
| 5 | MED | frontend | `friendlyForState` mapped enum names the backend never emits | Mapped the actual `user_facing_state` vocabulary |
| 6 | LOW | frontend | Transient poll error regressed the UI to "waiting to start" | Only a real 404 regresses; transient errors keep the last state |
| 7 | LOW | frontend | "Check again" showed no in-flight feedback | `poll()` sets `checking` at start |
| 8 | MED | estate | Per-user cap downgraded to a non-atomic `count()` | Restored a row lock scoped **solely** to cap atomicity; idempotency stays lock-independent (constraint + IntegrityError winner recovery), proven by `test_recovers_winner_on_integrityerror` |

A **final code review of the fix commit** (verifying the 8 fixes themselves) confirmed fixes 1–6 and 8
correct and found one more issue, resolved in `dc65820`:

| 9 | LOW | frontend | `apiFetch` surfaces the raw JSON body as `err.message` (its bare `catch` re-throws `new Error(text)`), so `err.message === "not_found"` and the 409 reason-code mapping never matched | Added `reasonFromError()` to tolerate both the bare `detail` and the JSON-body shape; a central `apiFetch` change to always surface the bare `detail` is a documented follow-up |

Gates after fixes: backend **1974 tests OK** · frontend **lint 0 errors** · **build compiled
successfully** · migrations reversible · no drift · **PR #240 CI green**.
