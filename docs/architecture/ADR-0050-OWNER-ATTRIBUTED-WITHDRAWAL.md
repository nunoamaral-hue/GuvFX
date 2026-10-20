# ADR-0050 — Owner-Attributed, Account-Optional Withdrawal

- **Status:** Proposed / DARK (schema + correlation behind `BROKER_WITHDRAWAL_CORRELATION_ENABLED`,
  default OFF; new migration `broker_intelligence/0016` unapplied in production).
- **Date:** 2026-10-20
- **Packet:** GUVFX_WV1_BOARD_DEMO_CRITICAL_PATH (§3 parser, §4 attribution).
- **Related:** ADR-0031 (frontend source of truth / parity), the WP2 Broker-Intelligence schema
  (`broker_intelligence.models`), the WP5 correlation engine (`broker_intelligence.correlation`).
- **Decision class:** AMBER — it changes an established data-model/correlation contract (the
  `Withdrawal` projection), so it is recorded here rather than made as an in-passing edit. It is
  additive and behaviour-preserving while the flag is OFF.

## Context

The genuine TradersWay pilot withdrawal (2026-10-19), already authenticated and content-hash-intact in
the production evidence store, is **owner-attributed but account-UNRESOLVED**: the member's connected
mailbox received the broker's external-withdrawal REQUEST and COMPLETION (linked by one authenticated
broker reference), but the TradersWay trading account the withdrawal was made from is **not registered**
in GuvFX. The packet (§4) requires this case be surfaced *as* owner-attributed / account-unresolved —
never bound to an unrelated account, never invented.

The current projection cannot represent it:

- `Withdrawal.trading_account` is a **non-null** `PROTECT` FK with **no owner field** — the record is
  keyed entirely on a resolved GuvFX account (`models.py` Withdrawal; partial-unique
  `uniq_withdrawal_account_reference` on `(trading_account, broker_reference_id)`).
- `correlation.correlate_event` **hard-skips** any event with `trading_account_id is None`
  (`OUT_SKIP_NO_ACCOUNT`), and every create/lookup keys on `trading_account_id`.
- `BrokerEvent` **already** models the needed split: `owner` (FK, `SET_NULL`, nullable) records the
  mailbox owner *independently of* `trading_account` (nullable) — "an event can be owner-attributed even
  when the specific transaction trading-account is UNRESOLVED." The event layer is the precedent.

Separately, the genuine external-withdrawal COMPLETION email ("Payment request #N … status changed from
Confirmed to Completed") carries **no payment rail and no direction wording** — so the parser (ADR-scope
§3, PR #530) honestly classifies it `transaction_category = UNKNOWN`. Its externality is provable only by
the shared authenticated broker reference #N to the REQUEST (which carries the positive
`Payment method: <rail>` evidence). The correlation engine today correlates **only** `EXTERNAL_WITHDRAWAL`
events, so it would never advance a withdrawal from such a completion.

## Decision

Extend the `Withdrawal` projection to be **owner-attributed and account-optional**, mirroring the
`BrokerEvent` owner/account split, and extend correlation to project and complete owner-only withdrawals —
**without ever weakening the economic safety invariant that a withdrawal is created only from positive
external-withdrawal evidence.**

1. **Schema (`broker_intelligence/0016`, additive, reversible).**
   - `Withdrawal.trading_account` → `null=True, blank=True` (keep `PROTECT`). Non-destructive
     `DROP NOT NULL`; the `Withdrawal` table carries no immutability trigger, so no trigger interferes.
   - Add `Withdrawal.owner` — `FK(AUTH_USER_MODEL, on_delete=SET_NULL, null=True, blank=True,
     related_name="owner_withdrawals")`, copying `BrokerEvent.owner` so an operator `User.delete()` purge
     decays attribution to NULL without destroying the financial-evidence row.
   - Replace the single partial-unique with **two**, closing the Postgres `NULLS DISTINCT` idempotency
     hole for account-null rows (the same owner-scoping idiom migration 0014 used for `BrokerEvent`):
     - account-scoped: `(trading_account, broker_reference_id)` where `broker_reference_id > '' AND
       trading_account IS NOT NULL`;
     - owner-scoped: `(owner, broker_reference_id)` where `broker_reference_id > '' AND trading_account
       IS NULL AND owner IS NOT NULL`.
   - Add `Index(["owner", "status"])` to match the existing `(trading_account, status)` index.

