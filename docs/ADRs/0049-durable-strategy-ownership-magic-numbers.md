# 0049 — Durable Strategy Ownership + MT5 Magic Numbers (Phase A)

- Date: 2026-09-22
- Status: Accepted

## Context

The multi-broker / multi-strategy programme requires every strategy-generated order,
position and trade to be durably attributable to the exact `StrategyAssignment` that
originated it. Today the live signal-copy path attributes execution to a `TradingAccount`
only; strategy attribution is comment-only (`Trade.comment = "WAY{plan}L{leg}"`), which is
truncation/loss-fragile — MT5 drops the comment and zeroes the magic on close deals
(`trading/views.py:689`). This ADR covers the Phase-A backend FOUNDATION only (DARK →
dual-write → verify); read-switch (A5) and enforcement (A6) are deferred to later packets.

## Verified facts

- The deployed node1 and node2 bridges are byte-identical (SHA `819E62B0…`) and already
  set `request.magic` from the order payload and expose `magic` on deals/orders/positions
  (`scripts/mt5_signal_bridge.py:1092/1457/1618` send; `1717/1823/1901/2050` read-back). No
  bridge change is needed.
- `Trade.magic_number` is already persisted from ingestion (`trading/models.py:336`,
  `position_ingest.py:88`). The live signal-copy path currently sends `magic=0`
  (`signal_promotion._order_payload` omits it).
- `ExecutionJob.strategy`/`assignment` FKs already exist (nullable); the live path leaves
  them NULL. `SignalExecutionPlan` and `Trade` had no strategy-assignment FK.
- Legacy magics observed in prod: `99xxxx` (Strategy.magic_number) and small strategy-id
  ints; a `1e9`-based band is disjoint. `Trade.magic_number` is `IntegerField` (int32).

## Assumptions

- The deployed bridge accepting a non-zero payload `magic` behaves correctly on a live open
  position — to be host-verified before enabling `STRATEGY_MAGIC_SEND_ENABLED` (money-path).
- The `(account, source, is_active)` unique-match rule resolves historical plans acceptably
  for backfill (ambiguous/none left NULL, never guessed).

## Decision drivers

Reversibility (DARK/flags-off byte-identical), fail-closed safety, additive/nullable schema
(mig-0022 lesson: no NOT-NULL/DB-default), DB stays authoritative (MT5 magic/comment are
reconciliation metadata), minimal money-path surface, governance (money-path gated).

## Options considered

- Comment-only (status quo) — fragile; loses attribution on close. Rejected.
- `hash()`-derived magic — unstable across runs. Rejected (packet §5).
- Denormalized per-leg ownership FK — rejected; the leg inherits ownership from its plan.
- **Deterministic DB-controlled magic (`1e9 + assignment.id`) + FK ownership chain,
  DARK/dual-write.** Chosen.

## Decision

Add `StrategyAssignment.magic_number` (+ `magic_number_allocated_at`, a partial-unique
registry, and a CheckConstraint keeping the band `>= 1e9`), `SignalExecutionPlan.strategy_assignment`
and `Trade.strategy_assignment` (all nullable, SET_NULL). Allocate magic deterministically as
`ASSIGNMENT_MAGIC_BASE (1e9) + assignment.id`. Dual-write the ownership under
`STRATEGY_OWNERSHIP_DUAL_WRITE_ENABLED`; send `magic` on the real (DEMO) order path under the
separate `STRATEGY_MAGIC_SEND_ENABLED`; stamp `Trade.strategy_assignment` in the monitor chain
via the §9 magic/comment matrix (fail-closed on conflict). Provide the guarded owner-scoping
primitive (`STRATEGY_OWNERSHIP_ENFORCE_ENABLED`, fail-open on legacy NULL); wiring it into the
protection workers is deferred to A6. Read preference (`STRATEGY_OWNERSHIP_READ_ENABLED`) is A5.
All four flags default OFF.

## Consequences

- With flags OFF, execution is byte-identical (no plan/job ownership written, no payload magic,
  no extra query). Backfill (`backfill_execution_ownership`) and allocation
  (`allocate_assignment_magics`) are DRY-RUN by default.
- Close attribution still needs the open-trade+comment fallback (MT5 drops magic on close deals).
- `Trade.magic_number` int32 bounds the band; widen to BigIntegerField if versioning is wanted.

## Risks and controls

- **P0 money-path** — nonzero magic on the live payload: SHADOW never sends magic; gated by
  `STRATEGY_MAGIC_SEND_ENABLED` (default OFF, one-time WARN); Red-adjacent → Nuno approval after
  host bridge verification. **P0** — additive/nullable only (mig-0022); Trade stamping is a
  Django-side monitor-chain step (the standalone ingest worker is untouched). **P0** —
  owner-scoping fails OPEN on NULL owner so legacy protection ladders never starve.

## Evidence / validation

`backend/.venf/bin/python manage.py test` → **4599 tests OK (skipped=1)**; new suite
`execution.tests_strategy_ownership` (27 adversarial tests) OK; governance-check + frontend-parity
PASS. Not covered: 6 PRE-EXISTING frontend i18n/route test failures (support/login/dashboard
localization) unrelated to this backend-only change (branch has zero frontend edits); natural-signal
dual-write/magic-send certification (time-gated, separate go-ahead).

## Reversal path

Flip the flags OFF (written FK/magic values are inert to current readers); reverse the three
additive migrations (drop columns/constraints); the allocate/backfill commands null only the ids
they set. Pre-deploy image tagged `rollback-preOWNERSHIP`.

## Revisit trigger

Enabling dual-write, magic-send, read-switch (A5) or enforce (A6) — each a separate packet/gate.

## Approval

Sponsor (Nuno) approved the DARK build + deploy (flags OFF) and this ADR via the Phase-A go/no-go
(2026-09-22). `MAGIC_SEND`, read-switch and enforce remain separate, un-granted gates.
