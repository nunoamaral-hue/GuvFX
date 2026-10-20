# GUVFX_BROKER_INTELLIGENCE — PD FOUNDATION REVIEW — INTEGRATED SUBSYSTEM REPORT

**Packet:** GUVFX_BROKER_INTELLIGENCE_PLATFORM — PD FOUNDATION REVIEW · **Author:** Principal Engineer (Claude) ·
**Date:** 2026-10-19
**Baseline main:** `54647ca` (end of the multi-account + public-metrics foundation programme) → **after this review:**
`6554290` (C1–C4 all merged). MT5/execution architecture frozen and untouched.

---

## 1. What the review asked, and what was delivered

The Programme Director's review directed four things on the already-merged DARK foundation (#511–#515):

1. **Correct #511's proposed Message-ID de-duplication architecture** — reconciling *existing global evidence
   uniqueness* with *owner-scoped event attribution* **first**, and not implementing the Message-ID suppression
   until the corrected design was accepted.
2. **Correct #513's** account-ownership, broker-consistency, mailbox-relationship, deletion and audit-integrity
   safeguards.
3. **Preserve the distinction** delivered-email ≠ broker-event ≠ financial-withdrawal: duplicate deliveries must not
   inflate statistics; unrelated authentic evidence must never be discarded.
4. **Continue independent DARK public-directory / customer-frontend scaffolding where dependencies permit**, complete
   full regression + adversarial review + governed integration without routine Sponsor approvals, and deliver **one
   integrated report**.

Delivered as four governed, CI-green, adversarially-reviewed squash-merges, each DARK and reversible:

| PR | Scope | Squash SHA |
|----|-------|-----------|
| #517 | **C1** — corrected design of record (ADR) | `4f9bdb3` |
| #518 | **C2** — dedup re-architecture (owner-scoped idempotency + delivery dedup + ref-less collapse) | `3bcbbea` |
| #519 | **C3** — association safeguards (PROTECT + fail-closed VERIFIED + DB-enforced audit) | `944c0a9` |
| #520 | **C4** — public broker directory frontend (DARK-graceful, en/ja) | `6554290` |

The design (C1) was **accepted (merged) before** any dedup implementation landed (C2), exactly as directed.

---

## 2. The architectural reconciliation (the heart of the review)

**The conflict.** Evidence is content-addressed: identical raw bytes → one immutable `EvidenceBlob` (sha256). The
former `BrokerEvent` uniqueness was **global per blob** (`uniq_brokerevent_evidence`), and the ingest idempotency
returned the first event for a blob. So byte-identical bytes reaching **two different owners** would hand the second
owner the **first owner's event** — a cross-owner provenance leak / silent drop. Simply "scoping the filter" does not
fix it: the common live path resolves `owner=None` (fail-closed recipient resolution), which collapses an
owner-scoped filter to `WHERE owner IS NULL` and re-opens the leak.

**The reconciliation (C2).** Keep the blob globally shared and immutable; make the **event** owner-scoped:

- The event is keyed on **`owner_key`** = the **trusted mailbox owner** (`owner_user`, non-null on every live
  `mailbox_ingest` delivery), *not* the fail-closed recipient resolution. The create, the early idempotency filter,
  and the `IntegrityError` recovery all use the identical `owner_key`.
- `BrokerEvent` uniqueness relaxed global → **owner-scoped** `uniq_brokerevent_evidence_owner (owner, evidence)`
  (partial, both non-null). The owner-null hard unique is **deliberately omitted** — it would hit Postgres
  NULLS-DISTINCT and, because `owner` is `SET_NULL` on user deletion, collide a to-be-nulled sibling for a shared blob
  and **deadlock the operator purge**; the live path always writes a non-null owner, so the null bucket is
  synthetic/legacy only and relies on the app-layer filter.

Result: two owners with identical bytes share **one** immutable blob yet each keep their **own** owner-attributed
event — no cross-owner leak, no dropped delivery.

---

## 3. The three layers (delivered-email ≠ broker-event ≠ withdrawal)

1. **Delivery** — de-duplicated by the **trusted mailbox-native** `(connected_mailbox, provider_message_id)`. A
   genuine re-fetch of the same message from the same inbox reuses the event; **two different inboxes = two preserved
   deliveries** (never suppressed). Empty `provider_message_id` / null mailbox ⇒ the delivery write is skipped (never
   a dropped event); the row is written **only after** a successful event (no tombstone). `BrokerMessageObservation`
   was repurposed from Message-ID suppression to this delivery record.
