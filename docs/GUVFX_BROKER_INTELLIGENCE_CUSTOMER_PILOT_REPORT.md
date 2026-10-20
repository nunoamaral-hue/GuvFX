# GUVFX_BROKER_INTELLIGENCE_CUSTOMER_PILOT_REPORT

**Packet:** GUVFX_BROKER_INTELLIGENCE_CUSTOMER_PILOT_COMPLETION (P0) · **Author:** Principal Engineer (Claude) ·
**Date:** 2026-10-20
**Baseline main:** `6554290` → **final main:** `8125770f`. Hosted MT5 / execution architecture frozen and
untouched. Everything below is DARK (new flags default OFF ⇒ 404 / no-op / byte-identical), additive and reversible;
no production migration applied, no activation, no public publication, no Google OAuth publishing change, no NAS
write, no storage purchase, no trading-execution authority, no financial action.

---

## 1. Executive summary

The remaining customer-facing multi-account Broker Intelligence subsystem is built, adversarially reviewed, CI-gated
and squash-merged across seven coherent DARK PRs. The public privacy posture is corrected (a live pre-activation leak
closed) and hardened (k-anonymity + a participation-consent contract); members can connect multiple Gmail inboxes;
the account-level broker-email association has an HTTP API + an EN/JA customer UI; VERIFIED now requires
independently-supportable routing evidence; attribution consults the verified bindings. The genuine TradersWay
external-withdrawal pilot's engineering prerequisites are in place and the engine is ready, but its **certification is
BLOCKED on the raw genuine RFC822 evidence**, which is deliberately not in the repository and was not accessible in
this packet — documented honestly, never fabricated (§6).

## 2. PRs & merge SHAs

| PR | § | Scope | Squash SHA |
|----|---|-------|-----------|
| #522 | 6a | public INSUFFICIENT_DATA leak fix + dedicated anon throttle | `dc6e43f` |
| #523 | 6b | minority-bucket k-anonymity + participation-consent contract | `1883e76` |
| #524 | 3  | multiple independent Gmail inboxes per member | `4365724` |
| #525 | 4  | account-level broker-email contact API + Withdrawal Monitoring UI (EN/JA) | `5b20983` |
| #526 | 5  | contact-scoped transaction-account attribution | `fa0f15f` |
| #527 | 2  | routing-evidence requirement before VERIFIED | `8125770f` |

All six CI-green before a governed manual squash-merge (auto-merge OFF; main SHA verified after each). #512/#513/#514/
#515/#517–#520 of the prior foundation remain accepted and were not rebuilt.

## 3. Multiple Gmail inboxes (§3, PR #524)

`BROKER_MULTI_MAILBOX_ENABLED` (default OFF ⇒ the V1 single-mailbox policy is byte-identical; all 63 existing mailbox
tests pass unchanged). The three single-mailbox view guards relax: an explicit `ADD_NEW_MAILBOX` is permitted while a
live mailbox exists, bounded by a per-owner cap (`BROKER_MAX_MAILBOXES_PER_OWNER`, default 5) enforced **atomically
inside the per-owner advisory-locked callback block**; `>1` live with no explicit op ⇒ `mailbox_op_required` (never
retargets); reconnect-while-other-live relaxes in multi mode. Preserved: explicit ADD_NEW vs RECONNECT, owner-scoped
ops, signed OAuth state, `gmail.readonly` only, encrypted credentials, per-mailbox cursors (no silent reset),
independent health/revocation, and the cross-user `(provider, provider_mailbox_id)` anti-hijack uniqueness. A
**PostgreSQL two-thread concurrency proof** shows two concurrent ADD_NEW callbacks for different accounts are
serialized by the advisory lock and both succeed under the cap (no lost write / breach / duplicate). No migration.

## 4. Account-level email management (§4, PR #525)

`BROKER_ACCOUNT_CONTACT_ENABLED` (default OFF ⇒ routes 404; the UI self-hides). Three owner-scoped `APIView`s
(`accounts/<id>/contacts/` list, `…/create/`, `…/<cid>/verify/`), all with the
`TradingAccount.objects.filter(id, user=request.user)` cross-user firewall and masked, secret-free payloads; reusing
the merged-DARK `TradingAccountBrokerContact` model, its append-only audit, the migration-0015 fail-closed VERIFIED DB
trigger, and the `contact_verification` writer (no model/migration change). One `(email, broker)` binds to many of
the member's accounts; binding is explicit, audited, and **never auto-verified**. A customer **Withdrawal Monitoring**
UI (`AccountWithdrawalMonitoring.tsx`, EN/JA, self-hides on 404) is mounted on the per-account detail page. The
adversarial review caught and the merge closed a **HIGH cross-user identity-takeover** (the create firewall now uses
the *effective owner* — `user_id` OR `trading_account.user_id` — under a row lock, claiming only a truly unowned
registry row), plus a mailbox-change-resets-to-PENDING fix and a documented existence-oracle residual.

## 5. Verification + attribution (§2 PR #527, §5 PR #526)

