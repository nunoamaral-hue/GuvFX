# PR-B — IS6 (IS6FX) Deterministic Parser — Certification Record

**Status:** DARK (not merged, not deployed, ingestion OFF). Account 51763830 NOT onboarded (that is PR-C).
**Branch:** `feat/is6-parser` · **Base:** `main` @ `90a9c95`
**Scope:** deterministic IS6 broker-email parser (parser #2), additive to the existing TradersWay parser (#1).

This record follows `.claude/rules/evidence.md`: exact commands + actual results, stated limitations, and a
RULE-11-style positive control. No genuine account number / amount / PII is reproduced here or in any committed
fixture (placeholders only); the genuine evidence (blob 3) stays immutable on production.

## 1. What was built

| File | Change |
|------|--------|
| `backend/broker_intelligence/parsers_is6.py` | NEW — `IS6Parser` (name `is6`, version `v1`): `can_parse` (exact is6.com domain/subdomain + EN/JP keyword) + `parse` (lifecycle + category + amount/currency/account/fee extraction). Pure, no I/O, no DB. |
| `backend/broker_intelligence/parsers.py` | `ParsedBrokerEvent` gained `fee: Optional[Decimal]` + `fee_currency: str` (absent ⇒ None/''; a 0 fee is a real explicit value). |
| `backend/broker_intelligence/models.py` | `BrokerEvent.fee` / `fee_currency` columns (evidential, added to `_IMMUTABLE`); `BROKER_EVENT_TYPES` reference vocab gained `DEPOSIT_REQUESTED/PROCESSING/COMPLETED/REJECTED`. |
| `backend/broker_intelligence/ingestion.py` | stores `fee` / `fee_currency` on the append-only `BrokerEvent`. |
| `backend/broker_intelligence/migrations/0008_brokerevent_fee.py` | NEW — adds the two columns + `CREATE OR REPLACE`s the append-only BEFORE-UPDATE trigger to include `fee`/`fee_currency` in the immutable `ROW()` comparison (NEW + OLD); `reverse_sql` restores the 0006 trigger (reversible, additive). |
| `backend/broker_intelligence/parsers_tradersway.py` | `register_default_parsers()` (the single canonical registrar the worker calls) now registers BOTH TradersWay and IS6, idempotently. |
| `backend/broker_intelligence/tests_is6_parser.py` | NEW — 36 tests (parser units + full ingestion pipeline). |

## 2. Safety invariants (preserved, with structural guarantees)

- **A/B — deposits can never contaminate withdrawal metrics, and there is NO prose→EXTERNAL path.** A `DEPOSIT_*`
  lifecycle always carries category `DEPOSIT`; a withdrawal lifecycle without explicit internal-transfer evidence is
  `UNKNOWN`; `INTERNAL_TRANSFER` only on positive internal evidence. The withdrawal projection counts ONLY
  `EXTERNAL_WITHDRAWAL`, which the parser never emits — external is reserved for certification against a genuine
  external sample (not yet available). Structural, not a filter.
- **C — completion is never inferred from a request / CTA / future / negation.** `_COMPLETED` is anchored to the
  transaction in perfect/present-perfect tense; bare "complete"/"completed"/"完了" are not accepted. A request maps to
  `*_REQUESTED`.
- **D — no URL / token / secret in any emitted field.** `broker_reference_id` is fixed `''` (IS6's template carries
  no safe reference label); `_html_to_text` discards href URLs before parsing.
- **E — fail-closed, never fabricated.** Ambiguous/unsupported/malformed ⇒ `None`; absent amount/account ⇒ `None`/`''`;
  a non-positive amount is dropped; a 0 fee is preserved; an ambiguous multi-separator number is refused (not
  mis-scaled).
- **F — exact sender-domain match.** `can_parse` delegates to `domain_matches`/`_domain_of`; `evilis6.com`,
  `is6.com.evil.com` rejected; `mail.is6.com` accepted.
- **G — append-only immutability intact.** Migration 0008's trigger `ROW()` list equals the 0006 list plus
  `fee`/`fee_currency`, on both NEW and OLD, no column dropped; reverse restores 0006. (Review dimension CLEAN.)
- **H — no TradersWay regression; parsers never claim each other's mail; parser imports only `broker_intelligence`;
  registered only via `register_default_parsers` (stays DARK until the worker is armed).**
- **I — EN + JP both parse** (入金/出金/手数料/口座番号); `occurred_at` left `None` because the broker-stated wall-clock
  time carries no explicit timezone (converting it would be fabrication — the worker derives a tz-correct time from
  headers, as for TradersWay).

## 3. Positive control (RULE-11)

The genuine captured IS6 deposit (blob 3) is the production RULE-11 positive control. A fixture reproducing blob 3's
EXACT grammar (EN) with PLACEHOLDER values yields:

```
event_type=DEPOSIT_COMPLETED  category=DEPOSIT  broker=IS6
amount=150 (placeholder)  currency=USD  fee=0  fee_currency=USD
destination_account=<placeholder>  source_account=''  occurred_at=None
```

A faithful Japanese reconstruction of the same layout yields the identical classification + extraction.

**LIMITATION (stated per the evidence rule):** the genuine-immutable-blob-3 run (parser applied to the real stored
bytes on production, read-only, verifying DEPOSIT_COMPLETED + the real amount/currency/account) has **NOT** been
executed under PR-B, because PR-B must not be deployed under this packet. It is the one remaining certification step,
to be run read-only (no event creation, no ingestion) at the authorised deploy/certify stage. The replicated-grammar
control above exercises the same measurement path and is green.

## 4. Adversarial review (multi-round)

A multi-round adversarial review ran over the parser (parallel lenses → refute-by-default verifiers; a finding is
kept only when independently reproduced by RUNNING the parser). The migration/immutability and privacy/domain
dimensions were CLEAN from the first pass. The lifecycle classifier (`event_type`) was hardened across the rounds
until an invariant-C round produced **no** false completion/rejection; every confirmed defect was fixed and is locked
by a regression test in `tests_is6_parser.py`. Confirmed-and-fixed defects, by class:

- **Field extraction** — a prose "account <N>" / "amount <N>" shadowing the labelled detail row (→ line-anchored
  labels + full "account number" label + single-distinct-or-'' account); European decimal comma mis-scale (→
  normalised, with a `(?![.,]?\d)` fail-closed guard for ambiguous multi-separator numbers).
- **Completion/rejection from non-events (invariant C)** — bare "complete"/"完了"; CTA ("to complete"); future ("will
  be completed"); negation ("has not been completed", JP 未完了 / 完了済みではありません); generic/habitual third-person
  subjects ("a/each/this deposit is completed", "Deposits are completed within 24h"); conditional/temporal clauses
  ("if … was declined", "after/once/until … is completed"); support-footer boilerplate ("if it failed / could not be
  credited"); a persisted "completed … processing" subject on a reply/reversal; quoted-reply history; a completed
  SUB-STEP (review/verification/confirmation/fee/card-auth "of/for your …"); the OTHER direction's event (a funding
  "deposit completed" line in a withdrawal email, incl. the ambiguous-token "your DEPOSIT payment completed" form);
  a JP cancellation ("出金を取り消す手続きが完了").
- The fixes are **structural, not a word-blacklist**: completion/rejection is believed only when the MEMBER'S OWN
  money word (of the resolved direction) is the grammatical SUBJECT of a perfect/past completion verb, within one
  non-conditional, non-quoted clause. EN uses a preposition-lookbehind + qualifier guard; JP uses a money-token +
  subject-particle anchor; both reject the other direction's token.

Throughout every round, the **safety-critical invariants held with zero breaches**: a deposit never classified as
`EXTERNAL_WITHDRAWAL`, no text path reaches `EXTERNAL_WITHDRAWAL` at all, and no finding contaminated the withdrawal
projection (a mis-classified lifecycle stays category DEPOSIT or UNKNOWN). These are structural, independent of the
lifecycle-precision work above.

**Residual limitation (stated per the evidence rule).** `event_type` precision is free-text natural-language
classification and is therefore not provably exhaustive: an inherently ambiguous phrasing (e.g. a bare "your payment
has been completed" in a contradictory "request received" email, with no direction keyword in the clause) or an HTML
`<blockquote>` reply-quote carrying no `>` marker could still mis-order the lifecycle. Every such residual is
**fail-safe for the stream's safety invariants** (category, and therefore the withdrawal/external metrics, stays
correct) and the parser is DARK (no events created). Lifecycle precision will be re-certified against genuine captured
IS6 samples at the Sponsor-gated activation; this mirrors the TradersWay parser's documented acceptance of
prose-classification limits.

## 5. Tests (exact commands + results)

```
cd backend && .venv/bin/python manage.py test broker_intelligence.tests_is6_parser    # Ran 57, OK
cd backend && .venv/bin/python manage.py test broker_intelligence                     # Ran 254, OK
cd backend && .venv/bin/python manage.py test execution.tests_tb1_migration           # Ran 2, OK (0008 reverse valid)
cd backend && .venv/bin/python manage.py test                                         # Ran 5592, OK (skipped=1)
cd backend && .venv/bin/python manage.py makemigrations --check broker_intelligence   # No changes detected
```

## 6. Out of scope / owed (separate authorisations)

- PR-C: onboard IS6 account 51763830 with confirmed owner + a VERIFIED IS6 `BrokerEmailIdentity`.
- Dual-broker continuous-ingestion activation (Sponsor-gated).
- The positive `EXTERNAL_WITHDRAWAL` classifier — still blocked on a genuine external-withdrawal sample (TradersWay
  and IS6 alike).

## 7. PR-B.1 — lifecycle classification correction (the genuine blob-3 certification fix)

PR-B deployed DARK; its production certification against genuine blob 3 **FAILED** on lifecycle: the deployed parser
returned `DEPOSIT_REQUESTED` instead of `DEPOSIT_COMPLETED` (category DEPOSIT, amount 200 USD, fee 0 USD, account
…3830, auth pass all correct). **Root cause:** the real completion sentence is *"…the payment procedure **for your**
bank transfer has been completed"* — a DEFINITE money-operation subject with prepositional ownership. The completion
anchor required a possessive *"your &lt;money&gt;"* subject: the subject head here is "**the** payment" (no "your") and
the only "your" is "**for** your bank transfer" (governed by a preposition → rejected by the sub-step guard), so no
completion matched and it fell through to `_REQUESTED`, which matched "**received**" in the footer *"If you received
this email by mistake."*

**Fix (structural, not phrase-specific):**
- A new **definite-subject completion branch** (`_own_completed_definite`, used by `_COMPLETED` only): a money-op noun
  as the SUBJECT HEAD under `the/a/our/this`, a `(for|of|to|via|on|into) your …` ownership PP whose object is itself a
  money word **immediately before a PERFECT/PAST** completion verb. This accepts the genuine construction while
  structurally rejecting present-tense habitual/SLA rules ("a deposit **is** completed within 24h"), cross-kind
  funding reassurance, generic/policy lines, fee-payment sub-steps ("the payment **of your deposit fee** has been
  completed"), and administrative sub-steps ("the **review** of your withdrawal …"). The possessive `_own` branch is
  unchanged (battle-tested across the PR-B rounds); `_REJECTED`/`_PROCESSING` get no definite branch (no genuine
  sample).
- **Request detection re-anchored** to an actual transaction request (`<txn> request` / `request (for) <txn>` /
  `received your <txn> request` / JP money-token + リクエスト/申請/申込/受付) — routine footer "if you received this email by
  mistake" can no longer be a request.

Both changes went through a **multi-round adversarial review**. An initial over-broad determiner generalization was
rejected and replaced by the tight definite branch; subsequent rounds then hardened a family of EN completion-verb /
possessive-gap precision gaps, each fixed and locked by a regression test:
- the definite branch gained an **inversion guard** (EN/JP parity): "the payment **cancellation/reversal/refund** for
  your withdrawal has been completed" is not a completion;
- the completion verb tolerates an **affirmative adverb** ("has been **successfully** completed") but the possessive
  **gap rejects negating/diminishing adverbs** (never/rarely/nearly/almost/partially/virtually/… — "has **never**/
  **nearly** been completed" is not a completion);
- a **generalized object guard** on the possessive branch: when the money head is an ambiguous operation ("your
  payment/transfer …"), its prepositional object (under of/for/via/by/… within a short window) must not be a
  sub-step / charge / inversion noun **or the other direction's transaction** — so "your payment **of the withdrawal
  fee** / **via the identity verification** / **for the originating deposit** has been completed" are all rejected,
  while instrument/destination/amount objects ("to your bank account", "via this method", "of 500 USD") stay genuine;
- **request detection** ignores imperative/marketing CTAs ("Request a deposit…", "you can request a withdrawal") and
  recognises member forms ("<txn> request", "received your <txn> request", "request for a <txn>", "you have requested
  a <txn>", JP money+リクエスト/申請/申込/受付).

Safety invariants were **structural and held with zero breaches across every round** (each review verdict confirmed
`category_safe` / `breaches_safety_invariant=false`): **no deposit → EXTERNAL_WITHDRAWAL, no prose → EXTERNAL path,
deposits never contaminate withdrawal metrics** — the residual precision gaps only ever mis-ordered the lifecycle
`event_type` with the category (and therefore the metrics) staying correct. No migration (parser-logic only; mig 0008
already applied on `main`).

**Residual limitation (stated per the evidence rule):** EN free-text completion/rejection classification is not
provably exhaustive; the object guard ends the known sub-step/charge/inversion/cross-kind object class structurally,
but a sufficiently novel prose construction could still mis-order the lifecycle. Every such residual is **fail-safe
for the safety invariants** (category correct) and the parser is DARK. Lifecycle precision is to be re-certified
against genuine captured IS6 samples at the Sponsor-gated activation (mirrors the TradersWay prose-limit acceptance).

**Repeatable read-only production certification** is prepared as a management command
`backend/broker_intelligence/management/commands/certify_is6_blob.py` — it verifies the evidence hash, runs the
DEPLOYED parser on the stored raw bytes WITHOUT ingestion, compares every expected vs actual field (account masked),
and asserts NO event creation / NO cursor advance / NO evidence change / counts preserved (snapshotted before & after).
It is strictly read-only and is **not** to be run against production until authorised:

```
python manage.py certify_is6_blob --blob-id 3 --expect-event DEPOSIT_COMPLETED --expect-category DEPOSIT \
    --expect-amount 200 --expect-currency USD --expect-fee 0 --expect-fee-currency USD --expect-account <acct>
```

PR-B.1 is DARK, development-only; not merged, not deployed, ingestion OFF, account 51763830 not onboarded.
