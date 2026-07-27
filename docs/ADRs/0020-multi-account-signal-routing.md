# ADR-0020 — Multi-account signal routing (one Telegram source → N isolated customer accounts)

- **Status:** **ACCEPTED FOR REPOSITORY IMPLEMENTATION — Sponsor (Nuno), 2026-07-27.** Surfaced during
  the Trusted Beta Fast-Track TB-1 implementation as a **contradiction with the accepted single-tenant
  architecture** (the trigger the Sponsor named for requiring an ADR). Repository build authorised behind
  the default-OFF `MULTI_ACCOUNT_ROUTING_ENABLED` flag under the controls below. **Final operational
  ratification (apply the migration to the beta VPS, enable the flag, activate additional beta accounts,
  allow any order through the fan-out path) remains a separate Class-B Sponsor gate** — none of those are
  authorised by this ADR.
  - *Sponsor controls:* flag default OFF; no deploy/APPLY/enable/prod-migration in this packet; prove
    Nuno's AUTO_DEMO path behaviourally unchanged with the flag OFF; migration preserves all existing
    `SignalExecutionPlan` records without loss/duplication/reinterpretation; DB uniqueness invariant =
    one plan per (source approval, destination account); idempotency proven under duplicate delivery /
    router retry / worker retry / concurrent fan-out / partial destination failure / reconciliation
    restart; two-account isolation proven; migration forward + rollback + data-preservation tests +
    focused/integration/mutation/adversarial-review/make-check/CI; merge only when all green.
