# ADR-0096 — Copy Trading event-driven post-fill follower-ticket binding (+ the #46 real-time source)

- **Status:** Accepted (repository-only / DARK, default-OFF system). Merge, deployment, the account-scoped
  read-only follower-positions enablement, host emitter installation, secret provisioning, flag enablement,
  live authorization, and any Sponsor-present live demo are **separate Sponsor gates**. No deploy, no host
  contact, no secret provisioning, no flag enablement, no broker order is part of this packet.
- **Packet:** GFX-COPIER-REALTIME-SOURCE-AND-RECONCILE-073.
- **Date:** 2026-11-12.
- **ADR number:** 0096 — **renumbered off 0095** (honest record). This packet first took 0095; while it was in
  flight the broker_intelligence PR #643 (`ADR-0095-vantage-currency-field-extraction`, which had itself
  renumbered off this workstream's ADR-0094) **merged first and holds 0095 on `main`**. Supersede-never-overwrite:
  the merged ADR is authoritative, so this one moves to the next free number, 0096 (0093/0094/0095 are all merged
  on `main`). Filename/number/references only — no behaviour, test logic or decision changed.
- **Builds on / realises:** ADR-0094 (the synchronous real-time trigger) — specifically its **Known limitation
  #2**, which records that follower-ticket linkage is *not* on the fast path and names "a fast, post-fill,
  event-driven reconcile" as the follow-up. This ADR *is* that follow-up. Also ADR-0087 (the mutation-dispatch
  SUCCESS → `EXECUTION_VERIFIED` write this mirrors), ADR-0057 (the LIVE OPEN producer, which mints the
  `PLACE_ORDER` and leaves the `CopierReplication` at `EXECUTION_REQUESTED` with `follower_ticket` NULL),
  ADR-0083 / ADR-0086 (the LIVE MODIFY / FULL-CLOSE producers + dispatcher, which fail closed unless the
  replication is `MATCHED` with a non-null `follower_ticket`), ADR-0074 (the positions-read `reconcile`, now a
  fallback), and ADR-0077 (the master-side host emitter — the real-time SOURCE). Nothing in those ADRs is
  modified; this ADR only adds a new *linker* at an existing convergence point and documents the source
  activation chain. No new flag, no new model, no migration.

## Context — the two gaps the 072 pre-demo readiness measured

The GFX-COPIER-REALTIME-HYBRID-PREDEMO-READINESS-072 assessment returned **NOT_READY** for two concrete,
independent reasons:

1. **No real-time master SOURCE is wired to #46.** ADR-0094 made the signed ingestion endpoint the primary
   trigger, but on production #46 (55442 / `Tradersway-Live`) every master event is still POLL-sourced
   (`ingested_revisions_total = 0`): the ADR-0077 emitter exists in the repository as a DARK bundle
   (`deploy/copier-emitter/`) but is not installed or armed on the host, so nothing *drives* the fast path. The
   real-time chain has a source-shaped hole at its head.

2. **The fast path had no post-fill binding of the follower ticket.** ADR-0057's LIVE OPEN producer mints a
   `PLACE_ORDER` `ExecutionJob` and leaves the replication at `EXECUTION_REQUESTED` with `follower_ticket` NULL.
   The only thing that bound `follower_ticket` was the ADR-0074 positions-read `reconcile`, which a **LIVE**
   follower bridge refuses with `account_not_demo` unless the route-scoped read flag
   (`COPIER_FOLLOWER_READONLY_POSITIONS_ENABLED`, ADR-0094) is armed. So on the real-time LIVE path a copied
   OPEN could not be linked, and the governed MODIFY / FULL-CLOSE — which both require a `MATCHED`, non-null
   `follower_ticket` — stayed fail-closed. The copied position could be opened but not automatically managed.

## Decision — bind the follower ticket from the OPEN fill RESULT, event-driven

