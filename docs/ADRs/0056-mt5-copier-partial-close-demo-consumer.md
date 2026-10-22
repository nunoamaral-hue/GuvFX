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

## Status / approval
DARK engineering design. The endpoint implementation, ADR ratification, DEMO arming, the dedicated worker
grant, deployment and LIVE all remain separate explicit Sponsor gates. `DEMO_CERTIFIED` /
`LIVE_EXECUTION_CERTIFIED` = false; actual live availability = 0%.
