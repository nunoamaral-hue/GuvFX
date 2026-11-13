# ADR-0103 — Automatic monitoring enrolment: a generic idempotent reconciler + a truthful dashboard enrolment projection

- **Status:** Accepted (repository-only; additive; the reconciler's route-mutation is DARK / default-OFF; the
  read-only dashboard projection is default-safe — no migration, no model, no new reason vocabulary).
- **Packet:** GFX-DASHBOARD-AUTO-MONITORING-ENROLMENT-087.
- **Date:** 2026-11-13.
- **Relates to / composes (never replaces):** ADR-0075 (per-host monitoring gateway + `MonitoringRoute`
  lifecycle + readiness contract — the primitives this reconciler drives), ADR-0100 (truthful dashboard
  live-position reason + equity-curve coverage — the taxonomy this extends to the whole-account level),
  ADR-0072/ADR-0033 (`evaluate_monitoring` eligibility + the LIVE positions demo gate — consumed, not
  changed), the `analytics.portfolio` honesty invariants (null-never-zero, per-tenant isolation).

## Context — the proven defect

A newly broker-connected MT5 account does not automatically become a complete dashboard data source. A
read-only diagnosis (code-level, consistent with the prior ACCOUNT_46 and ADR-0075 diagnoses) established a
single structural cause and one by-design secondary limitation:

1. **One record gates all three ingest paths — the per-tenant `HostedExecutionEndpoint` in `state=READY`
   with a non-empty `base_url`.** The closed-deal ingest (`monitored_ingest.monitored_account_ids`) *filters*
   `state=READY`, so a connected account without a READY endpoint is never enqueued; the equity-snapshot read
   fails closed (`ST_ENDPOINT_NOT_READY` / `ST_HOSTED_NO_ENDPOINT`) so no snapshot is written and the account
   drops out of the equity-curve `present` count; and the live-positions read returns `endpoint_unavailable`.
   The account is still counted in the universe (`owned_accounts` filters only `disconnected_at IS NULL`) — so
   it reads as "counted but not covered". **There is no account-ID allowlist, static route table or env gate
   anywhere in the dashboard universe** (verified): the gap is a missing lifecycle wire, not a hard-coded set.

2. **Nothing creates the enrolment records automatically.** The ADR-0075 foundation already built the
   enrolment engine (`monitoring_onboarding.reserve_route_for_workspace`, the `monitoring_allocation`
   lifecycle), but it was deliberately left unwired (zero production callers) to preserve the certified
   single-writer state machine; and the route-reconcile command had no scheduler. ADR-0075 itself deferred
   exactly this: "wiring the readiness contract into the dashboard UI and the onboarding writer."

3. **The dashboard could not say WHY.** "unavailable" collapsed into something indistinguishable from "zero"
   at the account level; ADR-0100 fixed this for positions, but not for the whole account.

4. **A LIVE terminal's `/mt5/positions` is demo-gated (`account_not_demo`, ADR-0072).** This is by design and
   OUT OF SCOPE here — a LIVE account still monitors balance/equity/deals; only its live positions are gated.
   This ADR does **not** lift that gate.

## Decision

Two additive layers, separated by what each is allowed to touch. Neither invents a new onboarding system, a new
model, a migration, or a new reason vocabulary.

### Layer 1 — the read-only enrolment PROJECTION (production-active, default-safe)

`execution.monitoring_enrolment.evaluate_account_enrolment` / `evaluate_enrolment_for_accounts`: a pure,
read-only function that projects each account to a stable **stage** + **reason code** + safe **UI copy** +
`monitored`/`structural` flags, derived only from durable, production-ACTIVE records (the endpoint state, the
`HostedMt5Workspace` canonical projection, the latest `AccountEquitySnapshot` freshness) — plus the durable
`MonitoringRoute` readiness *when the ADR-0075 gateway is enabled*. It makes **no host contact and no write**,
and it **never changes which accounts are in scope** — it annotates them. Stages are exactly the packet's
vocabulary: `READY, PROVISIONING, LOGIN_REQUIRED, ENDPOINT_MISSING, UNREACHABLE, IDENTITY_MISMATCH,
SNAPSHOT_STALE, STRUCTURALLY_EXCLUDED, TEMPORARILY_UNAVAILABLE, UNKNOWN`. Only `READY` counts toward coverage;
every other stage is surfaced with its cause and never rendered as a measured zero. A stage that will not
self-heal by waiting (login-required, identity-mismatch, structural) is flagged so the UI never implies
"reconnecting". `account_not_demo` never demotes an account below `READY` (equity/deals still flow) — the
positions limitation is carried as a per-capability note.