- **Routing evidence (§2).** `BROKER_ROUTING_EVIDENCE_REQUIRED` (default OFF ⇒ byte-identical). A same-owner CONNECTED
  mailbox no longer suffices: a VERIFIED binding needs **direct_address** (the bound mailbox *is* the broker-registered
  address, gmail-canonical) or **observed_delivery** (an authenticated `provenance=REAL` delivery of *this broker's*
  mail observed through the bound mailbox, owner-scoped on both the observation and the joined event). The review
  tightened observed_delivery to be broker- and event-owner-scoped (another broker's mail in a consolidated inbox
  never proves routing). The identity-verify path already enforced direct address-equality.
- **Attribution (§5).** `BROKER_ATTRIBUTION_CONTACT_SCOPED_ENABLED` (default OFF ⇒ number-only, byte-identical). When
  a VERIFIED identity is attributed, the broker-stated-number match is **narrowed** to that identity's VERIFIED-bound
  accounts — a shared email resolves to the one bound account instead of going ambiguous. The stated-number match
  stays **mandatory** (the binding only narrows, never substitutes; inbox/identity ownership alone never assigns an
  account; an un-bound number match is UNRESOLVED). Shared-email and multi-account negative cases certified.

## 6. Genuine TradersWay pilot — status: BLOCKED on genuine evidence (honest, not fabricated)

The Sponsor's genuine external Ethereum withdrawal (request + completion notifications, matching reference, 33m01s
interval) from a TradersWay account **not connected to GuvFX** establishes *candidate* evidence, not certification.
The required §7 actions are **BLOCKED on the raw genuine RFC822**, which is deliberately not in the repository
(data.md — no raw data in Git) and was not accessible to the engineer in this packet. Per the no-assumption rule and
the parser's own structural invariant, **none of this was fabricated**:

**BLOCKED ledger (who unblocks each):**
1. Ingest the genuine RFC822 into an immutable `EvidenceBlob` — needs the raw bytes (Sponsor/ops).
2. `extract_sanitised_fixture` to derive the real TradersWay EXTERNAL template structure — needs (1).
3. Write/certify the positive EXTERNAL_WITHDRAWAL classifier against that reviewed structure — the parser has a
   **structural no-prose→EXTERNAL invariant** (`parsers_tradersway.py`): external is *only ever* certified against a
   genuine email, never guessed. Fabricating it would violate that invariant and research.md.
4. Add TradersWay to `CERTIFIED_BROKERS` — an operator act, not code (`certify_broker_blob` can never do it);
   `CERTIFIED_BROKERS` stays empty ⇒ no public aggregate leaves INSUFFICIENT_DATA.
5. The `--attest-authenticity-signoff` human attestation.
6. The pilot account is **not connected to GuvFX** ⇒ the existing fail-closed path keeps attribution UNRESOLVED and
   the event out of every Withdrawal/stat (`correlate_event` → `OUT_SKIP_NO_ACCOUNT`) — this needs **no new code**.

**What is already engineered and ready** (no genuine evidence needed, already merged in the foundation): the
correlation engine projects `EXTERNAL_WITHDRAWAL` events deterministically (reference-first; the C2 ref-less
cross-inbox collapse prevents double-counting); aggregation reports `COMPLETED` as **broker-reported dispatch, never
wallet receipt** (`receipt_confirmed_if_ever_available` is always 0 — no receipt signal exists), so completion is
never equated with funds receipt; UNRESOLVED attribution for an unconnected account is preserved.

**Decisions (documented, RULE 7 / architecture.md):**
- **No speculative `broker_processing_at` field was added.** s7(d) (excluding human-confirmation delay) is satisfiable
  only if the broker's mail carries a broker-processing timestamp; the TradersWay flow is two notifications 33m01s
  apart with no evidence of an intermediate processing timestamp, so the interval is request→completion **wall-time**,
  not isolated broker-processing time. Adding a field with no populating source would be speculative infrastructure;
  the metric must instead be **labelled truthfully** (which it is). If a future genuine template proves an
  intermediate timestamp exists, the field is a bounded follow-up.
- **No fabricated TradersWay external extractor was written** (it would guess the genuine layout — forbidden).
- **No further Sponsor withdrawal is requested** — no specific remaining certification gap requires one; the gap is
  access to the already-performed withdrawal's raw evidence, not a new withdrawal.

## 7. Public privacy safeguards (§6a PR #522, §6b PR #523)

- **Leak fix (live pre-activation defect).** The public read model emitted exact `sample_size` / `contributor_count`
  / window / timestamp even in INSUFFICIENT_DATA. It now returns only a truthful generic stub
  (`{category, state, methodology_version}`) below threshold. The leak-locking test assertion was **flipped**
  (`assertIn("sample_size")`→`assertNotIn`) — "all tests pass" was not proof until the flip.
- **Dedicated anonymous throttle.** `GuvFXPublicAnonRateThrottle` (scope `public`, proposed 60/min) on both public
  views — a custom scope, so it is configured (the earlier `AnonRateThrottle` ImproperlyConfigured blocker is gone).
