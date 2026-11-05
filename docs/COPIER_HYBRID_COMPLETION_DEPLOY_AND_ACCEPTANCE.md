# Copier hybrid completion — rollback-first DARK deploy packet + synthetic/SHADOW acceptance plan

ADR-0074 (GFX-COPIER-HYBRID-COMPLETION-003). **Prepared, NOT executed.** Every step below is gated on an
explicit, separate Sponsor/operator GO. Nothing here is run by this packet. The code ships DARK: all FIVE new
flags default OFF, the endpoint is 503 when off, the bridge emitter is default-OFF and NOT installed, and all
new management commands are unscheduled (run on demand).

## 0. Preconditions (verify before any deploy)

- The DARK PR is merged to main with exact-head CI 5/5 (see the PR).
- Canonical base reconciled on current main; migration `trade_copier/0010` is based on `0009`,
  `trade_copier/0011` is based on `0010` (additive), and `reliability/0005` is choices-only (no DB schema
  change).
- Confirm in prod (read-only) that `COPIER_EVENT_INGESTION_ENABLED`, `COPIER_EVENT_PROJECTION_ENABLED`,
  `COPIER_LIFECYCLE_DISPATCH_ENABLED`, `COPIER_ALERTS_ENABLED`, `COPIER_ALERT_DISPATCH_ENABLED` are unset/OFF
  and `COPIER_INGEST_HMAC_KEY` is NOT yet set (the endpoint stays 503).

## 1. Rollback-first migration plan

All migrations are **additive and reversible**:
- `trade_copier/0010` creates three NEW tables (`CopierIngestedRevision`, `CopierIngestNonce`,
  `CopierLifecycleDispatch`) — no change to any existing table, no data migration. Reverse =
  `migrate trade_copier 0009` (drops the three empty tables). Safe while flags are OFF (nothing writes them).
