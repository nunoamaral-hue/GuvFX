# MT5 Copier — Partial-Close Operator Runbook, DEMO Rehearsal Plan & Rollback (ADR-0055)

Status: DARK engineering. Nothing here is armed, deployed or executed. Every step below that touches a real
(DEMO) account is **Sponsor-gated** and must not be performed without explicit written authorization naming
the accounts, limits, abort thresholds and a kill-switch operator.

## What shipped (code only)
- Distinct job type `ExecutionJob.JobType.PARTIAL_CLOSE_TRADE` (risk-reducing; not in the kill-switch or
  broker-gate sets; identity-pinned; not in any deployed worker's claim allowlist).
- Bridge primitive `scripts/mt5_signal_bridge.py::partial_close_position(ticket, volume, identity,
  expected_symbol)` — DEMO + HEDGING only, explicit ticket + explicit partial volume, send-time re-read +
  re-normalize, single `order_send` (no blind retry), structured reason codes, never a full close.
- Backend producer `trade_copier/execution_producer.py::produce_partial_close_one` + `admit_partial_close`
  (+ `produce_pending_partial_closes`), proportional reduction from the authoritative current follower
  volume, SHADOW-by-default.
- Migration `execution/0035_alter_executionjob_job_type.py` (choices only; NOT applied to any prod DB).
- Flags unchanged: gated by `COPIER_ENABLED` + `COPIER_EXECUTE_ENABLED` (+ `COPIER_EXECUTE_DEMO_ARMED` to
  arm), all default OFF.

## Bridge-contract summary (reason codes)
`account_not_demo`, `account_mode_not_certified` (netting/exchange/unknown), `volume_non_positive`,
`would_be_full_close` (volume ≥ current, incl. after rounding), `rounds_to_zero`, `below_min_reduction`,
`remainder_below_min`, `position_not_found`, `positions_get_failed`, `symbol_changed`,
`unsupported_filling_mode`, `identity_rejected`, `order_send_none_unknown` (lost ACK — needs reconciliation,
never retried), `partial_close_rejected`. Success returns `{ok, partial:true, requested_volume,
filled_volume, expected_remaining}`.

## Hedging / netting support decision
Certified for **HEDGING** accounts only (`margin_mode == RETAIL_HEDGING`). **NETTING, EXCHANGE and unknown
modes fail closed** (`account_mode_not_certified`). Rationale: on a netting account a symbol has one merged
net position, so a per-ticket partial reduction is ambiguous; certifying netting is a separate later decision.

## Known unsupported cases (fail closed)
Netting/exchange/unknown account modes; a below-min reduction or remainder (no full-close substitution
policy in this increment); a partial that computes to the full position; unsupported broker filling modes;
any position without a verified copier-owned ticket (`CPY<replication_id>`); LIVE (no bridge path).

## DEMO rehearsal plan (Sponsor-gated — NOT executed here)
Preconditions (Sponsor approves in writing): named HEDGING **DEMO** master + follower accounts; max position
size; max aggregate exposure; test duration; abort thresholds; named kill-switch operator.
1. Enable `COPIER_ENABLED` + observer/replication on the DEMO master only (no other tenant).
2. Open a copied DEMO position; confirm reconciliation sets `follower_ticket` (MATCHED, `CPY<id>`).
3. On the master, perform a partial close; confirm the `PARTIAL_CLOSE` event + replication intent.
4. With `COPIER_EXECUTE_ENABLED` only (NOT armed): confirm a SHADOW record with the computed reduction and
   **no** ExecutionJob.
5. Arm (`COPIER_EXECUTE_DEMO_ARMED`) for the approved window only; confirm exactly one `PARTIAL_CLOSE_TRADE`
   job with the partial volume + exact ticket.
6. Drive the DEMO bridge `partial_close_position`; capture the broker response.
7. Reconcile the actual remaining follower volume vs `expected_remaining`; record any divergence as an
   operator-visible exception.
Minimum evidence (secrets/account-sensitive values redacted): master op timestamp, observation event,
replication decision, execution job, bridge response, follower remaining volume, reconciliation result,
end-to-end latency, idempotency result, abort behaviour, screenshots.
Abort: disengage `COPIER_EXECUTE_DEMO_ARMED` (kill switch) at any threshold breach; the full-close path is
unaffected; no LIVE account is ever in scope.

## Rollback plan
Every change is additive and DARK (`COPIER_EXECUTE_*` default OFF; `PARTIAL_CLOSE_TRADE` claimed by no
deployed worker; the bridge primitive is new code invoked by nothing until a DEMO consumer is wired + armed).
- Code rollback = `git revert` the squash-merge commit; migration `0035` is in-repo only (never applied to
  prod), so no DB change to undo.
- No production deployment, no flag flip, no bridge mutation and no order occur on merge. Reverting touches no
  running system.
