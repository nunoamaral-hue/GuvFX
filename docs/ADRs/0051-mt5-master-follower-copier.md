# 0051 — MT5 Master/Follower Copy‑Trading Subsystem (DARK, observer‑first)

- Date: 2026-10-20
- Status: Accepted (DARK engineering only; see Approval)

## Context

The Sponsor packet `GUVFX_MT5_LIVE_MASTER_FOLLOWER_COPY_TRADING` (P0, 2026‑10‑20) requires a
GuvFX strategy that copies every eligible trading operation from one **master** MT5 account to
explicitly selected, individually authorized **follower** accounts (same‑ and cross‑broker:
IS6FX, TradersWay, Pepperstone), with configurable sizing, full SL/TP replication, safe
follower‑disable handling, and a customer frontend. LIVE money is the intended end state but is
explicitly a **separate Sponsor + Security GO**; this packet authorizes **DARK engineering** only.

No copy‑trading code exists today. The day‑1 capability map
(`docs/MT5_COPIER_DAY1_CAPABILITY_MAP.md`) established the collision gate, execution‑path reuse,
the master‑observation gap, and the Friday/Monday feasibility verdict.

## Verified facts

- The real order path is signal‑driven; `strategies/execution.py` is a placeholder that raises
  `ExecutionEngineNotImplementedError`. Every command funnels through `ExecutionJob.save()`
  (`backend/execution/models.py:275`), which enforces kill switch, Stop‑Trading, broker gate,
  broker‑runtime‑pause, and hosted identity‑pin injection.
- LIVE admission is centralized in `live_execution_permitted(account)`
  (`backend/execution/live_authz.py:57`), checked at five layers (planning, promotion, routing,
  readiness cond‑11, bridge‑config), default OFF, fail‑closed.
- Bridge `close_position`/`modify_position` handlers are **unconditionally demo‑locked**
  (`account_not_demo`, no `MT5_ALLOW_LIVE` path; `scripts/mt5_signal_bridge.py:1707,1997,2124`,
  proven by `backend/execution/tests_bridge_live_readonly.py:118`).
- Read‑only LIVE monitoring is supported via the snapshot identity firewall
  (`backend/execution/snapshot_transport.py` `verify_snapshot_identity:178`).
- Observation today is poll‑only on a history‑deals snapshot; **SL/TP, partial closes, pending
  orders, close‑reason and an event ledger are not captured** (`backend/trading/position_ingest.py`,
  `backend/mt5_trade_ingest_worker.py`). The bridge *can* return SL/TP/live price in a transient
  `/mt5/positions` read (`backend/analytics/views_trade_history.py:194`), but nothing persists/diffs it.
- Source‑scoped fan‑out already exists: `StrategyAssignment.signal_source` +
  `auto_router._resolve_targets` (`backend/execution/auto_router.py:222`).

## Assumptions

- Real‑MT5 **DEMO** infrastructure is reachable (Tailnet up; VPS + bridge `:8788` reachable on
  2026‑10‑20). The specific usable demo accounts, bindings and readiness are **not yet
  inventoried** (deferred read‑only prod query at the rehearsal gate) — treated as an assumption,
  not a fact, until confirmed.
- The bridge's positions read returns SL/TP/current price fields usable for diff‑based event
  detection — to be confirmed empirically in PR A against a real/recorded snapshot.

## Decision drivers

Safety (no new LIVE financial authority), reversibility (DARK/flag‑gated), minimal architecture
disturbance (reuse the one guarded execution path), auditability, and the governance boundary
(a new execution‑adjacent subsystem is Amber→RED‑adjacent and requires a recorded decision).

## Options considered

- **A — New governed strategy source feeding the existing execution path (chosen).** A read‑only
  master observer + durable event ledger; a reconcile engine emits **enqueue‑only**
  `PLACE_ORDER_SHADOW`→DEMO / `CLOSE_TRADE` / `MODIFY_POSITION` jobs via `ExecutionJob.save()`,
  inheriting every gate. Pros: no competing engine; inherits kill switch/risk/identity/live wall;
  smallest blast radius. Cons: the observer is a substantial new read‑only capability.
- **B — Direct broker execution from Django / a new copier engine.** Rejected: forbidden by
  `.claude/rules/architecture.md` (no competing execution engine), bypasses every safety gate.
- **C — Defer entirely pending LIVE certification.** Rejected: the DARK observer/ledger/follower
  foundation is safe, valuable, and prerequisite to any future LIVE path.

## Decision

Adopt **Option A**. Build the copier **observer‑first**, DARK, behind a default‑OFF feature flag,
as a new Django app that reuses `ExecutionJob`, the snapshot identity firewall, source‑scoped
routing, the three‑layer sizing, and `BrokerInstrument` symbol resolution. **LIVE follower
execution is out of scope** and is not enabled by any repository merge. The demo‑only
close/SL‑TP bridge protections are **not modified or bypassed**.

## Consequences

- New app with its own migration sequence (no shared‑app migration edits) — isolated from the
  Withdrawal/BI workstream.
- PRs: **A** master observer + ledger (read‑only); **B** follower config + replication engine +
  risk (enqueue‑only SHADOW→DEMO); **C** Copy Trading UX; **D** cross‑broker cert + demo readiness.
- LIVE follower close/SL/TP would require new bridge execution‑path work — explicitly **deferred**
  to a future Sponsor + Security‑gated packet.

## Risks and controls

- **Risk:** a DARK flag accidentally enabling real‑money execution. **Control:** the copier never
  calls `order_send`; it only creates `ExecutionJob`s, which remain SHADOW/DEMO‑gated by the
  existing five‑layer LIVE wall + kill switch; the copier flag is ANDed with those, never ORs past them.
- **Risk:** observing a copier‑placed follower trade as a new master event (replication cycle).
  **Control:** copier‑origin provenance (magic/comment) excluded from master‑event generation.
- **Risk:** cross‑user replication. **Control:** `CopierFollower.owner` must equal the master's
  `TradingAccount.user`; enforced at service + view layer, fail‑closed.
- **Boundary:** anything that touches the LIVE execution path, broker credentials, production
  deploy/migration, or risk‑limit changes is **RED** and out of scope for this ADR.

## Evidence / validation

Day‑1 reconnaissance (three independent read‑only sweeps) recorded in
`docs/MT5_COPIER_DAY1_CAPABILITY_MAP.md`. PR‑level empirical tests + adversarial execution‑safety
review to follow per PR; real‑MT5 DEMO certification is a separate rehearsal gate (PR D).
Not covered here: any LIVE execution evidence (blocked by design).

## Reversal path

The subsystem is DARK behind a default‑OFF flag and lives in a self‑contained app; disabling the
flag fully neutralizes it. Unmerged work is discardable by dropping the branch/worktree; merged
DARK work is revertible as additive commits with no production behaviour change.

## Revisit trigger

(1) A Sponsor + Security GO for LIVE follower execution; (2) a decision to build the LIVE
close/SL‑TP bridge path; (3) any change to the five‑layer LIVE wall or `ExecutionJob.save()` gates.

## Approval

Programme Director scope decision (2026‑10‑20): **GO — build DARK, observer‑first**; LIVE follower
execution NOT authorized; no production deploy/migration; governed manual squash‑merge after
checks + governance gates. If repository governance requires a separate recorded architecture
ratification before merge, that gate must be satisfied before merging. Sponsor/Notion ratification
is **not** claimed here unless and until actually granted.
