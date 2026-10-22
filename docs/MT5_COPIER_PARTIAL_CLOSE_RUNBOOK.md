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
`expected_symbol_required` (symbol is MANDATORY), `identity_pin_required` (job-bound login+server MANDATORY,
no env fallback), `position_not_found`, `positions_get_failed`, `multiple_positions_returned`,
`ticket_mismatch`, `symbol_changed`, `would_be_full_close` (volume ≥ current, incl. after rounding),
`rounds_to_zero`, `below_min_reduction`, `remainder_below_min`, `unsupported_filling_mode`,
`identity_rejected` (wrong login/server). Post-send outcomes ALL carry `need_reconcile:true` — the
authoritative remaining volume is broker state, not arithmetic: `order_send_none_unknown` (lost ACK),
`partial_close_rejected` (read before deciding terminal state), and a mutation (`ok:true, mutated:true,
retcode, filled_volume, volume_confirmed, estimated_remaining`) where `volume_confirmed` is true ONLY for a
full `TRADE_RETCODE_DONE` matching the request within step tolerance (a `TRADE_RETCODE_DONE_PARTIAL`, a
smaller/missing reported volume → `volume_confirmed:false`). A SINGLE `order_send`; never retried.

## Post-send reconciliation states (`reconcile.reconcile_partial_close`)
`PCLOSE_RECONCILED` (actual remaining within step tolerance of intended); `PCLOSE_DIVERGENT_VOL` (mismatch —
recorded, operator exception, NO blind retry); **`PCLOSE_TICKET_GONE` (ticket absent after a PARTIAL — a
DIVERGENCE, NEVER a silently-approved full close)**; `PCLOSE_RECON_UNAVAIL` (read refused — non-terminal,
re-read later).

## Lifecycle status — implemented vs NOT armed (ADR-0056)
Implemented + tested end-to-end (all DARK): producer; bridge primitive; the dedicated DEMO **consumer**
(claim → **authorize-send** → single bridge call → complete), with the authoritative post-send reconciliation
+ TERMINAL job bookkeeping; the host-side **worker** (`scripts/mt5_partial_close_worker.py`); and the
background **reconcile sweep** for a crashed worker. The flow:
1. **claim** (`/api/copy-trading/partial-close/claim/`) — dedicated `partial_close_worker` only; in-lock gates;
   marks the job RUNNING + leased; returns an ACK only (no mutation instruction).
2. **authorize-send** (`/api/copy-trading/partial-close/authorize-send/`) — the BACKEND re-reads all three
   flags + revalidates everything under the lock, persists a single-use nonce, and ONLY THEN releases the
   ticket/volume/symbol/identity. This is the authoritative pre-send arming check; the worker cannot see a
   backend flag.
3. the worker performs the SINGLE `partial_close_position` call (never retried) and reports via **complete**.
4. **complete** drives the reconciliation state machine + terminal job status (SUCCESS / FAILED / recon-pending).
5. a worker that dies mid-flight is recovered READ-ONLY by `sweep_partial_close_reconcile` (never a re-send).

**NOT done (each a separate Sponsor gate before any DEMO run):** granting a real `partial_close_worker`
identity (none exists outside tests); arming `COPIER_EXECUTE_DEMO_ARMED`; enabling the worker
(`COPIER_PCLOSE_WORKER_ENABLED`, unprovisioned by default); the account rehearsal. Until all four, no partial
close reaches a broker.

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
6. Grant the dedicated `partial_close_worker` identity (modern creds; `authorized_nodes` +
   `partial_close_account_id` for the DEMO follower only) and start the host worker
   (`COPIER_PCLOSE_WORKER_ENABLED=1`, modern creds) on the approved node.
7. The worker claims → authorize-send (backend re-reads the flags in-lock) → single `partial_close_position`
   call → complete; capture the broker response and the dedicated audit events (`COPIER_PCLOSE_*`).
8. Confirm the terminal job status (SUCCESS on reconciled; FAILED on divergence/ticket-gone) and that the
   actual remaining follower volume reconciles against `expected_remaining`; record any divergence as an
   operator-visible exception. If the worker is killed mid-flight, confirm the background sweep finalizes the
   job READ-ONLY with no second send.
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
