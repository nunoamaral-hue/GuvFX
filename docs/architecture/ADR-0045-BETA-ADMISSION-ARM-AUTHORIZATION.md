# ADR-0045 — Beta-admission-derived arm authorization + the hosted autonomous-cycle scheduler wire

- **Status:** PROPOSED (Amber decision — touches the CONTAIN-1 arm authorization boundary). Ships **DARK**
  (`BETA_ADMISSION_ARM_ENABLED` default OFF). Awaiting Sponsor ratification before the flag is ever set.
  *(Notion owns the approved-ADR lifecycle; git status may lag.)*
- **Date:** 2026-08-14 · **Programme:** Beta Launch Critical Path — First Supervised Beta User.
- **Relates to / amends:** CONTAIN-1 (Sponsor 2026-08-05 arm cohort gate), ADR-0021 (no-admission arm note),
  ADR-0033/0034 (persistent workspace + onboarding/execution), ADR-0044 (supervised single-tenant beta).

## Context

The Sponsor (2026-08-14) made the **first supervised beta user** — not Customer Zero — the acceptance
subject, with one milestone: *a brand-new beta user completes the hosted Provider-B journey autonomously,
with no engineer intervention once the journey starts*. Any step that still requires an operator mid-journey
is, by the Sponsor's rule, a **Beta Blocker** to be fixed.

A four-trace repository investigation of the fresh-user journey found the front and middle are autonomous in
code (registration, email verification, workspace creation, node allocation, the observation state machine,
autonomous arming — the last two shipped under ADR-0044), and that only the physical host/broker boundary and
operator flag flips remain. It surfaced **two genuine repository Beta Blockers**:

1. **The autonomous cycle had no deployable scheduler.** `run_hosted_observations` (allocate node → advance
   observation state machine → auto-arm, all in `run_cycle`) shipped with ADR-0044 but no cron/systemd/compose
   artefact exists to run it — so a self-requested workspace sits at `PROVISIONING` forever and an engineer
   must run the command by hand. (Contrast the wired `deploy/monitor-scheduler` and `deploy/soak-report`
   crons; the docs even refer to a "`run_hosted_observations` cron cycle" that did not exist.)
2. **Arm authorization required a redundant SECOND per-user operator action.** The self-serve "Enable Trading"
   endpoint (`strategies.views.signal_copy_arm`) is gated by `_arm_cohort_approved`, which under CONTAIN-1
   authorizes ONLY identities on the dedicated `INTERNAL_PILOT_ARM_APPROVED_EMAILS` allowlist (default empty,
   deny-all, no admission bypass). So a beta user who has *already* passed the `BetaTester` admission gate is
   still refused `403 not_pilot_approved` at Enable-Trading until an operator hand-adds their email to a
   second allowlist — a per-user engineer intervention mid-journey.

## Decision

### 1. Ship the hosted-cycle scheduler artefact (Fix #1)

Add `deploy/hosted-observation-scheduler/` (crontab + idempotent installer + README), mirroring
`deploy/monitor-scheduler` exactly (end-anchored marker, provisioned writable log + logrotate). It schedules
`python manage.py run_hosted_observations` every minute. **DARK:** the command is a dormant no-op unless
`HOSTED_OBSERVATION_SCHEDULER_ENABLED` is on (it self-gates at `handle()`), is a Postgres-advisory-lock
singleton, and every step is idempotent — installing the cron changes nothing until an operator flips the
flag. This is a deployment artefact (repository deliverable); installing it on a host is a separate
operational step.

### 2. Add beta-admission as a SECOND, additive arm-authorization source (Fix #2)

Introduce `BETA_ADMISSION_ARM_ENABLED` (default OFF) and extend `_arm_cohort_approved(user)` with a second,
independent authorization source, evaluated in order (either grants; neither ⇒ deny):

1. the existing `INTERNAL_PILOT_ARM_APPROVED_EMAILS` allowlist — **unchanged and still authoritative**; and
2. (new) when `BETA_ADMISSION_ARM_ENABLED` is on, an **admitted ACTIVE `BetaTester` who is NOT Customer Zero**
   is arm-authorized directly (`_admitted_beta_arm_authorized`). This unifies the operator's per-user touch:
   admitting a beta user once (`admit_beta_tester`) carries the whole autonomous journey, with no separate
   arm-allowlist entry.

**Customer Zero is excluded by construction.** `_admitted_beta_arm_authorized` denies any user who owns a
reserved Customer-Zero account, using the ONE canonical definition
(`hosted_workspace.tenant_isolation.customer_zero_account_ids` = `applocker_policy.RESERVED_CUSTOMER_ZERO`,
reused so the identity never diverges — security RULE 6). So even though Customer Zero is itself an admitted
`BetaTester`, it is never implicitly arm-authorized — preserving the original CONTAIN-1 guarantee and the
Sponsor's 2026-08-14 direction that Customer Zero remain a protected production account, never restored onto
the self-serve arm path.

## Invariants (permanent while the flag can be on)

- **DARK by default / byte-identical when off.** With `BETA_ADMISSION_ARM_ENABLED` OFF, `_arm_cohort_approved`
  is byte-identical to the pre-ADR-0045 email-allowlist-only gate.
- **Additive, never subtractive.** The email allowlist is never weakened; admission is only an *additional*
  grant. Neither source bypasses the downstream technical gates (demo+active, Provider-B readiness /
  runtime-ready, broker-health, single-tenant routing, ExecutionJob creation + final-dispatch gates). Arming
  creates AUTHORITY only; the global execution levers remain the order authority.
- **Fail-closed.** Any error, missing relation, empty email, inactive/absent `BetaTester`, or Customer-Zero
  ownership ⇒ deny. No staff bypass is introduced.
- **Customer Zero never re-authorized.** The exclusion is by the canonical account definition, not by email
  string, so it holds regardless of CZ's `BetaTester` status.

## Consequences

- **Reversibility:** instant — turn `BETA_ADMISSION_ARM_ENABLED` off (authorization reverts to email-allowlist
  only). No schema migration.
- **Scope of authorization relaxation:** exactly "an admitted active non-CZ `BetaTester` may self-serve arm
  their OWN demo account when the flag is on" — the closed-beta admission gate (operator-controlled) becomes
  the single per-user control point instead of two. It does NOT widen who is admitted, nor what a non-admitted
  user can do.
- **Not obsoleting containment:** the CONTAIN-1 boundary still holds; this ADR changes its *source of truth*
  for the beta cohort (admission) under an explicit flag, not its fail-closed nature.

## Why this is an approved decision (not self-accepted)

Fix #2 changes the SOURCE of an authorization boundary (CONTAIN-1), which under `.claude/rules/architecture.md`
("no silent architecture replacement … auth … requires an approved decision") and `.claude/rules/security.md`
(least privilege; separate research/paper/production permissions) is an **Amber** change. It therefore ships
DARK and is recorded here for Sponsor ratification before the flag is enabled on any environment. Fix #1 is a
purely additive deployment artefact for an already-DARK command and carries no authorization change.

## Verification

- `strategies.tests_arm_containment.ArmAdmissionAuthorizationTests` — DARK-by-default; admitted active non-CZ
  tester arms when on; non-admitted / inactive-tester / Customer-Zero all refused even when on; technical
  gates + email allowlist untouched.
- The scheduler artefact is exercised implicitly by the end-to-end autonomous-journey test
  (`hosted_workspace.tests_beta_journey_e2e`) via `run_cycle` / `run_hosted_auto_arm`.
