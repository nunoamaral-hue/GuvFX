# 0053 — MT5 Copier: SL/TP set/change/remove → follower MODIFY lifecycle (DARK/DEMO)

- Date: 2026-10-21
- Status: Accepted for DARK engineering (Sponsor-ratified 2026-10-21; DEMO arming, deployment and LIVE execution remain separate explicit gates)
- Extends: [0051](0051-mt5-master-follower-copier.md), [0052](0052-mt5-copier-close-lifecycle.md)
- Packet: `GUVFX_MT5_LIVE_MASTER_FOLLOWER_COPY_TRADING` Workstream B (trade lifecycle), increment 2

## Context

Increment 1 (ADR-0052, #542) added the follower-ticket association and the full-CLOSE producer. This
increment adds **master SL/TP set/change/remove → follower MODIFY**, the second mutating lifecycle
operation, DARK and DEMO-only, SHADOW-by-default. Like CLOSE, a MODIFY must target the **verified
copier-owned follower ticket**, never a position that merely shares the symbol.

## Verified facts (code, this branch's base = #542 head)

- The replication engine already emits `MODIFY_SLTP` intents and, per BLOCKER-3, derives the **COMPLETE
  desired SL+TP** from the authoritative same-poll snapshot (never the single changed field), storing
  `translated_sl` / `translated_tp` / `sltp_translation_ok` on the `CopierReplication`
  (`backend/trade_copier/replication.py:195`).
- `MasterEvent` records `event_type` (`SL_SET`/`SL_CHANGE`/`SL_REMOVE`/`TP_*`), `prev_sl`, `new_sl`,
  `side`, and the same-poll snapshot gives `price_open` (entry) — enough to classify a stop change as
  tightening vs weakening (`backend/trade_copier/models.py` MasterEvent; `diff.py`).
- `ExecutionJob.JobType.MODIFY_POSITION` already exists and is correctly classified: **excluded** from the
  kill-switch and broker-gate sets (risk-reducing / opens no exposure), **included** in the identity-pin
  set (a wrong-account modify must fail closed at the bridge mutation-identity gate)
  (`backend/execution/models.py:179,402`).
- The bridge `modify_position` handler is **unconditionally demo-locked** and additionally **refuses any
  SL widen** at send time (`scripts/mt5_signal_bridge.py`; the auto-breakeven "refuse-widen backstop").
  LIVE follower modify has **no execution path**.
- Reconciliation (ADR-0052) populates `follower_ticket` on the OPEN replication; a MODIFY resolves the
  same MATCHED ticket via `_matched_open_for_close` (position-keyed, not symbol-keyed).

## Decision

Add `admit_modify` + `produce_modify_one` + `produce_pending_modifies` to `execution_producer.py`, reusing
the `COPIER_EXECUTE_*` gates (default OFF) and the same fresh-admission-under-the-lock discipline as CLOSE:

- Resolve the **MATCHED copier-owned `follower_ticket`** for the master position (never by symbol); fail
  closed if unreconciled / `AMBIGUOUS` / missing.
- **"Stops are never weakened silently" (producer-side guard, defense-in-depth to the bridge refuse-widen):**
  classify the master SL change from the event and **fail closed** (produce nothing, record a reason) when
  it would weaken the follower's downside protection —
  - `SL_REMOVE` → `sl_removal_not_auto_replicated`;
  - `SL_CHANGE` that widens the stop (`|entry − new_sl| > |entry − prev_sl|`) → `sl_widening_not_auto_replicated`.
  `SL_SET`, SL tightening, and all `TP_*` changes are producible (they add/tighten protection or only move
  the profit target). Risk-increasing stop changes are deferred to a later, separately-authorized capability.
- Require `sltp_translation_ok` and the complete translated SL+TP from the replication (BLOCKER-3 complete
  state — an SL-only event still carries the intact TP). The MODIFY payload carries `{ticket, symbol, sl,
  tp}`.
- **SHADOW** (not armed): record the decision on the replication row (`SHADOW_JOB_RECORDED`, intended
  `follower_ticket`), create **no** ExecutionJob.
- **armed DEMO**: create one `MODIFY_POSITION` job for the ticket; `EXECUTION_REQUESTED`.
- Idempotent (one job per intent; dedup on a non-terminal `MODIFY_POSITION` for `(account, ticket)`);
  inherits every `ExecutionJob.save()` gate; the kill switch does not block a risk-reducing modify.

## Hard boundaries (unchanged)

DEMO followers only; LIVE rejected in admission and has no bridge path. BUILD ≠ ARM. No `order_send`/modify
from Django. Never by symbol. Stops never weakened silently (producer fail-closed + bridge refuse-widen).
Kill switch / Stop-Trading / broker gate / identity pin inherited. No migration.

## Discriminating negative controls

`SL_REMOVE` refused; `SL_CHANGE` widening refused; `SL_CHANGE` tightening produced; `TP` set/change/remove
produced; close-by-symbol-only resemblance never modified; unreconciled / `AMBIGUOUS` ticket refused;
cross-user / self-follow / disabled / non-demo follower refused; duplicate (second produce + pre-existing
non-terminal `MODIFY_POSITION`) creates no second job; SHADOW creates no job; armed DEMO creates exactly one
`MODIFY_POSITION` targeting the matched ticket; kill switch does not block the modify.

## Status / approval

**Accepted for DARK engineering (Sponsor-ratified 2026-10-21).** The Sponsor approved the engineering
decision: SL/TP operations require a MATCHED copier-owned follower ticket; symbol-only targeting is forbidden;
SL removal or widening fails closed where it could weaken protection; supported risk-reducing modifications are
DEMO-only and SHADOW-by-default. Merge does NOT authorize deployment, migration, arming or execution — those
remain separate explicit Sponsor gates (PM owns lifecycle status). `DEMO_CERTIFIED` / `LIVE_EXECUTION_CERTIFIED`
= false.
