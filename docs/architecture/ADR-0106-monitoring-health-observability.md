# ADR-0106 — Monitoring health observability: per-dimension freshness + an aggregate health rollup on the dynamic enrolment projection

- **Status:** Accepted (repository-only; additive; migration-free; observation-only; no execution/Copy-Trading
  authority; default-safe — no new flag or scheduler; no production mutation).
- **Packet:** GFX-DASHBOARD-AUTO-FRESHNESS-AND-ENROLMENT-099.
- **Date:** 2026-11-13.
- **Builds on (never replaces):** ADR-0103 (the auto-monitoring enrolment projection + dynamic reconciler),
  ADR-0105 (stage decoupled from the execution-confirmation ACK), ADR-0075 (monitoring gateway + `MonitoringRoute`),
  ADR-0072/0033 (LIVE positions demo gate). The existing dynamic collectors (`capture_equity_snapshots`,
  `ingest_monitored_deals`) and live-position reads are **unchanged**.

## Context — what the production diagnosis actually found

A read-only production diagnosis (backend `10f89d0d`, owner 29; no host contact — the packet's HARD STOP forbids
it) established that **the scalable automatic-monitoring mechanism already exists and runs**:

- The equity collector `capture_equity_snapshots` enumerates accounts **dynamically**
  (`TradingAccount.objects.filter(disconnected_at__isnull=True)` — no hard-coded ids, no static list), is scheduled
  (`*/5` cron), flag-ON in prod (`EQUITY_SNAPSHOT_LEDGER_ENABLED=1`), and reads via the existing identity-firewalled
  per-tenant attach. Deals are collected independently (`ingest_monitored_deals`, `*/15`), positions read live.
- A newly connected, identity-matched account with a serving bridge therefore enters dashboard monitoring
  **automatically** — no Claude packet, manual DB edit, manual route, or hard-coded id. The 5 fresh accounts
  (snapshots ~150 s old) prove the loop works.
- The 6 stale accounts (50, 53, 54, 56, 57, 58) have **READY endpoints and matched identity**, but their
  per-tenant bridges have not served a fresh account read for days (54/56/57 share ~7.0-day staleness — a shared
  host/bridge event). The `*/5` collector attempts all 12 every cycle, succeeds for 5, skips 6 (no usable read).
  This is an **operational/infrastructure** condition — the per-tenant terminals/bridges are not serving — **not a
  code gap**, and it is **out of scope for this repository packet** (a one-time bridge repair, listed separately).
- `#58`: READY endpoint, identity matched, last successful snapshot ~6 days old, 3 historical deals. Its terminal
  shows a live position, but the break is at **per-tenant bridge → backend reachability**: the bridge is not
  serving the account read, so the collector has written no fresh equity/position snapshot. Correct stage
  `SNAPSHOT_STALE` (ADR-0105). The classification is right; the data freshness is a separate infra repair.

`MonitoringRoute = 0` does **not** affect dashboard monitoring — routes belong to the ADR-0075 gateway subsystem,
which is DARK; dashboard data flows through the collectors above, not through routes.

The one genuine, code-addressable gap: **failed/skipped collection attempts are silent and there is no durable,
per-dimension, aggregate operational-health surface.** The dashboard could say an account is stale, but not give
operators an actionable per-dimension view (how fresh, stale by which dimension, how many recovery candidates).

## Decision

Add an **additive, observation-only observability layer** to the existing dynamic enrolment projection — not a
parallel monitoring subsystem (which the architecture rule forbids as speculative infrastructure):

1. **Per-dimension freshness age.** Each account's `detail['coverage']` gains `equity_age_s` (seconds since the
   latest equity-bearing observation, `None` when never observed) — so a stale dimension reads "stale for N
   minutes", never a fabricated zero, and recovery is observable.
2. **Aggregate health rollup.** `monitoring_health(statuses)` (pure, read-only) aggregates the independent
   per-dimension coverage into actionable operator metrics — `eligible` (connected + matched + endpoint-READY),
   `monitored`, `snapshot_stale`, `unreachable`, `identity_mismatch`, `endpoint_missing`, `provisioning`,
   `login_required`, `not_connected`, `structurally_excluded`, `recovery_candidates` (accounts that self-heal on
   the next successful bridge read), plus a `by_dimension` aggregate. `coverage_rollup` carries it under `health`,
   so the dashboard `monitoring` block and the read-only `reconcile_monitoring_enrolment report` surface it
   automatically.

Both are derived ONLY from the stage + the already-projected durable coverage; they enumerate nothing, contact no
host, mutate nothing, and grant **no** execution/trading/Copy-Trading/live authority. No new model, no migration,
no new flag or scheduler.

## Consequences

- Operators and the dashboard get a truthful, per-dimension, scale-free health view over the **dynamically
  enumerated** owner-scoped cohort, with explicit recovery-candidate counts — so a newly connected account appears,
  and a stale one is explained by dimension, with no hard-coded id and no manual step.
- Additive and **migration-free**; existing consumers (dashboard `monitored`/`total`/`by_stage`, the frontend
  panel) are unaffected. Execution/live-trading gates are byte-identical (only `monitoring_enrolment.py` changes).
- The 6 stale accounts' per-tenant bridges remain a **separate, one-time operational repair** (contact the hosts,
  outside this packet's HARD STOP). Once a bridge serves again, the existing `*/5` collector recovers the account
  automatically and `monitoring_health.recovery_candidates` decrements — no code change.

## Rollout / rollback

Repository-only; nothing deployed by this packet. The new surface is read-only and default-safe (no flag/scheduler).
Rollback = revert the commit (migration-free). **Auto-merge OFF. STOP before merge.**