2. **Broker event** — one per `(owner, delivery)`, owner-scoped idempotency (§2).
3. **Financial withdrawal** —
   - **ref-carrying**: deterministic `(trading_account, broker_reference_id)` collapse;
   - **ref-less** (e.g. the certified **IS6** template, which carries no reference — so the transaction-layer unique
     never fires and two inbox copies would otherwise **double-count** the public stat): the ref-less opening adopts an
     already-projected withdrawal of the same logical email for the same `(owner, trading_account)` at the
     **correlation layer**, using the RFC822 Message-ID as a **non-authoritative cross-inbox hint** (a conservative
     under-count on a forged id, never inflation, never cross-owner). Inert unless the delivery table is populated; the
     live correlation pass is **gated so it never arms without cross-inbox dedup** (`run_withdrawal_correlation`
     refuses otherwise).

**The RFC822 Message-ID is never a suppression/trust key again** — only non-authoritative metadata / a correlation
hint. Raw eligible broker evidence is never dropped on it.

---

## 4. Association safeguards (C3)

- **Deletion (history-safe).** `TradingAccountBrokerContact.trading_account` `CASCADE → PROTECT`. Routine offboarding
  is tombstone-only and the API `destroy` is refused, so PROTECT only guards the operator `User.delete()` purge and
  direct row deletes — and the purge must RETIRE/delete a user's bindings first. `TradingAccountBrokerContactAudit`'s
  back-reference FKs (incl. `contact`) are `SET_NULL` so append-only history **survives a purge as orphaned rows**
  instead of deadlocking it (`TradingAccount ←PROTECT— Contact ←PROTECT— Audit` would otherwise be unbreakable).
- **Audit integrity — DB-enforced, selective.** New `BEFORE UPDATE`/`DELETE` triggers make the audit append-only at
  the DB layer, not just the app guard. The UPDATE trigger is **selective**: recorded facts are immutable, while the
  five back-reference FKs may **decay to NULL on a parent cascade** but never be re-pointed or un-nulled — so parent
  and user deletions (incl. the beta purge) still work.
- **Fail-closed VERIFIED — ownership is the ACCOUNT owner, never the shared email.** A new `BEFORE INSERT OR UPDATE`
  trigger refuses `status='VERIFIED'` unless the mailbox is non-null and owned by the account owner **and** the broker
  email identity belongs to the account owner — enforced at the DB so `bulk_create`/`update()`/raw cannot forge a
  cross-user VERIFIED. It is **cascade-safe**: a mailbox decaying to NULL on an already-VERIFIED row is allowed
  (verification goes stale), while any non-null mailbox/identity is fully checked. The reference writer
  (`contact_verification`) applies the same checks + mailbox-connected + broker-name token match, VERIFYing on proof
  (+audit) or setting UNRESOLVED (+REJECT audit) — never a silent pass. **Canonical-catalogue-slug matching is a
  documented deferred TODO** (no name→slug resolver exists; not fabricated — the token match is used this packet).

---

## 5. Frontend (C4)

The **public broker directory** (`(public)/brokers` + `(public)/brokers/[slug]`) consumes the DARK
`GET /api/public/brokers/` API, en/ja, parity-registered. DARK-graceful: a 404 / network error shows a calm "not yet
available" panel; an empty list shows "no brokers published yet"; a below-threshold metric shows the truthful
**INSUFFICIENT_DATA** state. It renders **only** the aggregate-only projection — never a member identity, email,
account, transaction or amount — and carries the governance disclaimer (GuvFX never initiates/confirms/cancels
withdrawals; broker-reported ≠ funds received). Verified in-browser (both routes render the DARK panels; en/ja keys
resolve). **Deferred (dependency not yet permitting):** the customer account-level broker-email **binding/verify UI**
has no HTTP endpoint yet (C3 shipped `contact_verification` as a DARK service, no view/URL).

---

## 6. Adversarial verification (what it caught, and that it was fixed)

Multi-agent workflows ran **before** implementation (design verification) and **on each** substantive PR:

- **Design-verify (pre-implementation), 9 agents:** found **14 plan-breaking defects** and resolved all before a line
  was written — most importantly the `resolved_owner`-is-NULL leak (→ key on `owner_key`), the **IS6 ref-less
  double-count** (the "collapse at the transaction layer" premise was false for ref-less certified brokers → added the
  correlation-layer collapse + arming gate), and a user-delete deadlock (→ omit the owner-null unique).
