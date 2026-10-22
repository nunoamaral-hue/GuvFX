# 0056 — MT5 Copier: dedicated DEMO partial-close consumer + authoritative post-send reconciliation (DARK)

- Date: 2026-10-22
- Status: Proposed (DARK engineering design; a new mutation-DISPATCH surface — see "Why design-first")
- Extends: [0051](0051-mt5-master-follower-copier.md), [0055](0055-mt5-copier-partial-close-lifecycle.md)
- ADR-0054 remains reserved for the Withdrawal workstream (untouched).
- Packet: `GUVFX_MT5_LIVE_MASTER_FOLLOWER_COPY_TRADING` Workstream B increment 3b

## Context

ADR-0055 (#545, merged DARK) built the partial-close producer + the DEMO/HEDGING bridge primitive
`partial_close_position`, and made `PARTIAL_CLOSE_TRADE` **unclaimable** at the generic `next_job` endpoint.
That leaves the operation unreachable end-to-end — by design. This ADR specifies the **dedicated DEMO-only
consumer** that would make the lifecycle demonstrable, plus the authoritative post-send reconciliation and the
result state machine.

### Why design-first (this ADR before the endpoint lands)

A dedicated claim endpoint that dispatches a **broker-mutating** job is the highest-risk new surface in the
programme (architecture rule: a new execution path needs an approved decision before merge). This ADR is the
reviewable decision; the endpoint implementation is gated on its acceptance. Shipped alongside this ADR in the
same PR (already implemented + tested): the cross-producer in-lock arming fix (ADR-0055 pattern applied to
OPEN/CLOSE/MODIFY), the audit-classification fix (an unclaimable partial-close is "unsupported", not a
NODE_WRONG_CLAIM), and the reconciliation-state foundations.

## Trust boundary (dispatch of a DEMO partial close)

```
 master partial-close event (observed, read-only)
   -> replication intent (owner-scoped, SHADOW)           [trade_copier, DARK]
   -> producer admit + in-lock arm re-read                [backend, flags default-OFF]
   -> PARTIAL_CLOSE_TRADE job (armed DEMO only)            [NOT claimable at generic next_job]
   == TRUST BOUNDARY 1: dedicated CLAIM endpoint ==        [NEW surface, this ADR]
      gate: dedicated worker permission (partial_close_worker); mixed generic grant refused
      gate: node-aware + account-authorized worker (authorized_nodes, partial_close_account_id)
      gate: exact tenant/account route (job.account owner + active node)
      gate: re-check COPIER_ENABLED + COPIER_EXECUTE_ENABLED + COPIER_EXECUTE_DEMO_ARMED IN-LOCK at claim
      gate: DEMO env only; reject LIVE/CONTEST/unresolved; server-derivable identity pin present
      row lock + lease; serves ONLY PARTIAL_CLOSE_TRADE; cannot claim OPEN/CLOSE/MODIFY/arbitrary
      -> returns an ACKNOWLEDGMENT ONLY (job id + dispatch token); NO mutation instruction
   == TRUST BOUNDARY 2: dedicated AUTHORIZE-SEND endpoint == [NEW surface, this ADR — F1]
      the BACKEND (not the worker) re-reads ALL THREE feature flags + revalidates DEMO / follower /
      account / node / account-route / replication / ticket / symbol / identity UNDER the job row lock,
      fails closed on ANY change, persists a SINGLE-USE send nonce, and ONLY THEN releases the mutation
      instruction (server-derived ticket / volume / symbol / identity pin). Never a second authorization.
   -> host worker calls authorize-send, then performs the SINGLE bridge call with the issued instruction
   == TRUST BOUNDARY 3: bridge partial_close_position ==   [demo-locked, identity-pinned, ADR-0055]
      re-read position, RE-VERIFY MT5 identity (login/server) immediately before mutation, re-normalize
      volume, SINGLE order_send (never retried)
   -> broker response -> completion endpoint -> authoritative post-send reconciliation (re-read remaining
      volume) -> TERMINAL job bookkeeping (SUCCESS / FAILED) ; a crashed worker -> background reconcile sweep
```
Authority flows one way and each hop checks ONLY what it can see: the **backend** holds the feature-flag +
route + replication authority (claim and authorize-send re-check it in-lock — the worker cannot see a backend
flag and is never described as checking one); the **worker** is least-privilege, may claim nothing but
`PARTIAL_CLOSE_TRADE`, and only relays the server-issued instruction; the **bridge** independently re-verifies
the MT5 account identity immediately before `order_send`. No customer-supplied identity authority at any hop.

## Identity-pin source (server-derived, never customer-supplied)

- **Hosted (Provider-B) accounts:** expected login/server derive from the server-side provisioning binding —
  `AccountProvisioning` / `hosted_pin.hosted_windows_username_for` + the account's bound broker identity (the
  same authoritative source `ExecutionJob.save()` uses to inject the hosted identity pin). Never from request
  input.