- **Minority-bucket k-anonymity** (`BROKER_PUBLIC_MINORITY_SUPPRESSION_ENABLED`, default OFF). The review caught that
  the first implementation was **cosmetic** (a lone small bucket was re-published as its exact count under
  `suppressed`, and lifecycle was reconstructable by differencing against `sample_size`); the merge delivers real
  **complementary suppression** (a `suppressed` tally is published only when itself ≥ `min_bucket_size`; lifecycle
  folds the smallest published buckets until no single rare count can be differenced out; `processing_time.count`
  clears the same floor).
- **Participation-consent contract** (`BROKER_PUBLIC_CONSENT_REQUIRED`, default OFF). New `MetricParticipationConsent`
  (opt-in, revocable, DB-enforced single-ACTIVE per `(owner, scope)`, supersede-not-overwrite) — distinct from Gmail
  inbox-read consent; aggregation includes a member's evidence only while an ACTIVE row exists (fail-closed).

## 8. Consent / publication readiness (§H)

A documented participation/consent policy and an approved public scoring methodology remain **governance
prerequisites** to any public use of evidence — the consent *model* and the privacy controls now exist DARK, but the
policy, the methodology, and the activation decision are the PM/Sponsor's (notion.md; not advanced here). No broker
rating exists; no public metric is activated; `CERTIFIED_BROKERS` is empty.

## 9. Test & adversarial evidence

- **Backend (local, `--parallel 1`):** `broker_intelligence` **594** green, `broker_public` **22** green, plus the
  mailbox suite (69, incl. the PG concurrency proof). Exact command: `cd backend && .venv/bin/python manage.py test …`.
- **Frontend:** eslint + tsc clean on the new files; the public directory routes verified in-browser; parity routes +
  components registered.
- **CI:** every PR green across the duplicated workflow runs before a governed squash-merge; SHA verified after each.
- **Adversarial review ran on every substantive PR and found real defects that were fixed before merge:** PR2 — the
  cosmetic-suppression privacy defect (MEDIUM, + 3 related); PR3 — a LOW flag-OFF message regression; PR4 — a **HIGH
  cross-user identity-takeover** (+ 4); PR5 — observed_delivery broker/owner mis-scoping (MEDIUM/LOW); PR6 — clean. A
  pre-implementation design-verification workflow (earlier packet) and per-PR find→verify workflows were used
  throughout; every confirmed finding was resolved and regression-tested, or (for the one bounded existence-oracle
  residual) documented rather than overclaimed.

## 10. Remaining production dependencies

Migrations unapplied (migration-first deployment required, resolving the prod 0010 gap — `broker_public` 0002 +
`broker_intelligence` through 0015); the public surface needs the anon throttle rate and the k-anon / consent
governance values ratified before activation; the genuine pilot needs the raw evidence + `CERTIFIED_BROKERS`
certification (§6); arming `BROKER_ROUTING_EVIDENCE_REQUIRED`'s observed_delivery and the
contact-scoped attribution both benefit from arming `broker_cross_inbox_dedup` first (documented hard dependency).

## 11. Updated delivery forecast

The customer-facing multi-account subsystem (multi-mailbox, account-level binding + verify, routing evidence,
contact-scoped attribution) and the public privacy posture are **engineering-complete and DARK-merged** this packet.
The critical path from here is **not engineering**: it is the genuine certified evidence (§6), the consent + scoring
governance (§8), the activation decisions, and the migration-first deployment.

## 12. Amber decisions register (each needs an ADR/Notion record before activation)

1. `BROKER_MAX_MAILBOXES_PER_OWNER` cap source (settings vs entitlement) + more inboxes/owner raises duplicate-delivery
   probability ⇒ arming `broker_cross_inbox_dedup` is a prerequisite before public metrics are trusted.
2. `TradingAccountBrokerContact` is the authoritative account-level binding vs the legacy identity single-FK
   (non-destructive; documented).
3. The account-contact create existence-oracle residual (authenticated, audited, per-user-throttled, coarse) — a
   tighter create-throttle is a pre-activation hardening.
4. Routing-evidence flag sequencing (observed_delivery depends on the delivery plane) + acct46 grandfathering.
5. Contact-scoped attribution fallback semantics (flag ON + no identity ⇒ number-only).
6. Public `min_bucket_size`, consent-contract existence + scope granularity, and the anon-throttle rate.
7. §6 BLOCKED ledger ownership (Sponsor initiates withdrawals + Gmail consent; GuvFX never initiates/confirms/cancels).

## 13. DARK proof & governance boundaries

Every new behaviour is behind a default-OFF flag (OFF ⇒ 404 / no-op / byte-identical, proved by the flag-OFF
regression tests) or is an unconditional correctness fix with no activation. No production migration applied, no
production deploy, no public publication, no Google OAuth publishing change, no NAS write, no storage purchase, no
trading-execution authority, no financial action. GuvFX never initiates, confirms, or cancels a withdrawal.

*ENGINEERING_COMPLETE (this DARK packet) ≠ PRODUCTION_DEPLOYED ≠ PUBLIC_METRICS_ACTIVATED ≠ GENUINE_PILOT_CERTIFIED.*
