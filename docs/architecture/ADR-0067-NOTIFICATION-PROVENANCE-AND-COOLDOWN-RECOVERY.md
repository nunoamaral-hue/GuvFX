# ADR-0067 — Notification provenance (verified-execution-link) + monitored-ingest cooldown recovery

- **Status:** Accepted (correctness/safety fix; read path + notification gating; no order/execution change).
- **Date:** 2026-10-30
- **Workstream:** Dashboard closure corrections + monitored-ingestion notification safety (P0)
- **Relates to / refines:** ADR-0065 (monitored deal-history ingestion), the close-monitor → outcome-router →
  dispatch pipeline (`execution/close_monitor.py`, `execution/outcome_router.py`, `execution/notifications/`),
  the customer-notification outbox (`customer_notifications/`), `execution/monitored_ingest.py`.

## Context

ADR-0065 ingested broker **history** for monitoring-only accounts so the dashboard could show their closed trades.
A side effect surfaced a latent defect: the trade-result notification pipeline treated **any** closed `Trade` row
as a GuvFX strategy execution. `close_monitor._resolve_linkage` fails to a blank link when no `SignalExecutionPlan`
matches, and `TradeOutcomeRecord.is_delivery_candidate` was set from `outcome == WIN` **alone**; the customer path
(`enqueue_trade_outcome`, fired by a post-save signal) likewise notified for every outcome and `_trade_context`
defaulted `strategy="GuvFX"` when no plan was found. As a result, **externally-executed historical trades were
announced as GuvFX automation** — the stakeholder "results card" path transmitted **39** false WIN cards
("Automated strategy", "Trade completed automatically · No manual intervention") for monitoring-only accounts
(**37** for account #46, **2** for #49). This is false provenance: a `Trade` row is **not** evidence that GuvFX
executed it.

Separately, the monitored-ingest cooldown (ADR-0065) backed off an account for the full 180-minute window after a
transient failure even once the bridge recovered and a sync succeeded — so a recovered account waited out the window
before scheduled coverage resumed.

## Decision

### 1. A notification requires a VERIFIED same-account GuvFX execution link
The authoritative signal is `TradeOutcomeRecord.execution_job` — set by `close_monitor._resolve_linkage` only from a
`SignalExecutionPlan` **belonging to the trade's own account** whose leg carries an `ExecutionJob`. Gates, keyed on
that link:

- **Linkage is account-scoped** (`_resolve_linkage`): a correlation id / `WAY{plan}L{leg}` comment that resolves to a
  **different** account's plan is **not** a causal link — fail closed to "no link" (cross-account prevention).
- **Stakeholder path** (`close_monitor`): `is_delivery_candidate = (outcome == WIN AND execution_job is not None)`.
  An externally-executed / historically-ingested / monitoring-only WIN (`execution_job is None`) is **never** a
  delivery candidate, so `outcome_router` creates no `NotificationCandidate` and nothing is dispatched.
- **Customer path** (`enqueue_trade_outcome`): return early (create no outbox row) when `outcome.execution_job_id is
  None`. Re-running ingestion stays notification-idempotent.
- **Dispatch boundary** (`RealTelegramTransport.deliver`): defence in depth — refuse any candidate whose outcome
  carries no execution link, mirroring the existing WIN-only re-check, so a card can never ride on an external trade.

The outcome record is still **recorded** for internal accounting; only its *notification eligibility* is gated.
Externally-observed broker activity is **not** notified at all in V1; if ever supported it must use a distinct event
type and "Broker activity observed" wording with external-origin disclosure — **not enabled here**. Existing
historical Telegram messages are left untouched (not deleted, edited, or resent).

### 2. Cooldown recovery (monitored ingest)
`enqueue_monitored_deal_syncs` backs off an account only when its **most recent** monitored probe FAILED within the
window. A subsequent successful identity-verified sync (a recovered bridge) is then the latest, so the backoff
**clears immediately** and the account resumes normal scheduled coverage. Account-scoped (one account's failure never
suppresses another); rate limiting and ongoing-failure backoff are preserved.

## Consequences

- Strategy-result notifications (either channel) fire only for a WIN closed trade with a verified same-account GuvFX
  execution job — the exact #46/#49 false cards can no longer be produced. Genuine GuvFX executions notify exactly
  once, correctly attributed. Deposits/credits/balance rows never become trades, so they never notify.
- Recovered monitored accounts resume scheduled ingestion without waiting out the 180-minute window.
- No change to any order/execution path; execution remains OFF.

## Rollback

Revert the PR. The gates are additive guards; reverting restores the prior (over-permissive) behaviour. No data
migration; no stored rows are modified.

## Evidence

- `execution/tests_notification_provenance.py` — the packet's seven cases: external backfill inserts rows / notifies
  zero; idempotent rerun zero; linked execution → exactly one correctly-attributed, dispatch-eligible candidate;
  cross-account cannot notify; balance/credit rows never become trades; dispatch refuses a card without a link.
- `execution/tests_close_monitor.py` (linked vs unlinked vs cross-account delivery-candidate), `customer_notifications`
  (linked customer notification still sent), `execution/tests_monitored_ingest.py` (cooldown clears on recovery,
  still backs off on a fresh failure, account-scoped). Full `execution` + `customer_notifications` + `intelligence`
  suites green.
