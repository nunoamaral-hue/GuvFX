# External-Withdrawal Authenticity & Certification Spec (Withdrawal V1)

**Status:** DARK reference spec. Additive; changes no runtime behaviour.
**Scope:** defines exactly what a GENUINE external-withdrawal sample must satisfy before a broker's positive
classifier may be certified and added to `CERTIFIED_BROKERS`. It specifies **requirements**, never extraction rules
(no speculative positive-match regex — those are written only against the real template, §5/§6 of the packet).
**Authority:** certification is a deliberate operator act gated on a Sponsor-initiated genuine withdrawal; GuvFX never
initiates, confirms, cancels, or modifies a withdrawal.

---

## 1. Why this exists

The decision core (`external_withdrawal_classifier.py`) is fail-closed: `CERTIFIED_BROKERS` is empty, so no broker can
positively classify an `EXTERNAL_WITHDRAWAL` today, and neither pilot parser has a text→EXTERNAL path. The one missing
ingredient is a **genuine** authenticated email in which money actually left the broker. This spec makes the moment
that email arrives unambiguous: it lists the conditions, the evidence to capture, and the controls, so certification is
a checklist, not a judgement call.

## 2. Authenticity preconditions (ALL required)

A candidate message qualifies as a genuine external-withdrawal sample only when **all** hold:

1. **Provenance = REAL.** Captured by the live ingestion worker from the connected mailbox — never SYNTHETIC, never a
   hand-authored fixture. (`BrokerEvent.provenance == REAL`.)
2. **Allowlisted sender.** `From` domain is an exact registrable-domain/subdomain match of the broker's allowlist
   (`broker_senders.domain_matches`), never a substring.
3. **Authentication verdict = pass.** `Authentication-Results` yields `pass` under the deployed R3b logic — DMARC=pass,
   or a From-ALIGNED DKIM=pass from a trusted authserv-id (`gmail_source._auth_verdict`). A spoofed-From message is
   quarantined as evidence with **no** event and never certifies anything.
4. **Immutable evidence.** The raw bytes are stored write-once in the content-addressed `EvidenceStore`; the
   `EvidenceBlob.sha256` is recorded and re-verifies on read. The certification reads the stored bytes, never a copy.
5. **Correct owner + account.** The owner is resolved owner-scoped (spoofed recipient headers cannot cross-attribute),
   and the broker-stated source/destination account matches one of the owner's own `TradingAccount`s; otherwise
   `trading_account` stays `None` (UNRESOLVED) and the sample does not certify an attribution.
6. **Timestamps preserved distinctly.** The broker-stated transaction time (`occurred_at`) and the ingest time
   (`received_at`) are both retained and never collapsed.

## 3. Positive-evidence requirement (what makes it EXTERNAL, not merely "withdrawal")

`EXTERNAL_WITHDRAWAL` is asserted **only** when the genuine message contains explicit, unambiguous evidence that money
left the broker environment — mapped to exactly one `ExternalWithdrawalSignals` field:

- `external_destination` — a named external institution / bank / wallet (not an internal account), **or**
- `external_payment_rail` — an external settlement rail (bank wire, card refund, crypto, …), **or**
- `explicit_external_type` — the broker explicitly types it as an external/outgoing withdrawal.

**Hard overrides win regardless** (and must be checked first): `internal_transfer` (money stayed inside the broker) →
never external; `deposit` → never a withdrawal. The mere word "withdrawal", the subject, the sender, an amount, or an
account number are **never** sufficient. Ambiguous → UNKNOWN. These invariants are already enforced and unit-tested;
certification adds the per-broker **extraction** that populates these fields from the real template — written only
once the template is in hand.

## 4. Lifecycle (represent only what the genuine evidence shows)

Use the existing event vocabulary and `Withdrawal.Status`; invent no stages. Map broker wording to the approved
events (`WITHDRAWAL_REQUESTED/CONFIRMATION_REQUIRED/CONFIRMED/PROCESSING/APPROVED/COMPLETED/REJECTED/CANCELLED`). A
request is represented as a request (status REQUESTED); a completion is correlated to its request by reference-id first,
then the bounded deterministic heuristic (account + amount + currency within 14 days); multiple candidates → AMBIGUOUS,
never silently merged; no completion is ever fabricated; a rejected/cancelled withdrawal never enters completed totals.

## 5. Chain of custody & privacy for any committed fixture

- A committed test fixture is **SANITISED**, never REAL: derive it with `extract_sanitised_fixture --blob-id N`
  (read-only, hash-verified, zero DB mutation), review the reported residual numeric runs, and hand-finish redaction.
- Never commit raw financial identifiers, personal email content, confirmation URLs, authorization tokens, or
  credentials. Names, e-mail addresses, account/reference numbers, URLs and tokens are masked; labels, layout and the
  amount *shape* are preserved so the extractor can be written against structure.
- Record the source `EvidenceBlob.sha256` (integrity) in the certification record; the genuine values stay only in the
  immutable evidence store, never in Git.

## 6. Certification procedure (when a genuine sample exists)

1. Confirm §2 preconditions on the captured blob (owner, allowlist, auth=pass, provenance REAL, hash).
2. Run `certify_broker_blob --blob-id N --broker <Broker>` — read-only; today it proves the fail-closed posture (the
   broker is NOT in `CERTIFIED_BROKERS`, and a strong synthetic positive signal is still refused). Nothing mutates.
3. Write the deterministic, anchored per-broker extractor that populates `ExternalWithdrawalSignals` from the real
   template; add positive **and** negative controls (RULE 11) incl. the existing genuine-NEGATIVE blobs.
4. Add the broker to `CERTIFIED_BROKERS` and declare `EXTERNAL_WITHDRAWAL` in its `BrokerProfile.supported_categories`
   (a deliberate operator change, reviewed) — then `certify_broker_blob … --expect-category EXTERNAL_WITHDRAWAL
   --require-certified` must PASS.
5. Adversarial review; full backend regression; DARK until promotion (see the promotion/rollback plan).

## 7. Acceptance criteria for a certified positive classifier

- Deterministic, versioned, broker-specific, no LLM path.
- Every §8 adversarial case passes: auth failure, unverified sender, wrong account, cross-user conflict, internal
  transfer / deposit / unrelated exclusions, malformed, missing fields, duplicate, out-of-order, reference collision,
  ambiguous correlation, monetary precision, currency, timestamp/timezone, immutable-evidence preservation, no
  synthetic production events, no trading-system coupling.
- The genuine sample re-parses byte-identically and correlates to exactly one `Withdrawal` of provenance REAL.
