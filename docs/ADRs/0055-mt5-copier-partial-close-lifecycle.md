# 0055 — MT5 Copier: partial-close lifecycle + DEMO-only partial-close bridge primitive (DARK)

- Date: 2026-10-21
- Status: Proposed (DARK engineering only; merge, ADR ratification, DEMO arming, deployment and LIVE all remain separate explicit Sponsor gates)
- Extends: [0051](0051-mt5-master-follower-copier.md), [0052](0052-mt5-copier-close-lifecycle.md), [0053](0053-mt5-copier-sltp-modify.md)
- ADR allocation note: ADR-0054 is reserved for *Withdrawal controlled erasure* (separate workstream) and is NOT used here.
- Packet: `GUVFX_MT5_LIVE_MASTER_FOLLOWER_COPY_TRADING` Workstream B (trade lifecycle), increment 3

## Context

Increments 1–2 (#542 full-CLOSE, #544 SL/TP-MODIFY) are merged DARK. A master **partial** close
(`PARTIAL_CLOSE` event — volume decreased) must reduce the follower position **proportionally**. A
no-assumption check established that the existing bridge `close_position(ticket)` closes the **entire**
position (`volume = pos.volume`) — it has **no partial path**. Producing a `CLOSE_TRADE` for a partial
intent would silently convert partial→full, which is forbidden. The Sponsor therefore authorized (Path B)
engineering of a **DEMO-only partial-close bridge capability** — code only: no deploy, no migration, no
arming, no execution, no LIVE.

## Decision

Partial close is an **explicitly distinct operation** end-to-end. It does **not** reuse full-CLOSE
semantics. A new job type `PARTIAL_CLOSE_TRADE` and a new bridge primitive `partial_close_position` carry an
**explicit ticket + explicit partial volume**; neither can fall back to a full close, a different ticket, or
a symbol.

### End-to-end path
master `PARTIAL_CLOSE` event → owner-scoped replication intent (`requested_op="PARTIAL_CLOSE"`, already
emitted by `replication.py`) → verified copier-owned follower ticket (ADR-0052 reconciliation, `CPY<id>`) →
fresh follower-position read (identity-firewalled) → authoritative current follower volume → proportional
reduction → broker min/step normalization → admission → **SHADOW record by default** → separately-armed
DEMO `PARTIAL_CLOSE_TRADE` job → bridge re-verification immediately before mutation → broker response →
reconciliation of actual remaining volume → divergence/failure recording.

### Calculation rules (producer, from authoritative volumes)
- `ratio = (master_volume_before − master_volume_after) / master_volume_before` (0 < ratio < 1 for a partial).
- `raw_reduction = ratio × follower_current_volume` (current = fresh authoritative follower read).
- `normalized_reduction = floor(raw_reduction / step) × step` — **round DOWN** so rounding never *increases*
  the risk reduction beyond the proportional intent.
- Fail closed unless ALL hold: `raw_reduction > 0`; `normalized_reduction ≥ broker volume_min`;
  `normalized_reduction < follower_current_volume` (strictly — never a full close); `remaining = current −
  normalized_reduction ≥ volume_min` and step-aligned. A below-minimum reduction or a below-minimum/
  non-aligned remainder **fails closed** — there is **no full-close substitution policy** in this increment.
- Rounding never silently becomes zero and never silently becomes a full close (both are explicit rejects).

### Ticket & identity safety
Require a MATCHED copier-owned follower ticket with `CPY<replication_id>` provenance; exact follower account,
MT5 login, broker server and DEMO environment; a fresh bridge snapshot; the ticket still open; the current
symbol consistent with the stored replication; and the current volume consistent with the admission
calculation. **Never locate or mutate a follower position by symbol alone.**

### Bridge contract — `partial_close_position(ticket, volume, identity)`
- Unconditionally **DEMO-locked** (`account_info.trade_mode == 0`, re-asserted at send time via
  `verify_mutation_identity`); rejects LIVE/CONTEST/ambiguous environments.
- Accepts an **explicit ticket and explicit partial volume**; re-reads the position at send time; rejects a
  missing/changed ticket, a changed symbol, and a volume that is zero, negative, excessive (> current),
  equal to the full position (`would_be_full_close`), below broker min, or not step-aligned after
  normalization.
- Normalizes with the **current** symbol constraints; rejects unsupported filling modes; returns structured
  reason codes; **never** falls back to a full close, a different ticket, or a different symbol.
- Preserves the existing full-close `close_position` path unchanged; exposes **no** general-purpose
  arbitrary-position endpoint to customers.

### Hedging / netting decision
Partial close is certified for **HEDGING** accounts only (`margin_mode == RETAIL_HEDGING`), where each copied
position is its own ticket and a per-ticket partial reduction is unambiguous. **NETTING, EXCHANGE and unknown
margin modes fail closed** (`account_mode_not_certified`) — on a netting account a symbol has one merged net
position and "reduce part of ticket X" is ambiguous; certifying it is a separate, later decision.

### Job type
New `ExecutionJob.JobType.PARTIAL_CLOSE_TRADE`, classified risk-reducing: excluded from the kill-switch and
broker-gate blocked sets (opens no exposure), included in the identity-pin set (reaches a real mutation, so a
wrong-account partial close fails closed at the bridge mutation-identity gate). It is **not** added to any
deployed worker's claim allowlist, so no current consumer executes it; wiring a DEMO consumer is a separate
arming step.

### SHADOW / arm
Default SHADOW: the producer records the intended reduction on the replication row and creates **no**
ExecutionJob. Only when `COPIER_EXECUTE_DEMO_ARMED` (separate, default-OFF) is on does it create one
`PARTIAL_CLOSE_TRADE` job. BUILD ≠ ARM.

### Idempotency & recovery
One replication → at most one runnable partial-close job (row-locked, `execution_job` set once). Dedup on a
non-terminal `PARTIAL_CLOSE_TRADE` for `(account, ticket)`. An unknown broker result is NOT blindly retried —
remaining-volume reconciliation runs first. Reconciliation can determine intended, requested and actual
remaining volume; divergence raises an operator-visible exception; a per-follower failure never blocks other
followers.

## Known unsupported cases (fail closed)
NETTING / EXCHANGE / unknown account modes; a below-min reduction or remainder (no full-close substitution);
a partial that computes to the full position; unsupported broker filling modes; any position not carrying a
verified copier-owned ticket. LIVE partial close has no path.

## DEMO rehearsal plan (NOT executed here — Sponsor-gated)
Operator-gated rehearsal on named DEMO accounts: open a copied DEMO position (reconciled `CPY<id>` ticket) →
master partial close → observe replication intent → (arm) one `PARTIAL_CLOSE_TRADE` job → bridge
re-verification + partial `order_send` on the HEDGING DEMO account → broker response → remaining-volume
reconciliation → divergence check. Evidence: master op, event, intent, job, bridge response, remaining
volume, idempotency, abort behaviour; secrets/account-sensitive values redacted. Arming requires explicit
Sponsor approval of accounts, max volume, abort thresholds and a kill-switch operator.

## Status / approval
DARK engineering decision only. Merge, ADR-0055 ratification, DEMO arming, deployment, any real partial close
and LIVE all remain separate explicit Sponsor gates. `DEMO_CERTIFIED` / `LIVE_EXECUTION_CERTIFIED` = false;
actual live availability = 0%.
