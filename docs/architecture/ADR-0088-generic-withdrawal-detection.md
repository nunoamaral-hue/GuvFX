# ADR-0088 — Generic withdrawal-email detection (config-driven, fail-closed, DARK)

- Status: Accepted (DARK — globally OFF; owner-scoped `support@` canary only)
- Date: 2026-11-09
- Packet: GFX-WITHDRAWAL-GENERIC-DETECTION-013
- Supersedes: none. Extends ADR-0079 (Stream B explicit withdrawal lifecycle), ADR-0059 (classification
  revision), the WP3b privacy remediation (sender allowlist / retention gate), and §7 external-withdrawal
  classifier. Specific-broker adapters remain authoritative.

## Context

Onboarding a broker today requires writing and deploying a Python parser (`parsers_<broker>.py`). That does
not scale, and a withdrawal email from a broker with no parser is silently dropped (`select_parser → None`
→ no event). We need to detect credible withdrawal-lifecycle emails from brokers that have no code parser —
**deterministically, privacy-safely, and without weakening the existing high-confidence adapters** — and to
onboard new brokers by approving reviewable configuration instead of shipping code.

Hard constraint in tension with "detect unseen brokers": the WP3b **retention gate** in `ingest_message`
fail-closes on non-allowlisted senders — a member's personal inbox must never have non-broker mail retained
as raw evidence. The R3b **authenticity gate** additionally requires a passing SPF/DKIM/DMARC verdict before
any event is created. Neither may be weakened.

## Decision

A new, purely-additive, flag-gated detection layer that runs **only** at the `select_parser → None` point —
i.e. **after** specific parsers, which always win and are never consulted differently. Globally OFF
(`BROKER_GENERIC_DETECTION_ENABLED`), with a per-owner canary allowlist (`support@` only). Two tiers, both
fail-closed:

### Tier 1 — allowlisted sender, no code parser (full evidence present)
The message already passed the retention + authenticity gates, so its raw evidence is stored and the sender
is a configured broker. The generic detector consults the **pattern registry** (`GenericWithdrawalPattern`)
and the deterministic evidence gates:
- **GENERIC_HIGH_CONFIDENCE** — every mandatory gate passes and a pattern matches → create an ordinary
  owner-scoped `BrokerEvent` (`parser_name="generic:<pattern>:<version>"`, `confidence` set), which the
  existing correlation engine then advances/opens exactly as for a coded parser. No new event shape.
- **REVIEW_REQUIRED** — probably a withdrawal but a mandatory gate is missing → owner-scoped
  `WithdrawalReviewCandidate` (redacted), **no event**.
- **QUARANTINED** — authentication / ownership / tenant / parse / evidence ambiguity → candidate, **no event**.
- **NOT_WITHDRAWAL** — marketing / deposit / verification / margin / unrelated → no-op (evidence retained as
  today).

### Tier 2 — truly unseen (non-allowlisted) sender (observation-only, NO raw retention)
The raw-evidence retention gate is **unchanged** — a non-allowlisted sender's raw bytes are **never stored**.
Only in the canary path, and only when the message is **strongly authenticated** (SPF/DKIM/DMARC `pass`) and
carries **explicit withdrawal-lifecycle wording**, the detector creates a **redacted, hash-only**
`WithdrawalReviewCandidate` with disposition `REVIEW_REQUIRED` (or `QUARANTINED` on any ambiguity). It stores
**only**: the evidence sha256 (computed in-memory, for dedup), reason codes, the authenticated sender
**domain** (not the address), masked account suffix (last 4), amount/currency if safely extracted, a derived
candidate broker name, the proposed lifecycle state and confidence. It **never** stores the raw body, a full
account number, an email address, a name or credentials; it **never** creates an event; it **never** binds a
broker or account. Anything short of strong authentication + explicit intent is dropped (NOT_WITHDRAWAL) or
QUARANTINED — never retained and never accepted.

Operator approval of a Tier-2 candidate adds a `BrokerProfile` (which allowlists the domain) and/or a
pattern; **future** mail then flows as Tier 1. Approval is audited and **never retroactively processes** the
original or any prior message (the original raw body was never stored).

**v1 scope (this packet):** the ingestion pipeline wires **Tier 1** only — the generic detector is invoked at
the `select_parser → None` hook, which is reached only by mail that already passed the retention +
authenticity gates (i.e. allowlisted senders). The **retention and acquisition gates are unchanged**, so no
non-allowlisted mail is fetched, stored, or processed. The detector *core* already accepts the Tier-2 inputs
(`sender_allowlisted=False` → `REVIEW_REQUIRED`), so enabling Tier 2 later is a small wiring change gated by a
separate decision about the acquisition query / retention posture. Until then Tier 2 is inert (fail-closed).

## Precedence (invariant)

`KNOWN_BROKER_VERIFIED` (a specific adapter produced a result) always wins. Generic detection runs **only**
when `select_parser` returns `None`, so it can neither override nor alter a specific-parser result. A generic
result never upgrades a specific parser's category or lifecycle.