2. **Correlation — owner-scoped projection.** Relax the `OUT_SKIP_NO_ACCOUNT` gate so an
   `EXTERNAL_WITHDRAWAL` event with an owner but no `trading_account` still projects an owner-attributed,
   account-UNRESOLVED `Withdrawal`; set `owner_id = event.owner_id` at every create site; owner-scope the
   reference/heuristic lookups when `trading_account_id is None`; rely on the new owner-scoped partial-unique
   for idempotency (the existing `IntegrityError`-savepoint recovery pattern is unchanged).

3. **Correlation — advance-only completion by reference.** Add a bounded, **create-never** path: a
   COMPLETED-lifecycle event that carries a broker reference but is **not** itself `EXTERNAL_WITHDRAWAL`
   (the neutral completion, category UNKNOWN) may **advance** an *existing* external `Withdrawal` matched by
   `(owner-or-account, broker_reference_id)` to COMPLETED — and if no such withdrawal exists it is left
   UNRESOLVED, **never** fabricated into a record. This encodes "externality is inherited from the request
   by the authenticated reference, never re-derived from the completion's own text."

## Safety invariant (preserved, strengthened)

- **A `Withdrawal` is only ever CREATED from an event with positive external-withdrawal evidence** — i.e.
  an `EXTERNAL_WITHDRAWAL` event, which the parser emits only on the structured `Payment method: <rail>`
  label gated by a withdrawal-direction lifecycle. Completions (and any non-external lifecycle event) can
  only **advance** an already-created external withdrawal; they can never create or inflate one. This holds
  even if a deposit or internal completion re-used the identical "Payment request … Completed" template,
  because its reference will not match any external withdrawal and the create-never rule applies.
- **No account is ever invented.** An account-UNRESOLVED withdrawal stays account-UNRESOLVED; attribution
  is to the mailbox owner only.
- Under-claiming (a real external withdrawal read as UNRESOLVED) remains safe; over-claiming
  (metric inflation) remains the forbidden error and is structurally prevented.

## Darkness & preservation

- The new migration `0016` is **DARK and unapplied in production**. Production `broker_intelligence`
  remains at schema 0010; 0011–0015 are likewise unapplied. `DATABASE_RECOVERY_READINESS = BLOCKED`
  (no off-host verified recovery point) — the rollout checklist forbids applying **any** migration in prod
  until that gate clears (Sponsor + cost-gated). The Board demo therefore runs on an **isolated demo
  database** where migrations apply freely; prod is untouched.
- With `BROKER_WITHDRAWAL_CORRELATION_ENABLED` OFF, the live correlation pass does not run, so no
  `Withdrawal` rows are created or advanced by this change in any live environment.
- The public cross-member aggregation (`broker_public`) must derive owner from `Withdrawal.owner`
  (not `trading_account.user_id`) and **exclude** account-UNRESOLVED rows from public broker stats — an
  account-unresolved withdrawal cannot be attributed to a specific broker account and must not feed a
  broker's public metrics. (Handled in the API/UI PR.)

## Consequences

- Member withdrawal history/metrics become owner-scoped (account-owned rows ∪ owner-attributed account-null
  rows), with an explicit "account unresolved" presentation distinct from correlation-ambiguous UNRESOLVED.
- `Withdrawal.trading_account` nullability has **no** reverse-accessor breakage (no code reads
  `account.withdrawals`), but readers that dereference `w.trading_account` must guard the None path.
- The schema and correlation changes **must co-deploy** (code is incorrect against the old schema), and
  only after the DB-recovery GO/NO-GO gate clears for any production rollout.