- `trade_copier/0011` adds two **nullable/defaulted** `MasterEvent` columns (`source` default `'POLL'`,
  `broker_event_time` null — poll rows keep today's behaviour), one `CopierMaster` counter
  (`last_projected_ingest_id` default 0), and two NEW delivery tables (`CopierAlertDelivery`,
  `CopierAlertDeliveryAttempt`). No column is dropped or altered destructively; no backfill. Reverse =
  `migrate trade_copier 0010` (drops the two empty tables + the three added columns). Safe while flags are OFF.
- `reliability/0005` is a **choices-only `AlterField`** on `component` fields — Django state only, **no SQL
  DDL** (choices are not DB-enforced). Reverse = `migrate reliability 0004` (no-op at the DB level).

**Rollback-first sequence (per `reference_prod_deploy_mechanism`, image-based, no `build:` in compose):**
1. Snapshot: record current prod backend image tag + `showmigrations trade_copier reliability`.
2. Build/push the new backend image pinned to the merged commit (`--build-arg GIT_COMMIT`).
3. Apply migrations: `migrate reliability 0005`, then `migrate trade_copier 0010` and `migrate trade_copier
   0011` (all reversible as above).
4. Recreate backend only: `up -d --no-deps --force-recreate backend` (NEVER `--remove-orphans`).
5. Health: `api` 401 on an unauthenticated probe = healthy. `operations-summary.copier` block renders with all
   flags `false`, zero alerts and an empty `alert_delivery` backlog (byte-compatible — the block is additive).
6. **Rollback trigger** (any of: migration error, health regression, unexpected copier write): recreate the
   previous image, then `migrate trade_copier 0010`, `migrate trade_copier 0009`, and `migrate reliability
   0004`. Because the flags are OFF the new tables are empty, so the down-migration is clean.

Deploy leaves **all flags OFF** — behaviourally a no-op. Observer/replication crons are unchanged; no new cron
is added.

## 2. Arming is a SEPARATE, later Sponsor gate (NOT in the deploy)

Only after a clean DARK deploy, and as independent decisions each:
- Set `COPIER_INGEST_HMAC_KEY` (its own secret, min 32 chars; rotate via the alias name, never reuse
  `GUVFX_AGENT_TOKEN`) and arm `COPIER_EVENT_INGESTION_ENABLED` — the endpoint then accepts signed emissions
  but still only persists observations.
- Arm `COPIER_EVENT_PROJECTION_ENABLED` to promote accepted revisions into the authoritative `MasterEvent`
  ledger (still no ExecutionJob, no broker). Run `prune_copier_ingest_nonces --apply` as a pre-arming hygiene
  step if the nonce store has accumulated.
- Arm `COPIER_LIFECYCLE_DISPATCH_ENABLED` to record SHADOW dispatch rows (still no ExecutionJob).
- Arm `COPIER_ALERTS_ENABLED` to raise governed operator alerts (and enqueue delivery rows).
- `COPIER_ALERT_DISPATCH_ENABLED` governs the delivery dispatcher; its default transport has **no real
  destination** (dry-run) — configuring a real transport is a further, separate step, not part of arming.
- None of these enables execution; the execute/arm walls (`COPIER_EXECUTE_ENABLED` /
  `COPIER_EXECUTE_DEMO_ARMED` / `COPIER_LIVE_EXECUTE_ENABLED` + `CopierLiveAuthorization` + kill switch) remain
  the sole order authority and are untouched.

## 3. Synthetic / SHADOW acceptance plan (no broker, no order)

Run against a staging/synthetic backend with fake adapters — never a live broker:
1. **Ingestion**: POST signed synthetic batches; assert accepted/duplicate/replayed/rejected per the contract;
   assert `ExecutionJob` count stays 0 and no `MasterEvent` is written. (Covered by `tests_event_ingestion`.)
2. **Classification**: feed open → add SL → change SL+TP → partial close → full close; assert the named change
   sequence; assert floating price/PnL never emit. (`tests_change_types`.)
3. **No-late-recovery**: a late OPEN / late link / post-terminal change → `ENTRY_FAILED_FINAL` +
   `post_terminal_*_ignored` evidence, never a late entry. (`tests_completion_lifecycle`.)
4. **SHADOW dispatch**: drive lifecycles to OBSERVED_OPEN/MODIFY_PENDING/CLOSE_PENDING; assert
   `CopierLifecycleDispatch` rows with correct idempotency keys and verdicts; assert `ExecutionJob` count stays
   0; PARTIAL is MODELED (ledger-only, no broker read) and recorded with `requires_live_volume_at_arm`.
   (`tests_lifecycle_dispatch`, `tests_partial_close_shadow`.)
5. **Alerting + delivery**: force terminal failures; assert one deduplicated `AlertEvent` per failure,
   secret-free, on the ops summary; assert ONE idempotent `CopierAlertDelivery` enqueued; assert the dispatcher
   is inert while OFF (rows PENDING), a transmitting transport → SENT, and a dry-run/failing transport →
   FAILED→DEAD with an immutable per-attempt audit. (`tests_failure_alerts`, `tests_alert_delivery`.)
6. **Bridge emitter**: observe synthetic snapshots; assert monotonic per-ticket revisions, explicit-missing
   SL/TP preserved, open/modify/partial/full-close coverage, bounded spool + retry → dead-letter, restart/replay
   via the file spool, deadline non-extension on re-sign, and that the emitted envelope verifies against the
   real verifier. (`tests_bridge_emitter`.)
7. **Authoritative wiring**: drive the full chain bridge → ingest → immutable revision → event projection →
   `MasterEvent` → lifecycle → SHADOW dispatch; assert no untouched-ticket false close (overlay), poll/event
   dedup, idempotent re-run, out-of-order rejection, and no late OPEN; assert `ExecutionJob` count stays 0
   throughout. (`tests_event_projection`, `tests_hybrid_end_to_end`.)
8. **Nonce retention**: dry-run reports eligible; apply prunes only fully-expired; in-window nonces retained.
   (`tests_nonce_pruning`.)
9. **Capacity**: record p50/p95/p99 for detection/emission, ingestion, lifecycle, and total emitter→SHADOW
   dispatch on the target host; do NOT enable sub-second cadence until a measured `CapacityProfile` authorises
   it. (`tests_hybrid_capacity` + `tests_measurement` are the synthetic harnesses.)

Acceptance PASS requires every applicable check to run and meet its criterion; a check that cannot run is
recorded PARTIAL/FAIL with the reason (evidence.md). No real-money or demo broker order is placed at any point.

## 4. Still required beyond this (separate gates)

The repository-side bridge emitter and the authoritative event→`MasterEvent` wiring are now IMPLEMENTED and
DARK in the repo. What remains, each a separate Sponsor/operator-gated step: host **installation** of the
emitter on Windows (packaging, service/task registration, key provisioning); configuring a **real** alert
delivery transport (none is configured here); and a **measured host capacity profile** before any sub-second
production cadence.
