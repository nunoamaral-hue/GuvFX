# ADR-0079 — Explicit withdrawal lifecycle states + Pepperstone / Dominion Markets / Vantage adapters

- **Status:** Accepted (Amber). Repository-only, DARK (default-OFF), SYNTHETIC-ONLY. Unmerged pending a separate
  Sponsor merge GO.
- **Decision class:** **Amber** — it adds lifecycle *states* and *stage timestamps* to the `Withdrawal` projection and
  **extends the correlation scan surface** with a new advance-only pass (shared structure). It does NOT change the
  external-evidence rule, dedup, idempotency, owner/tenant isolation, evidence immutability, erasure, FX, or any
  Copy-Trader / audit-control state. Schema change is strictly additive (new nullable columns + a widened
  `status` width + three DARK config rows).
- **Relates:** builds on ADR-0050 (owner-attributed withdrawals), ADR-0059 (classification revision / effective
  category), ADR-0060 (ref-less requesting lifecycle opens a withdrawal), ADR-0061 (duration = observed email
  interval). Part of GFX-BROKER-INTELLIGENCE-FOUNDATION-001, Stream B. Stream A was ADR-0078.

## Context
The withdrawal projection collapsed the broker lifecycle into a flat `REQUESTED → PROCESSING → COMPLETED` plus
terminals, with `CONFIRMATION_REQUIRED` and `CONFIRMED` folded into `REQUESTED`. Members whose broker uses a
**card confirmation-required** flow (observe → confirm required → confirm → process → complete) could not see which
stage a withdrawal was actually at, and there were no `EXPIRED` / `FAILED` outcomes for a confirmation that lapsed or
a payout that failed. The event vocabulary already carried `WITHDRAWAL_CONFIRMATION_REQUIRED` / `WITHDRAWAL_CONFIRMED`;
the gap was in the *projection* (states + stage timestamps) and in two missing terminal events. Separately, only
TradersWay (#1) and IS6 (#2) had deterministic email adapters; Pepperstone, Dominion Markets and Vantage had none.

A genuine broker follow-up email (confirmation / processing / expiry / rejection / completion) frequently carries **no
withdrawal-method ("rail") line**, so its externality cannot be re-asserted from its own text — it classifies as
`UNKNOWN`. The pre-existing engine advanced an existing external withdrawal from a neutral `UNKNOWN` email **only for
`COMPLETED`** (pass 2, `correlate_completion_event`). A neutral `PROCESSING` / `EXPIRED` / `REJECTED` follow-up was
therefore dropped by both passes, so the new intermediate stages and the new terminals could never be reached from a
realistic (rail-less) follow-up email. This was found by the Stream B scenario tests and is the reason an advance-only
neutral pass is required (below), not merely new status enum values.

## Decision
1. **Explicit states.** `Withdrawal.Status` gains `CONFIRMATION_REQUIRED`, `CONFIRMED`, `EXPIRED`, `FAILED`
   (REQUESTED / PROCESSING / COMPLETED / REJECTED / CANCELLED / PENDING / UNRESOLVED retained). The headline ladder is
   `REQUESTED → CONFIRMATION_REQUIRED → CONFIRMED → PROCESSING → COMPLETED`, with `REJECTED` / `CANCELLED` / `EXPIRED` /
   `FAILED` as terminals. Monotonic rank: `UNRESOLVED 0 < {PENDING,REQUESTED} 1 < CONFIRMATION_REQUIRED 2 <
   CONFIRMED 3 < PROCESSING 4 < {COMPLETED,REJECTED,CANCELLED,EXPIRED,FAILED} 5`. Status only ever advances to a
   **strictly higher** rank; all terminals share the top rank so one terminal never flips to another. The relative
   order of pre-existing statuses is preserved, so no historical record regresses.
2. **Stage timestamps (additive, nullable).** `confirmation_required_at`, `confirmed_at`, `processing_started_at`,
   `last_status_change_at`, and `funds_received_at`. Each stage anchor is stamped **backfill-once** from the first
   observed event of that stage, **independent of the monotonic headline status**, so a late out-of-order stage email
   records *when that stage's evidence was observed* without regressing the status. Anchors use the broker-stated
   time when present else the observed email `Date` (ADR-0061). `funds_received_at` is reserved for future
   independent fund-arrival attestation and is **never inferred** from a completion email (left NULL otherwise).
   `completed_at` remains stamped **only** when the row is actually `COMPLETED`, so a rejected / cancelled / expired
   withdrawal never shows a completion time or a positive duration.
3. **Two new neutral advance-only events.** `WITHDRAWAL_EXPIRED`, `WITHDRAWAL_FAILED` map to the `EXPIRED` / `FAILED`
   terminals.
4. **Neutral advance-only correlation pass (pass 2b).** `correlate_neutral_advance_event` advances — **never
   creates** — an existing external `Withdrawal` matched by the shared authenticated broker reference, for a neutral
   (effective-category `UNKNOWN`) **non-completion** lifecycle event (confirmation-required / confirmed / processing /
   approved / expired / failed / rejected / cancelled). It is the non-`COMPLETED` sibling of the existing
   `correlate_completion_event`, with identical fail-closed + idempotent guarantees (event row locked, mutable
   `correlation_status` re-read, per-owner erasure lock). `run_correlation` restricts this pass to effective-category
   `UNKNOWN`, so an affirmatively non-withdrawal event (`INTERNAL_TRANSFER` / `DEPOSIT`) is **never scanned**; it runs
   after pass 1 (so the opening event created the record) and **before** pass 2 (so an explicit negative terminal is
   not overridden by a stray later completion for the same reference). Externality is still asserted **only** by the
   opening event's positive rail evidence, so no advance pass can create or inflate a withdrawal.
5. **Three DARK deterministic adapters.** `parsers_stream_b.py` adds Pepperstone, Dominion Markets and Vantage
   withdrawal parsers (one shared, conservative, SYNTHETIC-calibrated grammar per broker): exact registrable-domain /
   subdomain sender match (look-alikes refused), anchored lifecycle phrases (terminal / completion checked before the
   generic request, so a "confirm your withdrawal request" email is `CONFIRMATION_REQUIRED`, never `REQUESTED` or
   `COMPLETED`), **no prose → external** (`EXTERNAL_WITHDRAWAL` only on a line-anchored `Withdrawal/Payment method:`
   rail field + affirmative withdrawal direction + no deposit/terminal veto), a confirmation URL / token never
   captured into any field (and a URL reference dropped), amount/currency and a single-anchored source/destination
   account (ambiguous ⇒ neither). They register only via `register_default_parsers` (called by the DARK worker, not at
   import).
6. **Three DARK broker-config rows.** Migration `0026` seeds Pepperstone / Dominion Markets / Vantage as
   `is_active=False`, provenance `SEED`, sender domains marked **PROVISIONAL**. `is_active` is the gate that admits a
   profile's sender domains into the live acquisition/retention allowlist, its authserv-ids into the trust set, and
   the profile into catalogue reconciliation; seeding them inactive keeps them from widening any live surface until an
   operator verifies the real notification domain + auth posture against genuine broker email (which requires live
   access this packet forbids) and flips them active (recording `OPERATOR_VERIFIED`). `supported_categories` records
   each adapter's capability (`EXTERNAL_WITHDRAWAL,INTERNAL_TRANSFER,UNKNOWN`).

## Consequences
- **Positive:** a card confirmation-required withdrawal is represented honestly stage-by-stage; a lapsed confirmation
  or failed payout has an explicit terminal; three more brokers have deterministic, authenticated, under-claim-safe
  adapters; the engine now advances real (rail-less) intermediate and terminal follow-ups without ever creating a
  record from one.
- **Trade-off / residual:** the adapters are SYNTHETIC-calibrated — their patterns are validated against committed
  synthetic fixtures, not a genuine broker sample, and their sender domains are unverified; both are held DARK until an
  operator certifies them against real email. For two contradictory terminals on one reference (e.g. a stray
  `COMPLETED` after an explicit `EXPIRED`), the explicit negative terminal wins (pass 2b before pass 2); this is the
  safer bias and is noted for a future reconciliation if a broker ever emits both.
- **Safety invariants preserved:** externality asserted only by positive rail evidence (no create/advance-into-
  existence from prose, deposit, internal transfer, or completion); monotonic non-regressing status; account/owner
  isolation; idempotent + duplicate-safe; write-once evidence never mutated (only `correlation_status` advances);
  migrations additive; DARK by default.
- **No external identifier is ever captured.** The adapters' account-extraction clause matches a broker INTERNAL
  trading account only (`\d{4,12}` after an explicit "to (your) account"); it deliberately does NOT capture an
  external card PAN / bank / wallet number into the write-once `destination_account` (that would be a permanent,
  unredactable PII leak). The clause is structurally PAN-proof (no word boundary exists inside a 13+ digit run).
- **Owner bridge is account-scoped.** `_find_by_reference`'s owner bridge lets an account-RESOLVED event reach only
  its account-UNRESOLVED owner sibling, never a DIFFERENT account's withdrawal of the same owner — references can
  collide across one owner's accounts (the per-account uniqueness permits it), so an unscoped bridge would corrupt one
  account's record with another's evidence. (Hardened after adversarial review; the new pass 2b first made the path
  reachable for neutral events.)
- **Read models updated in lockstep.** The member-facing withdrawal buckets (`metrics.py`, `withdrawal_speed.py`) now
  classify the new states — `CONFIRMATION_REQUIRED` / `CONFIRMED` as in-flight (pending), `EXPIRED` / `FAILED` as
  terminal non-completions — so the production TradersWay parser's confirmation-stage withdrawals are never dropped
  from the pending bucket or mislabelled unresolved. A coverage test asserts every status maps to exactly one bucket.
- **Migration reverse is non-destructive.** Reversing `0026` cleanly removes only an untouched seed; it REFUSES
  (raises) rather than silently destroy a profile an operator has activated / verified / edited, preserving its
  append-only `BrokerProfileAudit` history.
- **Deviation from the packet (recorded):** the packet scoped "canonical lifecycle states … separate timestamps …
  adapters … scenario". Implementing the explicit states made a **new advance-only correlation pass (2b)** necessary —
  without it the new intermediate/terminal states are unreachable from realistic rail-less follow-up emails. This is an
  Amber extension of shared correlation structure, surfaced here and in the handoff; it strictly preserves every
  safety invariant and cannot create or inflate a withdrawal.