- **C2 implementation review:** 4 candidates → **3 false positives + 1 LOW** (a test-coverage gap, code correct);
  added `test_shared_message_id_never_merges_across_owners`.
- **C3 implementation review:** 7 candidates → **4 false positives + 3 confirmed**, all fixed: (LOW, latent) the
  VERIFIED trigger would abort a mailbox `SET_NULL` cascade on a VERIFIED row → made cascade-safe; (MEDIUM, coverage)
  the DB-floor identity-ownership and null-mailbox reject clauses were untested → added bulk/raw tests +
  a mailbox-delete-survives test; (LOW) `verify_contact` was not atomic → wrapped status+audit in one transaction.

---

## 7. Evidence

- **Tests (local, `--parallel 1`):** `broker_intelligence` **559** green (incl. 19 cross-inbox-dedup + 25 association
  tests) and `broker_public` **13** green. Rewrote `tests_cross_inbox_dedup` (old assertions encoded the superseded
  Message-ID suppression) and updated two tests encoding the old `owner=resolved_owner` semantics (the IS6 cross-user
  **firewall** test now asserts `owner=mailbox-owner` + `trading_account=None` — the firewall it protects is intact).
- **CI:** every PR CI-green across the duplicated workflow runs before a governed manual squash-merge (auto-merge OFF;
  main SHA verified after each).
- **Migrations 0014 + 0015:** additive, DARK, **NOT applied to prod** (prod `broker_intelligence` stays 0010).
  Reversibility proven end-to-end on dev (`migrate 0015 → 0010` and back; both `RunSQL` reverse paths clean).
- **Frontend:** eslint + tsc clean on the new files; parity routes registered; in-browser smoke test of both routes.

---

## 8. Amber decisions (documented, for the handoff)

1. `BrokerEvent.owner` write changes `resolved_owner` → `owner_key` (the mailbox owner) — more faithful to the field's
   own "verified mailbox owner" meaning, but it alters the set-once semantics on the live path.
2. `BrokerEvent` uniqueness relaxed global → owner-scoped; the owner-null hard unique is deliberately omitted.
3. Migration 0014 + the C2 ingestion code must **co-deploy** on any promotion to prod (the code is incorrect against
   the legacy global constraint for the shared-blob different-owner case).
4. Ref-less withdrawal collapse uses the RFC822 Message-ID as a non-authoritative hint; correlation arming is coupled
   to cross-inbox dedup.
5. `TradingAccountBrokerContact.trading_account` `CASCADE → PROTECT` + a mandatory **purge-sequence** change (RETIRE
   bindings before `User.delete()`); `TradingAccountBrokerContactAudit.contact` `PROTECT → SET_NULL`.
6. Two new DB triggers on the association/audit tables (selective append-only audit; fail-closed VERIFIED).
7. "Canonical catalogue slug" broker-consistency is a deferred TODO — token match only this packet (no slug resolver
   exists; not fabricated).

---

## 9. Governance boundaries honored

No production migration applied, no production deploy, no NAS write, no Google publishing change, no customer
activation, no financial action. All new behaviour is DARK (flags OFF ⇒ 404 / no-op). `CERTIFIED_BROKERS` stays empty
⇒ every public aggregate is `INSUFFICIENT_DATA`. GuvFX never initiates, confirms, or cancels a withdrawal. MT5 /
execution architecture is frozen and untouched.

---

## 10. Remaining (designed / sequenced, honestly gated — not claimed built)

- **Customer account-level broker-email binding/verify UI** — blocked on an HTTP surface for
  `TradingAccountBrokerContact` (the `contact_verification` service exists DARK). Build when that endpoint lands.
- **Canonical-slug broker-consistency resolver** (name → `BrokerProfile.slug`) — net-new; currently token-match.
- **DARK backfill** of legacy `BrokerEmailIdentity` bindings → `TradingAccountBrokerContact`.
- **Public-API anonymous/scoped throttle** — a pre-activation requirement before the public surface is armed.
- **§H consent/participation policy + approved public scoring methodology** — governance prerequisites to any public
  metric leaving `INSUFFICIENT_DATA`.

*ENGINEERING_COMPLETE (this correction packet, DARK) ≠ PRODUCTION_DEPLOYED ≠ PUBLIC_METRICS_ACTIVATED ≠
GENUINE_PILOT_CERTIFIED. The migration-first deployment (resolving the prod 0010 gap), activation, consent + scoring
governance, and genuine certified data are the critical path from here — not engineering.*
