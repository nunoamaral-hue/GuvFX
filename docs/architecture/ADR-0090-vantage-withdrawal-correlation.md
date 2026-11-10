# ADR-0090 — Vantage withdrawal lifecycle recognition + reference-correlation (repository-only, migration committed-not-applied)

- Status: Accepted (repository-only; Vantage activation migration committed but NOT applied; no public publication)
- Date: 2026-11-10
- Packet: GFX-WITHDRAWAL-VANTAGE-CORRELATE-EXEC-019
- Extends: ADR-0079 (Stream B withdrawal-email adapters), ADR-0089 (unknown-sender discovery)

## Context

The ADR-0089 unknown-sender discovery canary observed two genuine, authenticated **Vantage**
(`vantagemarkets.com`, a non-allowlisted broker) withdrawal emails — a submission-stage *"Withdrawal
Submitted"* and an acceptance-stage *"Withdrawal Request Successful"*, ~19 s apart — as redacted,
pre-retention candidates. Both are **REQUEST-stage** observations; neither proves completion.

Three gaps block turning these into **one** correctly-correlated withdrawal lifecycle without duplicate counting:

1. **Lifecycle terminology.** The Tier-0 `parsers_stream_b._LIFECYCLE_RULES` are separate from the generic /
   discovery `_REQUESTED` set. They did not recognise *"Withdrawal Submitted"* (no "request" word) or
   *"Withdrawal Request Successful"* ("successful" appeared only as *confirmation* successful), so the Vantage
   adapter would decline or mis-stage these emails.
2. **Reference-less double-count.** An opening `WITHDRAWAL_REQUESTED` with **no** stable reference opens its own
   ref-less withdrawal and is never heuristically merged (IS6, a certified ref-less broker feeding public metrics,
   relies on this). Two ref-less Vantage REQUEST emails would therefore become **two** withdrawals for one real
   lifecycle.
3. **Profile inert.** The Vantage `BrokerProfile` (seeded by migration 0026) is `is_active=False`, so
   `vantagemarkets.com` is neither allowlisted nor retained.

## Decision

A **narrow, repository-only** correction. No production mutation is performed by this ADR/packet.

### Lifecycle (truthful, fail-closed)
Extend the Tier-0 `WITHDRAWAL_REQUESTED` rule so *"Withdrawal Submitted"* and *"Withdrawal Request Successful"*
(and *"request accepted"*) classify as **`WITHDRAWAL_REQUESTED`** — the OPENING stage. Safeguards:
- The `WITHDRAWAL_COMPLETED` rule is evaluated **first** and still requires explicit settlement / payment /
  processed wording, so none of the new phrases can ever read as `COMPLETED`.
- *"successful"* is **anchored** to *"withdrawal request … successful"*, so a bare "successful",
  "deposit successful" or "payment successful" can never match.
- Future tense / negation break the "submitted" adjacency and **fail closed** (classify as nothing).
- Existing Pepperstone / Dominion / TradersWay behaviour is unchanged.

### Correlation (stable reference proves the relationship)
The existing reference-first correlation engine already dedupes deterministically: an event with a
`broker_reference_id` keys the single Withdrawal `(account|owner, reference)` via partial-unique constraints
(idempotent), advances monotonically (REQUESTED never regresses), and sets `completed_at` **only** on a real
completion. Two Vantage REQUEST events sharing one reference therefore become **one in-progress withdrawal**,
submission and acceptance timestamps preserved, **never completed**, replay-idempotent; two different references
remain two withdrawals.

### Reference-required fail-closed (per-broker, Vantage only)
A new per-adapter `requires_reference` flag (default **off**; Vantage **on**) downgrades an
`EXTERNAL_WITHDRAWAL` to `UNKNOWN` when no **stable** reference is extracted. A stable reference must carry at least
one **digit**: the shared `_REFERENCE` regex deliberately makes the trailing `id/no/#` token optional, so ordinary
withdrawal-email boilerplate ("this transaction may take 3-5 days", "for reference please keep this email") yields a
non-empty but meaningless alphabetic token — emptiness alone is therefore not trusted as the absence of a reference.
An unstable token is cleared and the event downgraded. Correlation never fabricates a record from an `UNKNOWN`
event, so a reference-less Vantage withdrawal email is **held for manual review**, never force-correlated by timing,
never double-counted, and a garbage token can never merge two genuinely distinct withdrawals under the
reference-first key. Brokers that do not set the flag (Pepperstone / Dominion / IS6) are unaffected.

### Activation (committed, not applied)
Migration `0030_activate_vantage_profile` flips **only** the Vantage profile to `is_active=True`
(`OPERATOR_VERIFIED`, append-only audit, true-inverse reverse), mirroring the Pepperstone 0027 activation. It is
committed to the PR but **must not be applied to production in this packet**; applying it, deploying, and
reprocessing the two genuine emails is a separate Sponsor-gated packet. Certification (`CERTIFIED_BROKERS`) and
public publication stay **disabled**.

## Consequences

- Two genuine Vantage emails can later become exactly one truthful **in-progress** (REQUESTED) withdrawal, with no
  duplicate counting and no premature completion.
- Privacy is preserved: only synthetic / sanitised fixtures are used; no customer name, account, reference, amount
  or raw body enters Git, tests, logs, evidence or Notion.
- The change is additive and fail-closed; no existing broker's behaviour changes.
