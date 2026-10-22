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
   == TRUST BOUNDARY: dedicated DEMO consumer endpoint ==  [NEW surface, this ADR]
      gate: dedicated worker permission (partial_close_worker)
      gate: node-aware + account-authorized worker (authorized_nodes)
      gate: exact tenant/account route (job.account owner + workspace)
      gate: re-check COPIER_ENABLED + COPIER_EXECUTE_ENABLED + COPIER_EXECUTE_DEMO_ARMED at claim
      gate: DEMO env only; reject LIVE/CONTEST/unresolved; require HEDGING
      gate: server-derived expected_login + expected_server present (fail closed if absent)
      row lock + lease; serves ONLY PARTIAL_CLOSE_TRADE; cannot claim OPEN/CLOSE/MODIFY/arbitrary
   -> host worker re-checks arming immediately before the mutation
   == TRUST BOUNDARY: bridge partial_close_position ==     [demo-locked, identity-pinned, ADR-0055]
      re-read position, re-verify identity (login/server), re-normalize volume, SINGLE order_send
   -> broker response -> authoritative post-send reconciliation (re-read remaining volume)
```
Authority flows one way: the **server** derives identity + authorizes; the worker is least-privilege and may
claim nothing but `PARTIAL_CLOSE_TRADE`; the bridge independently re-verifies. No customer-supplied identity
authority at any hop.

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

## Status / approval
DARK engineering design. The endpoint implementation, ADR ratification, DEMO arming, the dedicated worker
grant, deployment and LIVE all remain separate explicit Sponsor gates. `DEMO_CERTIFIED` /
`LIVE_EXECUTION_CERTIFIED` = false; actual live availability = 0%.
