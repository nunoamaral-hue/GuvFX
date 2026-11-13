# ADR-0104 — Monitoring stage accuracy: decouple the dashboard enrolment stage from the execution-confirmation ACK

- **Status:** Accepted (repository-only; additive; migration-free; corrects the ADR-0103 Layer-1 projection only;
  no flag change, no route/endpoint mutation, no host contact, Copy Trading untouched).
- **Packet:** GFX-DASHBOARD-MONITORING-STAGE-ACCURACY-FIX-095.
- **Date:** 2026-11-13.
- **Relates to / corrects (never replaces):** ADR-0103 (the auto-monitoring enrolment projection + reconciler —
  this corrects a single over-strict precedence rule in its Layer-1 projection and leaves Layer-2 and the whole
  architecture intact), ADR-0034 (`workspace_confirmed_at` — the customer-confirmation human ACK, a genuine
  **execution-arming** precondition, preserved in full), ADR-0072/ADR-0033 (`evaluate_monitoring` + the LIVE
  positions demo gate — consumed, not changed).

## Context — the proven defect (from the read-only production report, packet 094)

Packet 094 ran the read-only `reconcile_monitoring_enrolment report` against production. It established, from the
database alone (no host contact), that the ADR-0103 Layer-1 projection **mislabels** the support@ tenant's live
cohort:

- The projection short-circuited to `LOGIN_REQUIRED` whenever `TradingAccount.workspace_confirmed_at` was `None`,
  **before** consulting the endpoint/snapshot evidence.
- Every one of the 12 production support@ accounts has `workspace_confirmed_at = None`, so the projection reported
  **all 12 as `LOGIN_REQUIRED`, `monitored = 0`** — while 5 of them (shapes 46, 48, 49, 51, 52) have a `READY`
  endpoint and a **fresh equity snapshot** and the dashboard still shows their equity.
- The deployed "Monitoring N of M" line therefore read **"0 of 12 · 12 login required"**, contradicting the equity
  figures the same dashboard displays.

Root cause: `workspace_confirmed_at` is the **ADR-0034 execution-arming** human ACK. It has **never** gated the
monitoring data pipeline — `monitored_ingest.monitored_account_ids` (deals), the equity-snapshot collector, and the
positions read all key off the per-tenant `HostedExecutionEndpoint` being `READY`, not off confirmation. Folding the
confirmation gate into the monitoring-**stage** projection conflated two independent concerns.

The true per-account situation (production, from the report): **5 monitored** (READY endpoint + fresh equity),
**6 snapshot-stale** (READY endpoint, equity stale/never — the real operational blocker, incl. the #58 shape whose
equity snapshot is ~5.8 days old), **1 not-connected** (shape 59). #58's correct stage is `SNAPSHOT_STALE`, not
`LOGIN_REQUIRED` and not `ENDPOINT_MISSING`.

## Decision

1. **The monitoring stage uses monitoring evidence only.** `execution.monitoring_enrolment` no longer reads
   `workspace_confirmed_at`. Stage follows connection → identity → endpoint lifecycle → equity-snapshot freshness.
   `monitored` is defined as: **connected + identity-matched + `READY` endpoint + a fresh equity-bearing snapshot
   within the freshness window** — exactly the conditions under which dashboard data actually flows.

2. **The execution-confirmation ACK is preserved, untouched, everywhere it is an execution precondition.**
   `workspace_confirmed_at` continues to gate execution in `execution.readiness.evaluate`
   (→ `RW_NOT_CONFIRMED`), `execution.readiness.evaluate_monitoring` (the separate DARK D3 eligibility gate),
   `hosted_workspace.provisioning`, `hosted_workspace.capability_recovery` / `liveness_recovery` / `eligibility`,
   and `trading.managed_start`. None of these consult the dashboard projection; the fix cannot weaken them. A
   regression test asserts the SAME unconfirmed account is `READY`/monitored for the dashboard **and**
   `RW_NOT_CONFIRMED` (not execution-eligible) for arming.

3. **Dimensions are reported independently (truthfulness).** Each account's status now carries, in
   `detail['coverage']`, an independent per-dimension breakdown — `connection`, `endpoint`, `identity`, `equity`
   (`fresh`/`stale`/`never`), `positions`, `deals`, `route`. In particular **live positions are reported on their
   own axis**: a LIVE terminal's positions are `structurally_unavailable` (demo-gated, ADR-0072) **even when equity
   is fresh** — fresh equity never implies positions are available, and an unavailable position is never a
   fabricated zero. The monitoring summary (`N of M`) measures data monitoring only; it does not claim every metric
   is covered.

## Consequences

- The production support@ cohort reads **5 monitored · 6 snapshot stale · 1 login required** — truthful, and the
  dashboard line no longer contradicts the shown equity. The 6 snapshot-stale accounts now correctly surface the
  real operational gap (a per-account bridge producing stale data), which a **separate** infrastructure packet
  addresses — this change does not touch infrastructure, flags, routes or hosts.
- Additive and **migration-free**: no new model/field; one function split (public wrapper + internal stage machine)
  plus one pure per-dimension helper and two bounded prefetch reads (equity instant; deal existence). Layer-2
  reconciler, flags, and the ADR-0075 gateway are unchanged.
- `execution.readiness.evaluate_monitoring` (the DARK D3 gate that still requires confirmation) is **intentionally
  not modified** — it is a separate, execution-neutral eligibility concept not consumed by the dashboard, and
  out of this packet's scope.

## Rollout / rollback

Repository-only; nothing deployed by this packet. Rollback = revert the commit (migration-free, no DB rollback).
A later Sponsor-gated deploy of this corrected projection replaces the current deployed d800b78d projection; the
existing balance/equity/P&L/withdrawal figures are unchanged by it (additive stage/`monitored` relabelling only).

**Production unchanged. Auto-merge OFF. STOP before merge.**