- **Non-hosted (legacy) accounts:** from the account's own durable binding — `TradingAccount.account_number`
  (login) + its `broker_server.server_name` (server), as validated by the execution binding checks.
- The consumer stamps `expected_login` + `expected_server` onto the dispatch IMMEDIATELY before handing the
  job to the worker; the bridge re-verifies both immediately before `order_send` (ADR-0055). Either value
  absent ⇒ fail closed (no dispatch).

## Result / reconciliation state machine (on `CopierReplication.reconciliation_status`, ≤24 chars)

| State | code | meaning |
|---|---|---|
| dispatch refused (pre-mutation) | `PCLOSE_DISPATCH_REFUSED` | a claim/pre-send gate failed; no send |
| mutation sent, result known | `PCLOSE_SENT` | order_send returned a retcode; reconcile next |
| mutation outcome unknown | `PCLOSE_OUTCOME_UNKNOWN` | lost ACK (`order_send_none_unknown`) |
| reconciliation pending | `PCLOSE_RECON_PENDING` | sent; awaiting authoritative re-read |
| reconciliation unavailable | `PCLOSE_RECON_UNAVAIL` | re-read refused; retry the READ (never the send) |
| reconciled to intended | `PCLOSE_RECONCILED` | remaining within step tolerance of intended |
| divergent remaining | `PCLOSE_DIVERGENT_VOL` | remaining differs materially |
| ticket unexpectedly gone | `PCLOSE_TICKET_GONE` | position absent after a partial — divergence, never a full close |
| operator intervention | `PCLOSE_OPERATOR_REVIEW` | a divergence/abort requiring a human |

Invariant: a broker rejection, timeout, lost ACK or partial fill is **never** classified terminally
"unchanged" without an authoritative position read. No automatic `order_send` retry in any state; only the
READ is retryable (`PCLOSE_RECON_UNAVAIL`).

## Authoritative reconciliation (F)

Re-read the exact follower ticket through the identity firewall; reconfirm the expected symbol; obtain the
**authoritative broker `volume_step`** (from the follower's broker instrument metadata, not a hard-coded
default) for the comparison tolerance; compare actual vs intended remaining; a missing ticket is divergence
(never a full-close conversion); never retry `order_send`; concurrency-safe + idempotent; record a sanitised,
operator-visible outcome.

## Mandatory tests (to accompany the endpoint implementation)

Generic workers cannot claim partial-close; only the dedicated DEMO consumer can; missing/stale arming (at
claim and pre-send) prevents mutation; arming removed after claim → no send + intent preserved + no auto-retry;
wrong tenant/account/node/login/server prevents mutation; missing identity fields prevent mutation; LIVE/
CONTEST/unknown env fail; NETTING/EXCHANGE/unknown fail; full DONE / DONE_PARTIAL / lost ACK / rejection all
reconcile; missing ticket → divergence; incorrect remaining → divergence; reconcile read failure retryable
without resending; duplicate delivery never sends twice; lease expiry no blind second send; OPEN/CLOSE/MODIFY
unchanged.

## DEMO-rehearsal packet — still requiring Sponsor input (nothing below is done)

Before any DEMO run the Sponsor must name/approve: the dedicated worker identity + its `partial_close_worker`
permission grant; the authorized node(s); the exact HEDGING **DEMO** master + follower accounts (login/server);
the symbols; max position size + max aggregate exposure; the arming window; abort thresholds; the kill-switch
operator. No account rehearsal, arming, deployment or migration occurs without that written approval.

## Adversarial-review corrections (2026-10-22) — the endpoint implementation MUST close these

An independent adversarial review of this design surfaced the following; the design is updated accordingly and
the endpoint implementation is bound by them.

- **HIGH-1 — node authorization must be satisfiable.** The partial-close producer previously emitted the job
  with a NULL `terminal_node`, so a hosted node-authz gate (`job.terminal_node_id == account.terminal_node_id`)
  could never match. **Fixed in this PR:** `produce_partial_close_one` now snapshots `terminal_node_id`. The
  consumer's node-authz reuses the proven hosted entitlement (`authorize_hosted_claim` / `resolve_hosted_route`)
  for hosted accounts; for a **legacy DEMO** follower (no hosted workspace) node-authz is not provided by that
  helper, so the consumer MUST additionally bind the claim to the exact `account` owner + a dedicated
  node/account allowlist (never a pass-through).
