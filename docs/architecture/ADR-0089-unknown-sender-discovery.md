# ADR-0089 — Unknown-sender withdrawal discovery (pre-retention, redacted, DARK)

- Status: Accepted (DARK — globally OFF; `support@` owner-canary only; observation-only)
- Date: 2026-11-09
- Packet: GFX-WITHDRAWAL-UNKNOWN-SENDER-DISCOVERY-014
- Extends: ADR-0088 (generic withdrawal detection). This ADR adds the Tier-2 layer ADR-0088 deliberately
  deferred, and **materially expands the trust boundary**, so it is filed as its own ADR.

## Context

ADR-0088 Tier 1 detects brokers that are already allowlisted but have no code parser. A genuinely **new,
non-allowlisted** broker's withdrawal email never reaches the detector: the acquisition filter
(`gmail_source.list_new_messages`) fetches only allowlisted senders (server-side `q=from:(...)` on initial
sync; a metadata-only `From` pre-check on the incremental `history.list` path), and the `ingest_message`
retention gate is a hard second layer — so non-allowlisted mail is never fetched in full and never stored.

To discover unknown brokers we must look at **some** non-allowlisted mail. This is a real privacy-boundary
expansion and is therefore built as the narrowest, fail-closed, redacted-only, owner-canaried, observation-only
stage.

## Decision

A **pre-retention discovery stage** that runs **only** for an explicitly-canaried owner, piggybacks on the
**existing** incremental `history.list` walk (no new broad query, no second cursor, no cursor rewind), and
persists **only a minimal redacted record** — never the raw body, never personal data, never an event or
withdrawal.

### Where it runs (acquisition mechanism — narrowest footprint)
The incremental walk already lists every new message id and already reads `From` metadata of each to drop
non-allowlisted mail. For a canary owner, for a non-allowlisted id only:
1. read **one extra metadata header** (`Subject`, alongside the `From` already read) — no body;
2. if the Subject lacks a withdrawal-lifecycle keyword → **drop** (the raw body is never fetched);
3. only if it has one → fetch the raw (`format=raw`) **in memory**, derive SPF/DKIM/DMARC verdicts and run
   the strict discovery classifier, and **discard the raw** — it is never stored.
Allowlisted mail is unchanged (Tier-0 specific parsers, then Tier-1 generic, exactly as today). The **main
cursor advances exactly as before**; discovery is a side-observation within the same pass and never changes
cursor logic. Initial sync (no cursor) does no discovery (forward-only; no historical backfill).

### Mandatory gates for a discovery candidate (all must pass, else drop — no record)
1. Owner is explicitly in the discovery canary.
2. SPF, DKIM and DMARC meet the approved authentication policy (DMARC `pass`, or SPF+DKIM both `pass` with
   From-aligned DKIM). Weak/missing authentication ⇒ no candidate (fail closed).
3. A safe canonical sender-domain fingerprint can be derived.
4. A sufficiently strong, explicit withdrawal-lifecycle signal is present.
5. Marketing / newsletters / deposits / trade confirmations / statements / login alerts / KYC-only notices /
   unrelated financial mail are rejected.
6. Confidence meets a conservative band threshold.
7. Dedup/idempotency passes.

### What is persisted — `UnknownSenderDiscoveryCandidate` (redaction contract)
**Only** these fields, none of which ever receives a raw subject/body snippet or any personal datum:
owner; `message_sha256` (hash of the raw bytes, for dedup — not the bytes); `provider_message_id_hash`
(salted hash of the Gmail message id); `sender_domain_hash` (salted hash of the canonical domain);
`registrable_domain` (**only** when the authentication policy approved the sender as genuine — a broker's
public domain, not personal data; empty otherwise); SPF/DKIM/DMARC verdicts; `lifecycle_class`;
`confidence_band` (LOW/MEDIUM/HIGH — a band, never a precise score); `reason_codes` (structural enums);
`received_at`/`observed_at`; `detector_version`; `review_state`; immutable audit timestamps.
**Never persisted:** raw body or subject text, names, full email addresses, account numbers, references,
amounts, fees, payment details, URLs, tokens, OAuth/credential material.
The redaction contract `validate_discovery_record_clean` is formally tested: given a message carrying PII, the
produced record contains none of it (structural guarantee — the model has no field that can carry it).

### What it never does
Never stores the raw non-allowlisted body. Never writes a `BrokerEvent` or `Withdrawal`. Never adds a sender
to an allowlist. Never enables a `GenericWithdrawalPattern`. Never binds a candidate to a broker/account.
Never sends mail, approves a withdrawal, moves funds or contacts a broker. Never rewinds a cursor or
reprocesses historical mail.

### Staff review workflow
Owner-scoped read of discovery candidates (redacted). Staff-only, audited actions:
reject · mark insufficient-evidence · associate with an existing `BrokerProfile` · create a **proposed**
broker/profile candidate · create a **proposed `GenericWithdrawalPattern` in DISABLED state**. Pattern
creation requires an explicit staff decision and never enables detection; enablement is a **separate** audited
staff action. Once a staff-enabled pattern exists, future matching mail flows through the ADR-0088 Tier-1
engine (specific-parser precedence, owner isolation, conservative thresholds, no auto-binding) — no new code
deployment needed.

## Idempotency / replay
Unique `(owner, provider_message_id_hash)` ⇒ a re-delivered/re-walked message updates the one record, never a
duplicate. The main-cursor dedup is unchanged.

## Rollback / disable
`BROKER_UNKNOWN_SENDER_DISCOVERY_ENABLED` OFF (global) and the owner allowlist empty ⇒ the discovery stage is
never invoked, `GmailMailSource` is constructed without discovery, no extra metadata/raw read happens, and no
table is touched — byte-identical to ADR-0088. The migration (`broker_intelligence/0029`) is additive (one
new table + one append-only audit table); a code rollback leaves them inert.

## Consequences
- GuvFX can surface credible unknown-broker withdrawal emails for staff review **without** retaining the mail,
  creating a withdrawal, or auto-trusting a new sender.
- The privacy footprint is bounded to: one extra metadata header (Subject) on non-allowlisted new mail already
  metadata-read for `From`, and an in-memory raw read of only subject-withdrawal-keyword non-allowlisted mail,
  discarded after redacted extraction.
- All of Tier-0 (specific parsers) and Tier-1 (ADR-0088) behaviour is unchanged.
- GA (beyond the support@ canary) and any widening of the acquisition footprint are separate future decisions.
