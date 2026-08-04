# Rollback Matrix — Broker Connectivity (WP5.4 Workstream E)

Rollback for **every partial-arming state**. Machine-readable copy:
[`readiness-checklist.json`](readiness-checklist.json) → `partial_arming_states`.

**Guiding rule (from the merged design):** prefer **disabling a flag** over any destructive database
rollback. All six flags support instant DARK rollback for the DARK→armed direction — backend flags are read
live (set OFF, no restart); frontend flags require **redeploying the DARK image** built with the flag unset.
The operational-event read model is a **rebuildable cache**; the `VALIDATE_LOGIN` probe is **side-effect-free**
(`shutdown()` in `finally`); the execution gate and health engine are **additive and transparent when OFF**.
**No destructive database rollback is required for any of these states.** Where a schema reversal is
theoretically available (health table `migrate reliability 0003`) it is optional and only for defect
cleanup, never for disarming.

Legend — **New exposure possible?** = could a *new order* be opened in this state.

---

## Matrix

### 1. Customer frontend only
- **State:** `NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED` ON, backend `BROKER_CONNECTIVITY_ENABLED` OFF.
- **Customer impact:** Broker Accounts UI renders but every API call returns 404.
- **Trading impact:** none. **New exposure possible:** no.
- **Safest immediate action:** either redeploy the DARK frontend **or** enable the backend flag to match —
  do not leave the surface mismatched (a UI that only 404s).
- **Flag rollback:** redeploy frontend built with `NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED` unset.
- **Image rollback:** yes (frontend, build-time flag). **DB rollback:** none.
- **Evidence to preserve:** the deployed frontend build's flag state; screenshots of the 404 behaviour.
- **Post-rollback checks:** `/broker-accounts` returns 404, no nav entry.

### 2. Customer backend only
- **State:** `BROKER_CONNECTIVITY_ENABLED` ON, frontend flag OFF.
- **Customer impact:** none (no UI entry); the API is reachable only to an authenticated direct caller.
- **Trading impact:** none — validation/onboarding place no orders. **New exposure possible:** no.
- **Safest immediate action:** acceptable transient during staged arming (stage 4). Complete stage 4 or
  disarm.
- **Flag rollback:** set `BROKER_CONNECTIVITY_ENABLED` OFF → surface returns to 404 immediately.
- **DB rollback:** none. **Evidence:** flag state, any validation-attempt rows (secret-free).
- **Post-rollback checks:** WP1A endpoints return 404.

### 3. Operational events only
- **State:** `OPERATIONS_EVENTS_ENABLED` ON, `NEXT_PUBLIC_OPERATIONS_ENABLED` OFF.
- **Customer impact:** none. **Trading impact:** none (projection-only, fail-open). **New exposure:** no.
- **Safest immediate action:** acceptable — recording without a UI is safe. Watch logger
  `guvfx.operational_events` for recorder failures.
- **Flag rollback:** set `OPERATIONS_EVENTS_ENABLED` OFF → recording + API return to DARK.
- **DB rollback:** none. A corrupt projection is cleared by disabling the flag; it re-accretes **forward** on
  re-arm. **No backfill tool exists** — truncation permanently drops history (authoritative state
  unaffected), so do not truncate expecting reconstruction.
- **Evidence:** recorder-failure log sample (should be empty), row counts.
- **Post-rollback checks:** API `/api/operations/account-events/` returns 404.

### 4. Operator frontend only
- **State:** `NEXT_PUBLIC_OPERATIONS_ENABLED` ON, backend `OPERATIONS_EVENTS_ENABLED` OFF.
- **Customer impact:** none (operator-only, admin-gated). **Trading impact:** none. **New exposure:** no.
- **Safest immediate action:** operator sees empty/404 data. Enable the backend flag or redeploy the DARK
  frontend.
- **Flag rollback:** redeploy frontend built with `NEXT_PUBLIC_OPERATIONS_ENABLED` unset.
- **DB rollback:** none. **Post-rollback checks:** `/operations/accounts` returns 404, no nav.

### 5. Operational timeline unavailable
- **State:** operational-event API/recorder failing while `OPERATIONS_EVENTS_ENABLED` ON.
- **Customer impact:** none (read model is a cache). **Trading impact:** none (projection-only, fail-open).
  **New exposure:** no.