- **HIGH-2 — least privilege must be enforced, not assumed.** A node+account-authorized `partial_close_worker`
  could otherwise also poll the generic `next_job` and claim OPEN/CLOSE/MODIFY for its accounts (grants are
  additive). The implementation MUST make the dedicated identity carry ONLY `partial_close_worker` with NO
  generic-claim entitlement, AND the generic `next_job` MUST refuse an identity whose only grant is
  `partial_close_worker` (serve it nothing) — so the dedicated worker can claim `PARTIAL_CLOSE_TRADE` only, and
  nothing else can.
- **HIGH-3 — no double-send on lost ACK / lease expiry.** Reaching `order_send` MUST durably take the job out
  of every re-claimable / orphan-requeue path BEFORE the send (claim → RUNNING + lease, and
  `PARTIAL_CLOSE_TRADE` is EXCLUDED from the RUNNING-orphan PENDING-requeue recovery). A lost ACK
  (`PCLOSE_OUTCOME_UNKNOWN`) is resolved ONLY by an authoritative reconciliation read, never a re-dispatch;
  re-arming after an unknown result requires a fresh governed decision.
- **MEDIUM-4 — claim-time recheck is IN-LOCK.** The flag + arming recheck at claim occurs UNDER the claim row
  lock (same invariant as the producer fix in this PR), before the lease is written; the pre-send bridge
  recheck is the backstop.
- **MEDIUM-5 — authoritative step, no default.** `reconcile_partial_close` now REQUIRES an explicit `step`
  (the default was removed); the consumer MUST pass the follower's authoritative broker `volume_step`, never a
  hard-coded default, so the tolerance cannot silently mis-classify a divergence as reconciled.
- **MEDIUM-6 — CONTEST rejection is bridge-enforced.** `is_demo_environment` is binary (DEMO/LIVE) and does not
  model CONTEST; CONTEST is caught fail-closed at the bridge (`trade_mode != 0`). The consumer relies on that
  live `trade_mode` check for CONTEST (documented, not a consumer-side policy).
- **MEDIUM-7 — legacy identity-pin derivation + backstop.** No shared helper derives login/server for a
  non-hosted account (`identity_pin_for` returns `{}`). The consumer MUST derive them from the account's
  durable binding (`account_number` + `broker_server.server_name`); safety is additionally backstopped by the
  bridge forcing `require_identity_pin=True` and refusing the env fallback, so an omitted legacy pin fails
  closed rather than trading under an env pin.
- **LOW-8 — status field width.** `reconciliation_status` is `varchar(24)`; the longest new code
  (`PCLOSE_DISPATCH_REFUSED`, 23) fits with 1 char headroom. The implementation MUST keep codes ≤24 or widen
  the field by migration before adding longer codes.

## Implementation status (2026-10-22 — end-to-end completion, correction directive F1–F6)

The consumer is now a GENUINELY END-TO-END, crash-safe dedicated DEMO partial-close consumer. Changed/added in
#546: `backend/trade_copier/consumer.py`, `consumer_api.py`, `urls.py`, `scripts/mt5_partial_close_worker.py`,
and `backend/execution/execution_health.py` (sweep wiring). Endpoints under
`/api/copy-trading/partial-close/{claim,authorize-send,complete}/`. What remains before a DEMO run (each a
separate Sponsor gate): granting a real `partial_close_worker` identity (none exists outside tests); arming
`COPIER_EXECUTE_DEMO_ARMED`; enabling the host worker (`COPIER_PCLOSE_WORKER_ENABLED`, unprovisioned by
default); and the account rehearsal. ADR-0056 stays **Proposed** until this implementation passes review
(Sponsor disposition). Nothing is deployed, armed, executed, or granted.

### Correction directive findings (F1–F6) — how each is closed

- **F1 — authoritative PRE-SEND arming check (`authorize_send`).** A dedicated single-use endpoint
  `POST /api/copy-trading/partial-close/authorize-send/`. Under the job row lock it re-authenticates the
  claiming worker, confirms ownership + RUNNING + the `dispatched` phase, RE-READS all three feature flags,
  revalidates DEMO / follower / account / node / account-route / replication / ticket / symbol / identity,
  fails closed on any change, persists a single-use send nonce + timestamp, and ONLY THEN releases the
  mutation instruction. A second authorization for the same dispatch is refused (`already_authorized`). The
  claim no longer returns the mutation instruction — it is an acknowledgment only. (The earlier, incorrect
  claim that the host worker re-checks arming before the mutation is withdrawn: the WORKER cannot see a
  backend flag; the BACKEND authorize-send endpoint performs the authoritative flag re-check, and the bridge
  re-verifies MT5 identity.)
