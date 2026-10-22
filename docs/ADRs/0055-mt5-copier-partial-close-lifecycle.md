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

## PD review corrections (2026-10-22) — CHANGES REQUIRED, applied

The first #545 build was returned CHANGES REQUIRED. The following are now corrected in code + tests:

- **Partial broker fills (P0).** The bridge treats `TRADE_RETCODE_DONE` AND `TRADE_RETCODE_DONE_PARTIAL` as a
  **mutation**, never as a clean failure. Because the authoritative remaining volume is **broker state, not
  arithmetic**, EVERY post-send outcome returns `need_reconcile=True` — a lost ACK, a partial fill, a DONE
  whose reported volume differs from the request (or is missing), and even a reject (the position may have
  moved). The reported `retcode`/`filled_volume`/`order`/`deal` are recorded; `estimated_remaining` is an
  arithmetic estimate flagged NOT confirmed (`volume_confirmed` is true only for a full DONE matching the
  request within step tolerance). A single `order_send`; never retried on any ambiguous result.
- **Mandatory job-bound identity pin (P0).** `partial_close_position` fails closed with `identity_pin_required`
  unless `identity` carries BOTH `expected_login` and `expected_server`; the send-time gate runs with
  `require_identity_pin` FORCED on, so there is **no process-environment fallback** and a wrong login/server
  fails closed before any send.
- **Mandatory `expected_symbol` (P1).** Missing/empty → `expected_symbol_required`; otherwise compared exactly
  to the live position symbol.
- **Returned-ticket verification (P1).** Exactly one position must be returned and its ticket must equal the
  request (`multiple_positions_returned` / `ticket_mismatch` otherwise); the verified ticket is used in the
  request.
- **In-lock arming (P1).** The producer re-reads `copier_execute_demo_armed()` INSIDE the row lock, immediately
  before the job decision; a flag flipped off after admission yields SHADOW + no job; the payload mode derives
  from the in-lock value. (A preflight read remains a cheap hint only; a discriminating test flips the flag
  True→False between preflight and the locked decision and proves no job is created.)
- **Claim-endpoint enforcement (adversarial-review hardening).** `PARTIAL_CLOSE_TRADE` is now excluded
  UNCONDITIONALLY from `ExecutionJobViewSet.next_job`, so "no worker executes a partial close" is an ENFORCED
  endpoint property (mirroring the `PLACE_ORDER_SHADOW` guard), not merely the emergent "nothing asks for it".

### Follow-up finding (disclosed, NOT fixed here — out of #545 scope)

The adversarial review confirmed the six corrections are real. It also surfaced that the already-merged OPEN
(`produce_one`, #537), CLOSE (`produce_close_one`, #542) and MODIFY (`produce_modify_one`, #544) producers read
`armed = copier_execute_demo_armed()` **before** the transaction and use it inside — the same stale-arming
pattern corrected here for partial close. Severity is low (DARK; `COPIER_EXECUTE_DEMO_ARMED` never armed in
production; the window is the microseconds between the pre-lock read and job creation), but it is a real
correctness gap. Recommended as a separate follow-up hardening PR that applies the in-lock re-read to all three
merged producers; NOT changed in this PR to avoid scope-creep into merged code.

## Reconciliation lifecycle (status + what is / is NOT wired)

- **Implemented:** the producer (`produce_partial_close_one`); the bridge primitive (`partial_close_position`);
  the post-send reconciliation transition `reconcile.reconcile_partial_close(replication, intended_remaining,
  step, fetch)`, which RE-READS the follower position (authoritative) and writes a terminal state:
  `PCLOSE_RECONCILED` (actual remaining within step tolerance of intended), `PCLOSE_DIVERGENT_VOL` (mismatch —
  recorded, NO blind retry), **`PCLOSE_TICKET_GONE` (ticket absent after a PARTIAL — a DIVERGENCE/operator
  exception, NEVER a silently-approved full close)**, `PCLOSE_RECON_UNAVAIL` (read refused — non-terminal,
  re-read later, never retry the send).
- **NOT wired (required before DEMO certification):** the consumer that dispatches ONLY `PARTIAL_CLOSE_TRADE`
  to the bridge primitive, and the automatic result→replication state transition driving `reconcile_partial_close`
  after each send. No deployed worker claims `PARTIAL_CLOSE_TRADE` today. Until that consumer is built + armed,
  no partial close reaches a broker.

## Status / approval
DARK engineering decision only. Merge, ADR-0055 ratification, DEMO arming, deployment, any real partial close
and LIVE all remain separate explicit Sponsor gates. `DEMO_CERTIFIED` / `LIVE_EXECUTION_CERTIFIED` = false;
actual live availability = 0%.