- **Date:** 2026-07-27 · **Programme:** Trusted Beta Fast-Track (shared-VPS beta for 3–5 users).
- **Relates to:** ADR-0018 (certified two-plane architecture), the auto-router
  (`backend/execution/auto_router.py`), `SignalExecutionPlan` (`backend/execution/models.py`),
  `.claude/rules/architecture.md` ("no silent architecture replacement — requires an approved decision
  before merge").

## Context
The certified execution plane is **single-tenant per signal source by design**:
- `auto_router._resolve_target` resolves a source to the **UNIQUE** routable assignment and fail-closes
  to MANUAL on ambiguity (`auto_router.py:96-140`).
- `SignalExecutionPlan.approval` is a **`OneToOneField`** ("One plan per approval — hard idempotency",
  `models.py:720-724`) and there is a **`UniqueConstraint(source, chat_id, message_id)`**
  (`models.py:762-766`). So one signal ⇒ one plan ⇒ one account.

The Trusted Beta objective requires **one shared Telegram source (`ti_signals`) to fan out to N
independent customer accounts**, each with its own isolated runtime, plan, jobs, sizing, and
suspension. With the current model, binding a second account to `ti_signals` makes `_resolve_target`
ambiguous → returns `None` → the source fail-closes to MANUAL and **auto-copy stops for everyone,
including Nuno's live account**. This is fail-*safe* (no wrong orders) but blocks the beta.

`ExecutionJob` is already per-account (no plan FK; carries `account` + `terminal_node`), so the plan
layer is the only single-tenant chokepoint. Reusing the certified planning → risk → promotion pipeline
for N accounts therefore requires **N plans per signal — one per `(approval, account)`**, which both
constraints above forbid.

## Options
**A — Per-account plan model, feature-flagged (proposed).** Relax the plan to one-per-`(approval,
account)`:
1. `SignalExecutionPlan.approval`: `OneToOneField` → `ForeignKey` (keep `related_name="execution_plan"`).
2. `UniqueConstraint(source, chat_id, message_id)` → `(source, chat_id, message_id, account)`.
3. Plan idempotency in `plan_demo_execution` keyed on `(approval, account)` (two sites: `signal_planning.py:190,318`).
4. `auto_router` gains a **fan-out** path: resolve ALL routable assignments bound to the source and
   run approve-once → plan+promote **per account**, each isolated (one destination's failure never
   blocks another; per-account deferral audit; per-account idempotency; per-customer suspension honoured).
5. **Everything behind a new default-OFF flag `MULTI_ACCOUNT_ROUTING_ENABLED`.** Flag OFF ⇒ the
   resolver, planning, and router take the **existing single-tenant path, byte-identical** — same single
   destination, same plan/job shape, no extra account lookup, no new failure mode. Nuno's live route is
   unchanged with the flag off.
6. Downstream reverse-relation count queries adjusted for multi-plan semantics: the one `isnull=False`
   *count* (`operations_summary.py`) gains `.distinct()` so a fan-out approval with >1 plan is not
   miscounted. The `isnull=True` queries (`execution_health.py` unplanned scan, `operations_summary.py`
   unplanned/in-flight) are LEFT-JOIN-null and do not duplicate, so they are correct unchanged.

**B — Clone jobs to extra accounts (rejected).** Keep the single plan and copy the resulting
`PLACE_ORDER` jobs to accounts B…N. Rejected: bypasses the per-account risk gates, per-account plan
audit, per-account idempotency and per-account sizing — it would place orders that never passed a plan,
weakening the certified safety pipeline.

**C — Separate per-account execution record (rejected).** A new model outside `SignalExecutionPlan`.
Rejected: loses the certified planning/risk/promotion pipeline; duplicates a large, safety-critical
surface.

## Decision (proposed)
Adopt **Option A**. It is the smallest change that reuses the certified pipeline unchanged per account,
keeps Nuno's live single-tenant route byte-identical with the flag off, and confines multi-tenancy to a
default-OFF flag whose activation is a Class-B Sponsor gate.

## Consequences
- **Schema migration on the certified plane** (OneToOne→FK + constraint + `account` column already
  present). Even with the flag off the schema/invariant changes — so the migration itself is a
  certified-plane change requiring this approved decision, and running it on prod is a **Class-B gate**.
- Idempotency semantics move from "one plan per signal" to "one plan per signal **per account**" — the
  dedup identity gains the account dimension. The `(source, chat, message, account)` constraint keeps
  duplicate-signal idempotency intact per destination.
- Ops-metric queries counting "planned vs unplanned messages" change meaning under fan-out and are
  adjusted so a multi-destination message is not miscounted.
- **Reversibility:** the flag makes behaviour reversible instantly; the schema is reversible (FK→OneToOne)
  only while no signal has produced >1 plan for one approval (i.e. before the flag is ever enabled in
  prod), which the Class-B gate controls.
- **Idempotency scope (accurate per RULE 7):** fan-out idempotency is enforced at the PLAN layer by the
  `uniq_plan_approval_account` DB constraint — no duplicate plan for a `(approval, account)` is possible
  even under true concurrency, and the planner short-circuits to the existing plan. Duplicate-delivery /
  router-retry / reconciliation-restart idempotency is proven end-to-end (sequential). The residual
  job-layer window — two *genuinely simultaneous* promotions of the same PLANNED plan could create
  duplicate `PLACE_ORDER` jobs because the leg→job link has no DB uniqueness — is **pre-existing certified
  single-tenant behaviour that TB-1 neither introduces nor worsens** (sequential intake +
  `AcquiredMessage` unique(provider, message_id) make it unreachable in practice). Adding a DB uniqueness
  on the leg→job relation is tracked as a separate follow-up on the certified promotion path.

## Why this is a Sponsor gate (not self-accepted)
It relaxes a **load-bearing idempotency invariant on the certified execution plane** that trades live
money, and prior plan/idempotency changes have caused real incidents (the TI non-execution incident).
Under `.claude/rules/architecture.md` an execution-path/storage-layout change requires an **approved
decision before merge**. The build (behind the default-OFF flag, not deployed, Nuno unchanged) is the
autonomous repo work; **merging the plan-model migration and, separately, enabling the flag / running the
migration on the beta VPS are Sponsor gates.**

## Acceptance asks (for the Sponsor)
1. Accept **Option A** (feature-flagged per-account plan model) — or steer to a different design.
2. Confirm the plan-model migration may **merge to `main`** behind the default-OFF flag (no deploy, no
   enable), with the full Nuno-unchanged regression proof; OR require the ADR accepted first and the
   plan-model change held while the non-plan-model beta increments proceed.