The dashboard consumes this: the summary carries a per-account `monitoring_stage`/`monitoring_ui_state`/
`monitored` plus a `monitoring` coverage rollup (`monitored N of M` + per-stage breakdown); the open-trades
endpoint enriches each unavailable account with its enrolment stage and the same coverage block; the UI renders
"Monitoring N of M accounts . 2 setting up monitoring . 1 login required" instead of an unexplained partial.
This is additive and default-safe (ADR-0100 precedent: no flag) — safe when the ADR-0075 foundation is dark
(there are then no routes; the stage derives from the active endpoint/workspace/snapshot records).

### Layer 2 — the idempotent RECONCILER (dry-run by default; apply is two-level DARK)

`execution.monitoring_enrolment.reconcile_enrolment` + the governed `reconcile_monitoring_enrolment` command
(dry-run by default, `--apply`, `--account`, `--owner`, deterministic redacted JSON). It selects the same
canonical universe (`disconnected_at IS NULL`, owner-scopable), projects every account's stage, and — with
`--apply` AND both `MONITORING_GATEWAY_ENABLED` and `MONITORING_ENROLMENT_RECONCILE_ENABLED` on (two-level
darkness, like `reconcile_monitoring_routes`) — drives `reserve_route_for_workspace` to idempotently reserve +
register a deterministic `MonitoringRoute` from each account's existing endpoint, and raises governed
`reliability.AlertEvent`s for accounts stuck in a non-ready stage. It writes only `MonitoringRoute`/audit +
alert rows. It **never marks a route READY** (that needs the gateway's live identity probe — a separate
Sponsor-gated deploy), contacts a host, mutates a Tailscale policy / bridge / account / workspace, places an
order, or touches Copy Trading. A new `deploy/monitoring-enrolment-scheduler/` cron installs the apply pass —
default-OFF, inert until both flags are flipped.

## Why this shape

- **Extend, don't duplicate.** The selection loop drives the existing ADR-0075 primitives; it adds no new
  allocator, route model, health enum or onboarding path (`.claude/rules/architecture`).
- **Truthful now, automation dark.** Presentation becomes honest in production immediately (Layer 1 is
  read-only and production-active); the record-writing automation ships gated from its first line.
- **No host contact, fail-closed, secret-free.** Logins/account numbers never appear in a stage, reason code
  or alert. Every ambiguity degrades to a truthful `UNKNOWN`/`PROVISIONING`, never a fabricated `READY`.
- **Bounded cost.** The projection prefetches endpoints/workspaces/routes/snapshots in a fixed, small number
  of queries regardless of account count (no N+1).

## Consequences

- A newly connected account is discovered and (once the apply flags are on) enrolled by one idempotent pass —
  no bespoke packet, no manual allowlist, no per-metric registration. Partial coverage is always explained by
  stage. Failures are actionable (alerts) not silent.
- Invariants preserved: never fabricate a figure; never substitute zero for unavailable; never shrink the
  account universe; never cross tenants; never imply a structural exclusion will reconnect; the LIVE positions
  demo gate is unchanged; no migration, no model, no new network behaviour, no Copy-Trading / execution change.
- Deferred (separate Sponsor-gated steps): marking routes READY + live identity verification via the ADR-0075
  gateway process; the production backfill `--apply` run; flipping the gateway/enrolment flags.

## Evidence

`evidence/manifests/GFX-EVD-DASHBOARD-AUTO-MONITORING-ENROLMENT-087.json`. Backend: `execution` suite (1620)
OK incl. the new `tests_monitoring_enrolment` (31), `analytics` suite (272) OK (no regression), `check` clean,
`makemigrations --check` no drift. Frontend: `OpenTradesPanel.test.tsx` (26) OK incl. the two ADR-0103 coverage
cases, `npm run build` OK, eslint clean on changed files. Synthetic fixtures only; no PII, no host contact.
