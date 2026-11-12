# ADR-0099 — Data-driven certified broker fingerprint (automatic broker recognition without a code packet)

- Status: Accepted (repository-only; DARK — gated, not seeded/certified/activated in production)
- Date: 2026-11-12
- Packet: GFX-BROKER-AUTORECOGNITION-IMPLEMENTATION-074 (programme lane A)
- Extends: ADR-0088 (generic detection), ADR-0089 (unknown-sender discovery), ADR-0090 (Vantage reference-first),
  ADR-0093 (Vantage completion), ADR-0095 (Vantage currency field)
- Isolation: separate from the active Copy Trading workstream — touches no execution / live-authorization /
  host-admission / emitter / mutation-transport / broker-order path.

## Context

Onboarding each new broker today requires bespoke per-broker code (a parser in `parsers_stream_b` / `parsers_*`,
with its semantics — `requires_reference`, `completion_processed_successfully`, `account_from_body`,
`currency_from_field`, `wants_html` — held as **code constants**). That does not scale: every new broker needs a new
code change, review and deploy.

ADR-0088 already ships a fail-closed GENERIC detector (Tier-1/2/3) and a `GenericWithdrawalPattern` config whose
extraction hints (`lifecycle_phrases`, `amount_hint`, `currency_hint`, `reference_hint`, `account_suffix_patterns`)
were **inert** — `detect()` consumed only the sender domain + subject/body indicators, and an accepted generic event
was hard-capped at `transaction_category = UNKNOWN` ("generic never asserts EXTERNAL_WITHDRAWAL without certified
evidence"). So a verified broker could not drive extraction from data, and could never produce a counted withdrawal.

## Decision

Introduce a **versioned, immutable-once-certified, governance-tracked, privacy-safe `BrokerFingerprint`** and wire
the detector to consume it, so a genuinely new broker is onboarded as **governed data**, not code.

### 1. Model (`BrokerFingerprint` + `BrokerFingerprintAudit`, migration 0032, additive)

- Broker relationship (FK `broker_profile`), authenticated sender / envelope-from / reply-to / DKIM signing domains,
  subject/body indicators, `{STATE: [phrase]}` lifecycle classifiers, amount/currency/reference field-label hints,
  safe account-suffix patterns, and the per-broker **semantic overrides as DATA** (`ALLOWED_SEMANTIC_KEYS` only —
  an unknown key fails closed).
- `version` + unique `(name, version)`; `source_hash` = deterministic sha256 of the canonicalised matching snapshot
  (order-independent), recomputed on every save.
- **Certification ≠ activation.** `governance_status` (PROPOSED→VALIDATED→CERTIFIED→DEACTIVATED/ROLLED_BACK/REJECTED),
  `is_active` (activation), and `certified_capability` (version-bound authority to emit a certified
  EXTERNAL_WITHDRAWAL) are **distinct**. A merely active or review-approved fingerprint has **no** certified authority.
- **Immutability.** Once `certified_capability` is true, the matching snapshot is frozen — enforced at the app layer
  (`save()` compares to the persisted row) AND at the database (migration-0032 PL/pgSQL triggers reject any UPDATE
  that changes `source_hash` while certified, reject clearing `certified_capability`, and reject DELETE of a certified
  version — so a bulk `QuerySet.update()`/raw SQL cannot rewrite it). A change requires a **new version**.
- **Anti-ambiguity at the source.** Partial unique `(name)` where `is_active AND certified_capability` → at most one
  live certified version per broker. Audit is append-only (write-once; never deleted).
- Tenant isolation is at the **event** layer (owner-scoped `BrokerEvent`); a fingerprint is global broker config.

### 2. Detector wiring (`generic_detector.detect`, additive)

`detect()` gains optional `fingerprints` / `dkim_domains` / `envelope_from_domain` / `reply_to_domain`. **With
`fingerprints=()` behaviour is byte-identical to ADR-0088.** When supplied:

- A fingerprint's `lifecycle_phrases` are **added** to the built-in classifier (they drive classification); its
  amount/currency/reference field-label hints drive extraction on the case-preserving text (ReDoS-safe: the label is
  escaped + length-bounded).
- `_fp_eligible` is the detector's **own** default-deny gate (active + certified + status CERTIFIED), independent of
  the caller's filter — a mis-scoped query can never unlock certified authority.
- **Tier 1 (certified).** Exactly one eligible fingerprint claims the message (domain + any declared DKIM/envelope/
  reply-to alignment + indicators), authentication passes, there is an explicit lifecycle, extraction is safe and
  no contradiction — the detector MAY emit the certified broker/category. **Certification unlocks the capability;
  emission of `EXTERNAL_WITHDRAWAL` still requires positive external-money-out evidence in the message**
  (`_external_evidence`: an external rail AND no internal-transfer veto) and, when the fingerprint's semantics require
  a stable reference, a reference — otherwise the category stays `UNKNOWN` (a valid lifecycle event, never counted).
- **Tier 2 (probable new broker).** Credible withdrawal, no certified fingerprint → `REVIEW_REQUIRED` + a redacted
  discovery/review candidate; never an automatic certified withdrawal.
- **Tier 3 (ambiguous/unsafe).** Authentication failure, two distinct certified brokers matching one message,
  contradictory terminal lifecycle, or unsafe evidence → `QUARANTINED`; no withdrawal.

The service (`generic_detection`) loads only active+certified fingerprints and persists the detector's category
(the previously hard-coded `UNKNOWN` becomes the detector's value, fail-closed to `UNKNOWN` on any surprise value).

### 3. Governed onboarding (`fingerprint_onboarding`, `broker_fingerprint` command)

`propose → validate (dry-run) → certify → activate → [deactivate | rollback]`, each explicit + audited. **Certify is
fail-closed**: staff actor + reason + ticket; an ACTIVE broker profile; the fingerprint's authenticated domains ⊆ the
profile's approved senders; **no domain overlap** with another active+certified fingerprint (conflict scan);
representative SYNTHETIC fixtures present; and a dry-run **regression gate** (every positive fixture recognised, no
negative fixture auto-accepted). There is deliberately **no command that silently certifies a discovered broker.**

### 4. Bounded reprocessing (`fingerprint_reprocess`)

Owner-scoped, version-pinned, bounded, **dry-run-first** planning over **already-retained** evidence. Idempotent /
duplicate-safe (an item already evented is SKIPPED); a prior event that disagrees STOPS for review; evidence that no
longer resolves stays N/A. It NEVER reconstructs a historical raw email, broadens Gmail access, or moves a cursor.

### 5. Seed (`fingerprint_seed`, seed MECHANISM — never an auto-running data migration)

The Stream-B brokers (Vantage/Pepperstone/Dominion) are seeded as data-driven fingerprints carrying the migrated code
constants, each with SYNTHETIC fixtures and a deterministic checksum. **Not run in production by this PR.**

## Scope / non-goals

- **DARK + non-regressive.** The detector consults fingerprints only on the generic path (`select_parser` → None) and
  only for a canary owner. The existing SPECIFIC parsers (TradersWay, IS6, Pepperstone, Dominion, Vantage) run FIRST,
  so a seeded fingerprint can never change an existing broker's behaviour. Brokers with a bespoke parser KEEP it.
- No production seed/certify/activate; no mailbox read; no cursor movement; no public publication; no broker action;
  no Copy Trading edit. DKIM/envelope/reply-to alignment is not yet surfaced by `MailMessage` (only an aggregate
  `auth_verdict`) — a fingerprint DECLARING those constraints fails closed until ingestion populates them (follow-up).

## Consequences — gated sequence (NOT executed here)

Production use is a **separate, Sponsor-gated** sequence: deploy → seed a specific broker's fingerprint → operator
dry-run → certify (with ticket) → activate → bounded reprocess (dry-run → apply) → monitor. Each step is audited and
reversible (deactivate / rollback). This ADR is the reviewable mechanism, not an authorisation to run it in prod.