The follower broker ticket becomes known the instant the hosted bridge FILLS the OPEN order and reports the
outcome to the job-completion endpoint `ExecutionJobViewSet.complete`, which writes the ticket into
`ExecutionJob.result["ticket"]`. That endpoint is the **single SUCCESS convergence point** for a hosted order
(there is no Django signal on `ExecutionJob`). `trade_copier.open_result_binding.bind_open_result(job)` is
called **fail-safe** from that endpoint, immediately after the job's own completion write, for a copier
`PLACE_ORDER`:

- **SUCCESS with an integer `result["ticket"] > 0`** → bind `follower_ticket` + `broker_result`, set
  `reconciliation_status = MATCHED`, and transition `EXECUTION_REQUESTED → EXECUTION_VERIFIED` (the exact write
  the ADR-0087 mutation-dispatch SUCCESS path already makes). MODIFY / FULL-CLOSE can now target the follower.
- **SUCCESS with no / non-integer / non-positive ticket** → **UNKNOWN**: bind NOTHING, never guess a ticket. The
  ADR-0074 positions-read reconcile remains the fallback, and the bound-ticket precondition keeps MODIFY/CLOSE
  fail-closed (contained) meanwhile.
- **FAILED** → mark the replication `EXECUTION_FAILED` (no ticket, no lifecycle op). The ADR-0083 rule stands:
  an OPEN that failed is never re-placed.

No `/mt5/positions` read is performed, so the binding is unaffected by the `account_not_demo` wall. The read is
of the order's own result, by the same backend that minted the order.

### Determinism, idempotency, fail-closed

- **Scoped to the exact replication the job belongs to.** The job's `payload["copier_replication_id"]` selects
  the replication under `select_for_update`, and the bind is refused unless that replication's
  `execution_job_id` is NULL or equals this job (`job_replication_mismatch`) — a completion can never bind a
  ticket onto a sibling route's or another tenant's replication.
- **Idempotent.** A replay reporting the SAME ticket is a no-op success.
- **Conflict fails closed.** A DIFFERENT ticket on an already-bound replication is refused — the existing ticket
  is NEVER overwritten — and a governed, de-duplicated, secret-free reconciliation alert is raised
  (`failure_alerts.raise_copier_alert`).
- **Transactional.** The read-modify-write runs inside `transaction.atomic()` with the row locked
  (`ATOMIC_REQUESTS` is False in this project).
- **Fully fail-safe.** Any error is swallowed + logged; a binding fault can never break the job-completion HTTP
  response the bridge depends on, nor the job's own status/result write (already committed before the hook
  runs).

### Why the hook is intentionally NOT behind a new flag

A copier `PLACE_ORDER` exists **only** because the gated ADR-0057 producer created it — which requires the full
live-arm stack and a human-created `CopierLiveAuthorization`. So the hook is **inert in a DARK system** (there
are no copier orders to bind) and MUST run for a real copier order — *including if the operator disarms
mid-flight* — so a placed position can always be reliably MODIFY/CLOSE-managed and contained. Binding is a
**recording / containment act, never an order path**: it creates no order, contacts no broker, and consumes no
authorization allowance (the OPEN already consumed it). Gating it would create a state where a live position
exists that the backend has deliberately chosen not to be able to close — the opposite of containment.

## Consequences

- **MODIFY / FULL-CLOSE no longer hard-depend on `COPIER_FOLLOWER_READONLY_POSITIONS_ENABLED` for the common
  path.** When the OPEN fills and reports a ticket (the normal case), binding happens from the result and the
  governed mutations can fire. The positions-read reconcile becomes the **fallback** for the UNKNOWN
  (no-ticket-in-result) case and for audit/restart recovery. The route-scoped read authority remains valuable
  (it lets the fallback link an UNKNOWN OPEN, and supports read-only follower monitoring) but is no longer on
  the critical path for a well-behaved fill.
- With the system DARK (no armed copier orders), **nothing new runs** — there are no `PLACE_ORDER` copier jobs
  to complete, so the hook returns `not_copier_order` / `not_place_order` and is byte-inert.