- **F2 — the actual host worker (`scripts/mt5_partial_close_worker.py`).** Modern-creds-only; polls ONLY the
  dedicated endpoints (no generic-endpoint knowledge); obtains the single-use authorization; calls ONLY
  `partial_close_position` with the server-issued ticket/volume/symbol/identity; reports via the completion
  endpoint; NEVER retries `order_send` on timeout/disconnect/lost-ACK/restart/uncertain result; redacts
  credentials + account-sensitive values (login/server/secret) from logs; stops safely when any gate is
  unavailable. UNPROVISIONED + INACTIVE by default (`COPIER_PCLOSE_WORKER_ENABLED` OFF); this PR creates no
  credentials and grants no permission.
- **F3 — terminal job bookkeeping.** After authoritative reconciliation the ExecutionJob is driven terminal:
  reconciled → `SUCCESS`; divergent volume / ticket-gone / operator-review / dispatch-refused → `FAILED`;
  read-unavailable stays `RUNNING` (recon-pending) for the sweep ONLY. No outcome returns the job to PENDING
  or any re-send path; a reconciled job is never left indefinitely RUNNING and is not re-claimable.
- **F4 — interrupted-reconciliation recovery (`sweep_partial_close_reconcile`, wired into the monitor chain).**
  READ-ONLY: it re-reads the authoritative remaining volume with the server-derived ticket / intended / broker
  step and finalizes a stranded job; it NEVER calls `order_send`, re-dispatches, or requeues; concurrency-safe
  (`skip_locked`) + idempotent; escalates to operator review after a bounded number of unavailable reads. A job
  that was claimed but never authorized (nothing sent) is failed without a read. Crash boundaries tested: after
  claim-before-authorize, after authorize-before-bridge, after bridge-before-complete, after
  completion-before-reconcile — none can cause a second broker send. INERT while the copier is DARK.
- **F5 — completion lease + concurrency.** `_worker_lease_ok` is renamed `_completion_authorized` and
  documented: lease expiry is DELIBERATELY not an authorization boundary after dispatch (the worker id +
  dispatch token are the authority, completion is idempotent, and there is no re-send path — a dead worker's
  job is resolved by the read-only sweep). Concurrent duplicate completions are serialised by the `completing`
  phase + the in-lock recon state so only one transition reconciles; a duplicate returns the durable result.
- **F6 — dedicated audit-event family.** `COPIER_PCLOSE_{CLAIM_ACCEPTED,CLAIM_REFUSED,SEND_AUTHORIZED,
  SEND_AUTH_REFUSED,DISPATCH_ATTEMPTED,OUTCOME_UNKNOWN,RECONCILED,DIVERGENCE,OPERATOR_REVIEW,DISPATCH_REFUSED}`
  via the fail-open `core.audit.log_event`. Metadata is safe identifiers + reason/status codes only (no
  credentials, secrets or raw payloads; `_sanitize_metadata` is a backstop).

### Reconciliation-input safety (carried from the prior review, still in force)

- The tolerance `step` is the follower's AUTHORITATIVE broker instrument step (`_broker_constraints(acct,
  symbol).lot_step`; non-authoritative ⇒ `PCLOSE_OPERATOR_REVIEW`) and the intended remaining is the
  producer-stamped `job.payload["expected_remaining"]`. Any worker-supplied `volume_step` /
  `estimated_remaining` in the completion POST is IGNORED.
- Identity pin is derived UNIFORMLY from the account's durable binding (`account_number` +
  `broker_server.server_name`); blank/unresolved fails closed; `require_identity_pin` forced; the bridge
  re-verifies login/server before `order_send`.
- Mutual exclusivity rests on the UNCONDITIONAL 403 at the generic `next_job` + the base-qs exclusion, not the
  grant shape; `assert_dedicated_worker` additionally refuses a `shadow_worker` co-grant.

## Status / approval
DARK engineering design + implementation. ADR ratification, DEMO arming, the dedicated worker grant,
deployment and LIVE all remain separate explicit Sponsor gates. `DEMO_CERTIFIED` /
`LIVE_EXECUTION_CERTIFIED` = false; actual live availability = 0%.