- **Safest immediate action:** set `OPERATIONS_EVENTS_ENABLED` OFF; investigate `guvfx.operational_events`
  logs. Authoritative state (`AuditEvent`, WP1A/WP3/WP2 models) is unaffected.
- **Flag rollback:** set `OPERATIONS_EVENTS_ENABLED` OFF. **DB rollback:** none; the projection re-accretes
  **forward** on re-arm (no backfill tool — truncation loses historical rows).
- **Evidence:** recorder-failure log lines, projection row counts at the time of the incident.
- **Post-rollback checks:** authoritative state intact; API 404 after disable.

### 6. Health enabled without execution gate
- **State:** `BROKER_CONNECTIVITY_HEALTH_ENABLED` ON, `BROKER_CONNECTIVITY_EXECUTION_GATE` OFF.
- **Customer impact:** none. **Trading impact:** none — health emits signals only; **no pause/resume/dispatch
  enforcement without the gate.** **New exposure:** no.
- **Safest immediate action:** acceptable observation state. If health is misbehaving, disarm health.
- **Flag rollback:** set `BROKER_CONNECTIVITY_HEALTH_ENABLED` OFF (engine no-ops instantly).
- **DB rollback:** optional `migrate reliability 0003` (drops the additive `BrokerAccountHealth` table) —
  only for defect cleanup, never to disarm. **Post-rollback checks:** `get_contract` returns `None`.

### 7. Health state not converging
- **State:** `BrokerAccountHealth` not converging as expected (flapping, stuck, unexpected distribution).
- **Customer impact:** possibly incorrect eligibility signalling. **Trading impact:** **only if the gate is
  also ON.** **New exposure:** no (health alone cannot open exposure).
- **Safest immediate action:** if the gate is on, **disarm the gate first** (stop enforcement), then disarm
  health; investigate per-account with `get_contract` + validation-attempt history.
- **Flag rollback:** set `BROKER_CONNECTIVITY_EXECUTION_GATE` OFF, then `BROKER_CONNECTIVITY_HEALTH_ENABLED`
  OFF.
- **DB rollback:** none required. **Evidence:** `get_contract` snapshots, `state_version` series,
  validation-attempt rows.
- **Post-rollback checks:** gate transparent; health no-ops.

### 8. Execution gate enabled
- **State:** `BROKER_CONNECTIVITY_EXECUTION_GATE` ON (with or without health).
- **Customer impact:** ineligible accounts cannot execute. **Trading impact:** execution refused unless
  `VALIDATED` + eligible; existing eligible flows unchanged. **New exposure possible:** **yes** (this is the
  only state where the gate governs live order placement).
- **Safest immediate action:** monitor the refusal rate. If **eligible** accounts are wrongly refused,
  disarm the gate. If an **ineligible** account is permitted to execute, treat as **SEV-1**
  ([incident-response.md](incident-response.md)) and disarm the gate.
- **Flag rollback:** set `BROKER_CONNECTIVITY_EXECUTION_GATE` OFF → gate transparent instantly (short-circuits
  before any DB read).
- **DB rollback:** none. **Evidence:** refusal audit events, dispatch-gate decisions, any permitted-order
  reconciliation.
- **Post-rollback checks:** gate returns `gate_disabled`; existing execution behaviour restored.

### 9. Execution refusal spike
- **State:** elevated `EXECUTION_GATE_REFUSED` / `EXECUTION_DISPATCH_REFUSED` for **eligible** accounts.
- **Customer impact:** eligible customers cannot trade. **Trading impact:** missed legitimate execution.
  **New exposure:** no (refusals withhold orders).
- **Safest immediate action:** **disarm the execution gate immediately** (transparent when OFF); investigate
  eligibility/health inputs (validation status, health contract).
- **Flag rollback:** set `BROKER_CONNECTIVITY_EXECUTION_GATE` OFF.
- **DB rollback:** none. **Evidence:** refusal reason-code distribution, affected account eligibility states.
- **Post-rollback checks:** eligible accounts execute again.

### 10. Mixed frontend/backend build versions
- **State:** frontend and backend images at different build versions.
- **Customer impact:** potential UI/API contract mismatch. **Trading impact:** indirect. **New exposure:** no.
- **Safest immediate action:** redeploy to a single consistent version pair; treat as a deploy defect, not a
  flag issue.
