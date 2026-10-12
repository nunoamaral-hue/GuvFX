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
- The genuine-immutable-blob-3 read-only positive control at the deploy/certify stage (§3 limitation).
- The positive `EXTERNAL_WITHDRAWAL` classifier — still blocked on a genuine external-withdrawal sample (TradersWay
  and IS6 alike).