- Live follower execution remains **human-gated**: binding grants no execution authority; MODIFY/CLOSE still
  independently re-check every gate + the admission + the authorization window under their own locks.

## The #46 real-time SOURCE — reuse, not rebuild

Gap 1 (no master-side source) needs **no repository change**. The ADR-0077 emitter
(`trade_copier/bridge_emitter.py` + `trade_copier/emitter/app.py`, bundled under `deploy/copier-emitter/`)
signs its emissions with the **same** `trade_copier.event_ingestion` module the ingestion endpoint verifies
with, so the envelope is byte-for-byte compatible. `trade_copier/tests_emitter_ingest_contract.py` pins this at
the signed-verifier boundary for the exact #46 / 55442 / `Tradersway-Live` lifecycle: a known-good
OPEN→MODIFY→CLOSE envelope verifies, and any post-signing mutation of the tenant (e.g. retargeting account 46→48),
payload, nonce or signature fails closed.

What remains for Gap 1 is therefore entirely **host-side operational** and Sponsor-gated, documented in
`docs/COPIER_REALTIME_SOURCE_ACTIVATION.md`: install the DISABLED scheduled task on the host, provision the
ingestion HMAC secret (by env-var NAME; min 32 chars; fail-closed), point it at the #46 read-only bridge and
the backend ingestion endpoint, arm the emitter's own delivery gate AND the backend
`COPIER_IMMEDIATE_DISPATCH_ENABLED`, then enable the task. Installation alone cannot cause a broker order: the
emitter adapter has no order method, every emitter gate defaults OFF, and the backend path stays DARK until its
own flags + a human authorization are present.

## Known limitations (documented; reviewed — not safety gaps)

- Binding depends on the hosted bridge reporting a truthful integer ticket for a filled OPEN. A SUCCESS that
  omits the ticket is treated as UNKNOWN (never guessed) and left for the positions-read fallback; until the
  fallback (or a later truthful completion) links it, MODIFY/CLOSE stay fail-closed for that replication. This
  is containment, not a gap.
- **Order vs position ticket on a netting follower (adversarial review MEDIUM-1; fails SAFE).** The governed
  MODIFY/FULL-CLOSE resolve the follower by its MT5 **position** ticket; the signal bridge's normal OPEN result
  reports the opening **order** ticket. For a fresh market open into a **position-clean** follower — which the
  governed #46→#48 route requires (exact pair, one order, one position) — the position ticket equals the order
  ticket, so the result-derived bind is correct. The one divergence is a **netting follower already holding a
  same-symbol position** (forbidden by the route's position-clean precondition): there the bound order ticket
  resolves to NOTHING at `positions_get` (MT5 tickets are globally unique per server), so MODIFY/CLOSE fail
  CLOSED — never the wrong position — and the ADR-0057 operator-manual-close fallback applies. `_result_ticket`
  prefers an explicit `position` / `position_ticket` field whenever a bridge surfaces one (the lost-ACK recovery
  result already carries the position ticket); surfacing the position ticket in every bridge OPEN result is a
  bridge-side follow-up, not a backend change.
- A conflicting fill ticket on an already-bound replication is logged **unconditionally** (not only through the
  default-OFF governed alert) and NEVER overwrites the bound ticket (adversarial review MEDIUM-2).
- The binding is proven on deterministic in-DB fixtures (no real bridge/broker). The live result→bind path is
  exercised only when the feature is enabled end-to-end with a host emitter + authorization present (separate
  gated steps).
- `PARTIAL_CLOSE` is unchanged — it keeps its dedicated worker path (ADR-0094).

## Separately gated steps (NOT in this packet)

1. Merge (Sponsor GO). 2. DARK deployment. 3. Host emitter installation + secret provisioning. 4. Account-scoped
read-only-positions enablement (+ backend recreate). 5. Flag enablement (`COPIER_IMMEDIATE_DISPATCH_ENABLED` +
the per-worker stack). 6. Human `CopierLiveAuthorization` + signed host admission. 7. Sponsor-present bounded
live demo.