## Generic evidence contract (mandatory gates for an accepted Tier-1 event)

All must hold, else the disposition degrades to REVIEW_REQUIRED or fails closed to QUARANTINED:
1. Owner/mailbox scope verified (owner is the trusted mailbox owner; canary owner only).
2. Evidence sha256 + (when present) provider message-id available for dedup.
3. Authenticated sender: SPF/DKIM/DMARC `pass` recorded; domain is an allowlisted broker domain (Tier 1).
4. Clear withdrawal intent present (explicit withdrawal wording, not the bare word "withdrawal").
5. Explicit lifecycle wording for the proposed state.
6. Amount + currency extracted **when present** (never fabricated when absent).
7. Broker/source identity derivable from the authenticated domain or clearly attributable content.
8. Event time (occurred/received) and observation time kept separate.
9. No cross-tenant account substitution (account resolution stays within the owner's own accounts).
10. No contradictory lifecycle wording in the same message.
11. No unsafe/unredacted evidence persisted; no URL/token in any stored field.

**Completion is special:** the word "processed" alone is **not** completion. COMPLETED requires an explicit
completion phrase (paid / completed / sent / transferred / withdrawn / credited, or tested multilingual
equivalents) or approved completion semantics. Present/future tense ("is being processed", "will be
processed") is PROCESSING at most, never COMPLETED.

## Fail-closed dispositions

Authentication failure, ambiguous ownership, contradictory status, tenant ambiguity, parse/evidence
ambiguity → **QUARANTINED** (candidate only, never an accepted event). Weak correlation → a **review
candidate**, never a mutation of an unrelated withdrawal. An orphan completion → REVIEW_REQUIRED; it never
fabricates a missing request timestamp, amount, reference, broker account or duration.

## Pattern registry & approval governance

`GenericWithdrawalPattern` is versioned, reviewable configuration (authenticated domains, display name +
aliases, subject/body indicators, lifecycle phrases per state, amount/currency/reference extraction hints,
account-suffix patterns, locale, positive/negative examples, effective date, reviewer, enabled flag,
confidence, provenance). Onboarding a broker is normally **adding/enabling a pattern row** — no deployment,
unless the canonical contract itself changes. A dedicated adapter may still be written when a broker has
genuinely exceptional semantics. **No auto-approval** of sender domains or broker/account mappings: a new
domain only ever yields a review candidate; only a staff-authorised, audited approval enables it.

## Review workflow & surface

`WithdrawalReviewCandidate` (owner-scoped, redacted) carries disposition, proposed state, reason codes,
confidence, masked suffix, amount/currency, authenticated domain, candidate broker, dedup keys and the
correlation result. A minimal owner-scoped, **read-only-by-default** review API exposes these. Approve /
correct-identity / reject-as-not-withdrawal / quarantine actions require **staff** authority, are **audited**
(append-only `ReviewCandidateAction`), and preserve the original evidence hash. Approval must not
retroactively process unrelated messages. Generic REVIEW_REQUIRED / QUARANTINED candidates are **excluded**
from public totals, withdrawal-speed statistics and completed counts.

## Lifecycle / correlation rules

Canonical lifecycle states a detection may propose: REQUESTED, CONFIRMATION_REQUIRED, PROCESSING, COMPLETED,
FAILED, CANCELLED. REVIEW_REQUIRED and QUARANTINED are **candidate dispositions**, not `Withdrawal` statuses
(the `Withdrawal` model and its metrics are untouched). A Tier-1 GENERIC_HIGH_CONFIDENCE event is correlated
by the existing engine (reference-first, then the bounded owner/account/amount/currency/time heuristic);
transitions stay monotonic unless an explicit correction event is recorded; duplicate/replayed messages are
idempotent (one `(owner, evidence)` event; candidates unique per `(owner, evidence_sha256)`).

## Privacy boundaries

Gmail stays observation-only (no write/send/delete). Raw retention gate unchanged (no non-allowlisted raw
bytes stored). Candidates store redacted structural data only — never raw names, full account numbers, email
addresses, message bodies, URLs/tokens or credentials. No PII, raw email content or private evidence in Git,
logs, fixtures or Notion. Owner-scoped throughout; no cross-tenant read or correlation.

## Rollback / disable behaviour

`BROKER_GENERIC_DETECTION_ENABLED` OFF ⇒ the detector is never invoked and no table is touched
(unmigrated-safe path mirrors the existing DARK convention): behaviour is byte-identical to today. The
per-owner canary gate (`broker_generic_detection_enabled_for`) fail-closes to empty. Image rollback +
`broker_intelligence/0028` is additive-only (new tables + nullable columns; no data migration), so a code
rollback leaves the new tables inert.

## Consequences

- New brokers onboard by config (a reviewed pattern), not code.
- Unknown/ambiguous mail is surfaced for review, never silently dropped and never fabricated.
- The highest-confidence path (specific adapters) and all existing behaviour are unchanged.
- A later, separately-gated decision may widen Tier 2 to richer unseen-broker handling; this ADR keeps it
  fail-closed (redacted review candidate only) until then.