- **Flag rollback:** redeploy the matched DARK image pair. **DB rollback:** none.
- **Post-rollback checks:** frontend/backend versions match; smoke test the customer + operator surfaces.

### 11. Provisioner protocol mismatch
- **State:** the agent bundle does not advertise the full `PROVISIONING_OPERATIONS` set.
- **Customer impact:** provisioning/validation refused **channel-wide** (fail-closed by design). **Trading
  impact:** none (no orders). **New exposure:** no.
- **Safest immediate action:** re-stage the correct agent bundle **before/with** the backend (ADR-0014
  ordering). This is a host action — **HOST-VERIFIED / OUTSIDE REPOSITORY CONTROL**.
- **Flag rollback:** N/A — re-stage the agent bundle; the backend `assert_compatible` will pass once the
  agent advertises the full set.
- **DB rollback:** none. **Evidence:** `NEGOTIATE` response, agent `manifest.json` `supported_operations`.
- **Post-rollback checks:** `NEGOTIATE` returns the full set; `assert_compatible` passes.

### 12. Agent unavailable
- **State:** the Windows agent is unreachable.
- **Customer impact:** validation returns **UNAVAILABLE (retryable)** — the customer is **not** asked to
  re-enter correct credentials. **Trading impact:** none. **New exposure:** no.
- **Safest immediate action:** restore the agent (host recovery — see `OPERATIONS_RUNBOOK.md` §4/§5).
  Validation stays fail-closed UNAVAILABLE meanwhile.
- **Flag rollback:** N/A (host recovery). Optionally disarm the customer journey (stage 4 rollback) to hide
  the UNAVAILABLE surface from customers.
- **DB rollback:** none. **Post-rollback checks:** validation returns HEALTHY/NEEDS_ATTENTION again.

### 13. Validation image failure
- **State:** the build-5833 validation image fails `verify_image` or the baseline is dirty.
- **Customer impact:** validation UNAVAILABLE (`validation_baseline_dirty` / `isolation_check_failed`).
  **Trading impact:** none. **New exposure:** no.
- **Safest immediate action:** rebuild the isolated image (`build_validation_image.ps1`) **or** revert to the
  **6073** rollback baseline (a directory/config swap; probe is side-effect-free). **HOST-VERIFIED.**
- **Flag rollback:** N/A — image swap. Optionally disarm stage 4 to hide the surface.
- **DB rollback:** none. **Evidence:** `verify_image` failure output, baseline `cleanup_status`.
- **Post-rollback checks:** `verify_image` PASS; a demo validation returns HEALTHY.

### 14. Migration failure
- **State:** a broker-connectivity migration fails to apply during stage 1.
- **Customer impact:** deploy blocked. **Trading impact:** none (flags OFF). **New exposure:** no.
- **Safest immediate action:** **halt arming**; redeploy the prior image; resolve the migration before
  proceeding. The three apps' migrations are additive (`trading 0012/0013/0014`, `operational_events 0001`,
  `reliability 0004`).
- **Flag rollback:** N/A — keep flags OFF; **do not arm on a failed migration.**
- **DB rollback:** only if a partially-applied migration must be reversed and it is **verified** safe;
  otherwise leave the DB as-is and roll the image back. Prefer keeping additive tables.
- **Evidence:** migration traceback, `showmigrations` output.
- **Post-rollback checks:** `makemigrations --check` clean on the restored image.

---

## What is NOT an available rollback (do not invent)

- There is **no dedicated code-revert rollback procedure** for the execution gate or health engine in the
  ADRs — because none is needed: disarming the flag restores prior behaviour (ADR-0029 §2, ADR-0030
  Consequences).
- There is **no destructive database rollback** in the DARK→armed direction for any flag. Reversing an
  additive migration is a defect-cleanup tool, not a disarm mechanism.
- The **6073 → 5833** validation-image change and its rollback are **host directory/config swaps**, not data
  restores; whether either is currently on the host is **HOST-VERIFIED / OUTSIDE REPOSITORY CONTROL** (ADR-0027
  records Phase 2 as not yet deployed/live-certified).
