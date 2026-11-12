# ADR-0093 — Staff-gated completion of a MANUAL_STAFF withdrawal + Vantage completion parsing (reference-less)

- Status: Accepted (repository-only; DARK — command gated, not applied in production)
- Date: 2026-11-12
- Packet: GFX-WITHDRAWAL-VANTAGE-COMPLETION-CORRECTION-063
- Extends: ADR-0079 (Stream B withdrawal lifecycle), ADR-0090 (Vantage reference-first correlation + fail-closed
  `requires_reference`), ADR-0092 (staff-gated manual withdrawal correlation)

## Context

ADR-0092 projects exactly one **`REQUESTED` / `MANUAL_STAFF`** `Withdrawal` from two or more reference-less
unknown-sender discovery candidates, with `trading_account` deliberately UNRESOLVED and `amount`/`currency` left as the
evidence states them (often null/blank). By design that record never advances automatically: the autonomous
correlation engine **excludes `MANUAL_STAFF` rows** ("the engine never touches a staff record", `correlation.py`), and
the only completion path is reference-keyed (`correlate_completion_event` returns UNRESOLVED without a stable
`broker_reference_id`).

In production (withdrawal #4, owner 29) the genuine Vantage **completion** email ("…has been processed successfully")
then arrived and nothing advanced the withdrawal. Diagnosis (read-only, privacy-safe boolean phrase probe on the
retained evidence) established three distinct gaps:

1. **No Vantage completion template.** The completion email states "processed successfully" but does **not** place
   "processed" adjacent to "withdrawal", so the shared `WITHDRAWAL_COMPLETED` anchors miss it and it classifies as
   `WITHDRAWAL_REQUESTED` (both prod events #16/#17 are REQUESTED, amount 30).
2. **The external account login is never extracted.** The Vantage text path only captured an internal-transfer clause,
   so the stated login (ending `5017`) was never read and the owner's Vantage `TradingAccount` was never bound.
3. **The record is `MANUAL_STAFF` / reference-less**, so neither the reference path nor the heuristic path (which
   excludes `MANUAL_STAFF`) can ever advance it.

The authenticated owner visual acceptance (packet 062) therefore **failed**: the withdrawal rendered its broker label
(ADR-0092) but stayed REQUESTED / pending / account-unresolved while genuine completion evidence (account `…5017`,
amount `30`, ~19s after submission) existed.

## Decision

### 1. Vantage completion parsing (per-broker, additive)
The `vantage/v1` adapter gains, **opt-in per broker** (existing brokers byte-for-byte unchanged):
- a **perfective completion upgrade**: a non-terminal base classification is upgraded to `WITHDRAWAL_COMPLETED` when
  the text carries a PERFECTIVE `(has been|have been|was|were|been|is now) processed successfully` statement and there
  is **no deposit/funding context** (deposit-vetoed). Future ("will be processed successfully") and habitual/
  promotional ("are processed successfully") lack the perfective auxiliary and never match; terminal outcomes
  (rejected/failed/cancelled/expired) are never upgraded. Under-claiming is safe (a missed completion falls to staff
  review), so the veto is deliberately broad.
- a **single stated-login extraction** from the plain-text body (`account <digits>` with no `:` separator), kept ONLY
  when exactly one distinct login is present; `\d{4,12}\b` structurally refuses a 13+ digit card/bank PAN, and the
  owner-scoped resolver still binds it only on an EXACT account-number match.

These anchors were verified privacy-safely against the genuine retained evidence (boolean phrase match only — no raw
body was read into any output, commit, test, log or manifest).

### 2. Staff-gated completion exception path (extends the ADR-0092 invariant)
A **broker-neutral, staff-gated** completion of ONE `MANUAL_STAFF` withdrawal from evidence that is already retained
and already linked to it (one of its `StaffWithdrawalCorrelation` candidates):
- Pure service `staff_completion.plan_staff_completion` / `apply_staff_completion` + a dry-run-by-default command
  `complete_staff_withdrawal` (`--apply` to write).
- Fail-closed preconditions: active-staff reviewer; withdrawal is `REQUESTED` + `MANUAL_STAFF` + `provenance=REAL` +
  account-UNRESOLVED + not-yet-completed + owned by the named owner; the completion candidate is one of the
  withdrawal's APPLIED correlation's candidates and post-dates the request within a bounded window.
- The candidate's **immutable stored blob is re-parsed** (read-only) with the current parser: it MUST classify as
  `WITHDRAWAL_COMPLETED` for the selected broker, carry the expected amount, and state a login that resolves to
  **exactly one owner-scoped account** whose suffix matches the expected suffix (never cross-owner, never suffix-only —
  `resolve_transaction_account` matches the FULL account number owner-scoped and returns None on 0/>1).
- On apply (transaction + row lock, re-validated under the lock): set `trading_account`, `status=COMPLETED`,
  `completed_at` = the completion evidence's trusted timestamp (never `now`), and `amount` (when absent). `requested_at`
  is preserved; `currency` stays as the evidence states it (`""` when unknown — **never invented**); `completed_event`
  stays `None` (the MANUAL_STAFF evidence model links evidence via the correlation candidates, not a BrokerEvent FK).
  Idempotent: a re-run on an already-completed, account-resolved withdrawal is a no-op.

### 3. Projection / UI truthfulness
The read-only projection already masks the resolved account (`••••` + last 4), drops the "Account unresolved" warning
once an account is bound, and sums completed amounts **strictly per-currency** (unknown-currency amounts are never
summed or converted). The member UI now renders a known amount whose currency is unknown as an explicit neutral
`<amount> · currency unavailable`, rather than hiding it or coercing a currency.

### Why the autonomous engine is deliberately NOT changed (the key architectural decision)
A reference-less completion could, in principle, be auto-correlated by owner + account + amount + time-window. We
**reject** wiring that into the autonomous engine: it would weaken the ADR-0092 invariant (the engine never
auto-advances a human-owned staff record) and risk a wrong merge on hint-matching alone. A `MANUAL_STAFF` / reference-
less withdrawal advances to COMPLETED **only** through this explicit, human-gated command, backed by re-parsed
evidence and fail-closed checks. This is the fail-closed-to-staff posture ADR-0090/0092 established, extended to the
completion stage. The parser correction is additive and benefits any future Vantage completion (held for staff review
when reference-less), without any engine policy change.

## Scope / non-goals
- Repository-only; **DARK** (command gated). No migration — every `Withdrawal` field used already exists and no new
  enum value is introduced. The `vantage/v1` adapter is corrected in place (the completion stage is added to the same
  ADR-0090 adapter, consistent with how that adapter was built incrementally); parser name/version are unchanged.
- No production mutation, no deploy, no mailbox/cursor action, no money movement, no broker contact, no public-
  statistics change, no Copy Trading / TIM / MT5 interaction.

## Review hardening (independent adversarial review, 2026-11-12)
A multi-dimension adversarial review (false-completion, account-safety, duplicate/idempotency, fabrication, privacy,
regression) with per-finding verification confirmed 8 real defects in the first draft; all were fixed:
- **(HIGH) over-claim** — the perfective phrase matched anywhere in the mail, so an unrelated "…processed successfully"
  line (identity verification / refund / internal transfer / card payment) completed a still-pending withdrawal. Fixed
  by anchoring `_PROCESSED_SUCCESSFULLY` to a **word-bounded withdrawal noun in the same sentence**, future/modal/
  negation-guarded (so "reFUND", a cross-sentence line, "will/should have been", and "has not been" all fail).
- **(MEDIUM×2) future-perfect / modal / bare auxiliary** — dropped bare `been`/`were` from the auxiliary; and the
  SHARED `WITHDRAWAL_COMPLETED` rule's optional id slot was restricted to NUMERIC (so "withdrawal **will** have been
  processed" no longer reads a modal as a withdrawal id). Verified non-regressing across Pepperstone/Dominion/TradersWay.
- **(LOW) account/PAN** — `_SINGLE_ACCOUNT` now refuses a grouped-PAN fragment (`(?!\s*[-\d])`).
- **(LOW) empty suffix** — a blank `--expected-account-suffix` is refused (`EXPECTED_SUFFIX_REQUIRED`), never silently
  disabling the cross-check.
- **(LOW) fabricated 0-duration / misstated amount / preview divergence** — the idempotent plan no longer coerces a
  `None` duration to `0`; the command's before/rollback use the ACTUAL prior amount/currency (masking from the resolved
  account's real last-4); and `apply` now persists an evidenced currency when the withdrawal has none, so the dry-run
  preview matches what is written.
Mutation-killing regression tests were added for each. The anchor was re-verified privacy-safe against the genuine
retained evidence (match preserved, all attack classes rejected).

## Consequences — gated production repair sequence (NOT executed here)
1. Sponsor **merge GO** (exact-head CI 5/5; auto-merge OFF).
2. **Rollback-first** backend deploy of the corrected parser + command.
3. **Dry-run** `complete_staff_withdrawal` against withdrawal #4 (owner 29, broker profile Vantage, the completion
   candidate, expected suffix `5017`, expected amount `30`) — must print the full PLAN and the resolved account
   `••••5017`, with no mutation.
4. Sponsor **GO for `--apply`** (the only production data mutation; fully reversible — the command prints the exact
   BEFORE state and rollback).
5. **Authenticated owner visual recheck** on the live Withdrawals page.
