# 0052 — MT5 Copier: follower-ticket reconciliation + full-CLOSE lifecycle (DARK/DEMO)

- Date: 2026-10-21
- Status: Accepted for DARK engineering (Sponsor-ratified 2026-10-21; DEMO arming, deployment and LIVE execution remain separate explicit gates)
- Extends: [0051](0051-mt5-master-follower-copier.md) (MT5 copier, observer-first)
- Packet: `GUVFX_MT5_LIVE_MASTER_FOLLOWER_COPY_TRADING` Workstream B (trade lifecycle), increment 1

## Context

ADR-0051 built the copier observer (PR #528), the read-only UX + owner-scoped API (PR #535),
and the **OPEN-only** DEMO execution-job producer (PR #537). The producer deliberately refuses
CLOSE/MODIFY/SCALE_IN (`_PRODUCIBLE_OPS = {"OPEN"}`) because those operations must target a
**specific follower position**, and the copier does not yet record which follower broker ticket
resulted from a copied OPEN. Without that association a CLOSE could only be matched by symbol —
forbidden by the packet ("Never modify a position merely because its symbol resembles the
master's symbol").

This ADR adds the missing foundation (the **follower-ticket association**) and the first
mutating lifecycle operation that depends on it (**master full CLOSE → follower full CLOSE**),
DARK and DEMO-only, SHADOW-by-default. Partial close, scale-in, SL/TP modification and
disable-policy are deferred to later increments of Workstream B.

## Verified facts (code, this branch's base = #537 head)

- `CopierReplication` already carries `follower_ticket` (BigIntegerField, null) and
  `reconciliation_status` (CharField), populated by nothing today
  (`backend/trade_copier/models.py:391,394`; docstring at `:365` anticipates the DEMO follow-up
  filling them).
- The OPEN producer stamps a deterministic provenance comment `CPY<replication_id>` on every
  follower order and carries `copier_replication_id` in the payload
  (`backend/trade_copier/execution_producer.py:168`).
- The replication engine **already emits CLOSE intents** (`requested_op="CLOSE"`) for a master
  `FULL_CLOSE` event (`backend/trade_copier/replication.py:45`,`208`); they are recorded
  `SHADOW_RECORDED` with no ExecutionJob and are simply ignored by the OPEN producer.
- `ExecutionJob.JobType.CLOSE_TRADE` already exists and is correctly classified: excluded from
  the kill-switch, broker-gate blocked sets (closing reduces risk / opens no exposure) and
  included in the identity-pin set (a wrong-account close must fail closed at the bridge's
  mutation-identity gate) (`backend/execution/models.py:170,378,391,402`).
- The bridge `close_position` handler is **unconditionally demo-locked** — it re-asserts
  `trade_mode == 0` at send time and returns `account_not_demo` otherwise
  (`scripts/mt5_signal_bridge.py:666`). LIVE follower close therefore has **no execution path**;
  this ADR does not create one.
- The existing CLOSE_TRADE contract is `payload["ticket"]` (account-scoped) + `symbol` +
  `windows_username`; dedup is "a non-terminal CLOSE_TRADE for this (account, ticket) exists"
  (`backend/execution/provider_commands_engine.py:110,137`).
- `observer.fetch_master_positions(account)` reads *any* account's live positions through its
  own per-tenant bridge under the snapshot identity firewall (`verify_snapshot_identity`,
  `require_environment=True`) and returns each position's `comment`
  (`backend/trade_copier/observer.py:107`). It is reused unchanged to read **follower**
  positions.

## Decision

Two additive pieces inside `backend/trade_copier/`, each behind its own default-OFF flag, ANDed
with the master `COPIER_ENABLED` gate:

### 1. Follower-ticket reconciliation (`reconcile.py`, flag `COPIER_RECONCILE_ENABLED`)

For an **armed DEMO OPEN** replication (`status=EXECUTION_REQUESTED`, `follower_ticket IS NULL`):
read the follower account's live positions (identity-firewalled, read-only), find the position
whose `comment == "CPY<replication_id>"`, and durably record `follower_ticket` +
`reconciliation_status`:

- exactly one match ⇒ `MATCHED` (the verified copier-owned follower ticket);
- no match yet, order recent ⇒ left unreconciled (benign — may not have filled);
- more than one match ⇒ `AMBIGUOUS` (fail closed — never pick one);
- identity-firewall refusal / read error ⇒ no write (never assume).

Reconciliation is **read-only** and **append-only in effect**: a ticket is written once; a later
divergence is recorded as a new status, never a silent overwrite of a confirmed match. SHADOW
replications have no real order and are never reconciled.

### 2. Full-CLOSE producer (`execution_producer.produce_close_*`, reuses `COPIER_EXECUTE_*`)

For a `requested_op="CLOSE"` replication, under the same FRESH-admission-inside-the-lock discipline
as the OPEN producer:

- resolve the follower position by looking up the **OPEN** replication for the same
  `(follower, master, master position_ticket)` whose `reconciliation_status == MATCHED` — the
  CLOSE targets *that* `follower_ticket`, never a symbol;
- fail closed if the OPEN is unreconciled / `AMBIGUOUS` / missing, if the follower is
  disabled/rebound/non-demo/cross-user, or if a non-terminal CLOSE_TRADE already targets
  `(follower_account, follower_ticket)` (idempotent);
- **SHADOW** (not armed): record the decision on the CLOSE replication
  (`status=SHADOW_JOB_RECORDED`, intended `follower_ticket`), create **no** ExecutionJob;
- **armed DEMO**: create one `CLOSE_TRADE` ExecutionJob (`payload={ticket, symbol,
  windows_username, signal_source:"copier", comment:"CPYX<replication_id>", reason}`), inheriting
  every `ExecutionJob.save()` gate and the bridge demo-lock; `status=EXECUTION_REQUESTED`.

### Explicit decision: shadow-close representation (asymmetry with OPEN)

The OPEN shadow path creates a suppressed `PLACE_ORDER_SHADOW` ExecutionJob (historical parity).
The CLOSE shadow path records the decision on the `CopierReplication` row and creates **no**
ExecutionJob. Rationale: the replication row is already the canonical per-operation intent ledger
(it carries `follower_ticket`, `status`, volumes), so a distinct `CLOSE_TRADE_SHADOW` JobType
would add surface to the shared execution model (enum + `next_job` worker-claim seam + three
classification sets) for no safety gain — a shadow close reaches no broker either way. Unifying
the two shadow representations (adding `CLOSE_TRADE_SHADOW`) is recorded as a candidate follow-up
if uniform shadow accounting is later preferred. This is the one conscious divergence from the
OPEN pattern and is flagged in the handoff.

## Hard boundaries (unchanged, re-asserted)

- DEMO followers only; LIVE is rejected in admission and has no bridge path.
- BUILD ≠ ARM: a real DEMO close runs only when `COPIER_EXECUTE_DEMO_ARMED` is on *and* a Sponsor
  has chosen the accounts — never by merging this code.
- No `order_send`/close from Django; the backend only creates a gated ExecutionJob that the
  existing demo-locked bridge may later execute.
- Never close by symbol; a close requires a `MATCHED` copier-owned follower ticket.
- Stops/risk are never weakened; the kill switch, Stop-Trading, broker gate and identity pin are
  inherited via `ExecutionJob.save()` and the bridge mutation-identity gate.

## Discriminating negative controls (each fails with the guard, passes without regression)

close-by-symbol-only is refused; an unreconciled / `AMBIGUOUS` ticket is refused; a cross-user or
self-follow follower is refused; a disabled/rebound follower is refused; a non-demo follower is
refused; a duplicate CLOSE (second produce, or a pre-existing non-terminal CLOSE_TRADE) creates no
second job; an engaged kill switch does **not** block a close (closing is risk-reducing) but does
block the paired OPEN; a reconciliation identity-firewall refusal writes no ticket; a SHADOW close
creates no ExecutionJob; an armed DEMO close creates exactly one CLOSE_TRADE targeting the matched
ticket.

## Status / approval

**Accepted for DARK engineering (Sponsor-ratified 2026-10-21).** The Sponsor approved the engineering
decision: copier-owned follower tickets are established by exact `CPY<replication_id>` provenance
reconciliation; ambiguous / absent / identity-refused matches fail closed; full CLOSE targets only a
verified copier-owned follower ticket; symbol-only matching is prohibited; CLOSE is DEMO-only and
SHADOW-by-default. Ratification and merge do NOT authorize DEMO arming, deployment or LIVE execution —
those remain separate explicit Sponsor gates (PM owns lifecycle status). `DEMO_CERTIFIED` /
`LIVE_EXECUTION_CERTIFIED` remain false.
