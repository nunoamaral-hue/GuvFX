# GUVFX_WITHDRAWAL_V1_GENUINE_PILOT_REPORT

**Packet:** GUVFX_WITHDRAWAL_V1_GENUINE_PILOT_COMPLETION
**Author:** Principal Engineer (Claude)
**Date:** 2026-10-17
**Branch / PR:** `feat/withdrawal-v1-genuine-classifier-prep` (off canonical main `f39dc27`) → **PR #504** (DARK; do not merge without PD authorization). Head commit `72cf33b`.
**One-line outcome:** All independent DARK engineering is complete, reviewed, and green. The central deliverable — certifying a **genuine** positive external-withdrawal classifier — remains **BLOCKED** on a Sponsor-initiated genuine withdrawal (a human financial action GuvFX must never perform). Nothing was fabricated; `CERTIFIED_BROKERS` stays empty; no production change; no schema change.

---

## Why the pilot cannot be "completed" now (and why that is correct)

The subsystem is built and fail-closed. A positive `EXTERNAL_WITHDRAWAL` can only be certified against a real email in which money actually left the broker — and none exists (production `Withdrawal` count = 0; the pilot parsers emit only DEPOSIT/INTERNAL_TRANSFER/UNKNOWN). The packet forbids fabricating a template or writing speculative positive-match rules. So the classifier certification is genuinely blocked; I completed every build-able thing around it and left a certified, ready-to-fill path for the instant the genuine sample arrives.

## What was delivered (DARK, schema-free, against canonical main)

1. **Privacy-safe genuine-template fixture extraction** (`fixture_extraction.py` + read-only `extract_sanitised_fixture`) — the one structural gap. Masks salutation names / emails / account+reference numbers / URLs / long tokens; preserves labels + amount shape; residual-review net **broader than the masker** (surfaces decimal-glued identifiers it deliberately leaves); real hash verification; zero DB mutation; rejects an in-repo `--out`; integrity markers on any artifact.
2. **Generalized read-only certification harness** (`certify_broker_blob`) — ready for the positive path; today proves the fail-closed posture (cannot add to `CERTIFIED_BROKERS`). `certify_is6_blob` untouched.
3. **Ingestion alerting** (`broker_mailbox_health --exit-nonzero-on-recovery/-degraded`) — scheduled runs can now signal a dead mailbox; default still exits 0.
4. **Correlation robustness** — the adoption path promotes in its own savepoint (parity with the create path); a concurrent `(account,ref)` race can no longer poison the outer transaction.
5. **Test hardening** — classifier override/normalization/predicate; correlation lifecycle ladder/terminal/mapping/backfill/race; capability + structural guards; RULE-11 fixture controls; ops-command read-only/alert/tamper controls.
6. **Docs** — authenticity spec, OAuth sustainability recommendation (proximity-signal + `Status.ERROR` designs flagged PD-gated), production promotion/rollback plan.

## §14 required report

1. **Genuine evidence inventory:** 5 genuine authenticated blobs (TradersWay #1 internal-transfer; IS6 #2/#3 deposits; IS6 #4/#5 daily-confirmations) + the 10-14 IS6 daily confirmation captured on recovery (blob #7). **All genuine-NEGATIVE** w.r.t. external withdrawal. No external-withdrawal template for either broker. Production `Withdrawal` = 0.
2. **TradersWay withdrawal lifecycle:** only the ambiguous `WITHDRAWAL_CONFIRMATION_REQUIRED` precursor exists (blob #1), classified INTERNAL_TRANSFER / UNKNOWN. No genuine REQUESTED→…→COMPLETED external sequence observed.
3. **Authenticity verification:** enforced + tested — exact-domain allowlist (never substring), R3b `Authentication-Results` verdict (DMARC pass or From-aligned DKIM pass), fail-closed quarantine of unauthenticated broker mail, owner-scoped attribution firewall, immutable content-addressed evidence with tamper detection.
4. **Positive-classifier implementation:** decision core + contract present and fail-closed; the per-broker **extraction** is **BLOCKED** (needs the real template — not written speculatively). Added the generalized certification harness + authenticity spec so certification is a checklist when the sample lands.
5. **Negative exclusion results:** PASS — internal-transfer / deposit / no-positive-signal / uncertified-broker all → not external, at the decision core (synthetic) AND the parser layer (incl. genuine-negative blobs). New capability/structural guards prove parsers never emit `EXTERNAL_WITHDRAWAL` and the decision core is not wired into them.
6. **Evidence preservation:** PASS — EvidenceBlob + BrokerEvent append-only at app + DB layers; content hash re-verified on read; fixture extraction is strictly read-only.
7. **Correlation results:** synthetic-certified (reference-id determinism, bounded heuristic, ambiguity-safety, monotonic no-regress, idempotency, race recovery) + hardened this packet. **Genuine correlation BLOCKED** (no genuine EXTERNAL event to correlate).
8. **Monetary/timestamp fidelity:** per-currency never mixed; amounts/fees never fabricated; `occurred_at` (broker-stated) vs `received_at` (ingest) kept distinct. Noted nuance: a completion lacking `occurred_at` falls back to ingest time for `completed_at` (duration only on COMPLETED-status rows in metrics) — documented, not silently changed.
9. **Metrics certification:** synthetic-certified (pending≠failed, rejected/cancelled excluded from completed totals, per-currency sums, duration avg/median/fastest/slowest, None-not-0). **Genuine metrics BLOCKED** (0 genuine withdrawals).
10. **UX certification:** synthetic-certified + DARK (owner-scoped, masked accounts, pending/completed distinction, empty states, self-hide on 404, no cross-member exposure). Minor UX gaps (visible sample-size caption, single-sample caveat, known-amount disclosure, genuine count) recommended as a small follow-up; not implemented here to keep the diff backend-only.
11. **Regression results:** `broker_intelligence` **443 tests OK**; full backend suite green (5956 OK on the pre-review-fix tree; final-tree re-run: see the PR / closeout line). No models/migration change.
12. **Adversarial review:** 3 independent skeptics. Ops-governance **0 findings**. Fixture-extraction: 3 CONFIRMED (residual blind spot HIGH, whole-amount over-mask MEDIUM, hash tautology MEDIUM) + lows — **all fixed + re-verified**. Correlation: one pre-existing, documented, SYNTHETIC/DARK-only orphan-row limitation (not a blocker; docstring made honest).
13. **PRs / merge SHAs:** PR **#504** (this work, DARK, open — not merged). Related already on main: #500 (`bb053ab`), #501 (`e168235`), #503 (`f39dc27`). #502 is the production hotfix artifact (off `a474f79`) — **not** for main.
14. **Production activation readiness:** NOT ready — gated on the genuine classifier certification. Promotion/rollback documented (`docs/WITHDRAWAL_V1_PRODUCTION_PROMOTION_ROLLBACK.md`); schema boundary unchanged (prod 0010; 0011 unapplied).
15. **OAuth reliability findings:** root cause = Testing-mode 7-day refresh-token expiry (recurred; re-consent 2026-10-17 restored ingestion — 2 passes `failed=0`). Shipped: alertable health exit code. Recommended (PD-gated): publish the app to Production + Gmail restricted-scope verification; a dedicated refresh-credential-age proximity signal; `Status.ERROR` on a blocking re-auth. Publishing status not host-determinable.
16. **Remaining risks:** (a) the genuine withdrawal sample (Sponsor); (b) no off-host DB recovery (promotion stays gated); (c) orphaned `.enc` credential accumulation (hygiene, later, never blind-delete); (d) unbounded quarantine growth from forged-From mail (monitor, never drop evidence).
17. **Updated completion estimate:** ~**94%** of Withdrawal V1 engineering. Remaining ~6% is (1) a Sponsor-performed genuine withdrawal, then (2) the per-broker extractor + certification + correlation/metrics validation on genuine data, then (3) governed promotion.
18. **Exact next action (blocked):** the **Sponsor performs one smallest-safe genuine external withdrawal on TradersWay account 46**, using the broker's legitimate process. GuvFX never initiates, confirms, cancels, or modifies it. On arrival, the healthy pipeline captures it; I extract a sanitised fixture, write + adversarially certify the deterministic positive classifier, validate correlation/metrics/history on genuine data, and prepare governed promotion.

## §13 completion conditions

| | Condition | Status |
|---|---|---|
| A | Genuine external-withdrawal evidence exists | **BLOCKED** (none; Sponsor action required) |
| B | Positive classification certified | **BLOCKED** (depends on A) |
| C | Correct BrokerEvent attribution proven | PARTIAL — genuine-negative proven; positive blocked on A |
| D | Withdrawal correlation certified | PASS (synthetic/DARK); genuine blocked on A |
| E | Metrics verified against genuine data | **BLOCKED** (synthetic certified; genuine depends on A) |
| F | Member history verified | PASS (synthetic/DARK); genuine blocked on A |
| G | Negative transaction exclusions intact | **PASS** (synthetic + genuine-negative; new structural guards) |
| H | No financial/trading action by GuvFX | **PASS** (none performed; read-only throughout) |
| I | Production promotion risks documented | **PASS** (promotion/rollback plan) |
| J | Internal pilot acceptance criteria met | **BLOCKED** (depends on A/B) |

## §14 markers

| Marker | Verdict |
|---|---|
| `GMAIL_INGESTION_PRODUCTION_HEALTHY` | **PASS** (post-consent; two `failed=0` passes) |
| `TRADERSWAY_CAPTURE_READY` | **PASS** (healthy pipeline; identity bound; parser registered) |
| `GENUINE_EXTERNAL_WITHDRAWAL_EVIDENCE_AVAILABLE` | **BLOCKED** |
| `TRADERSWAY_POSITIVE_CLASSIFIER_CERTIFIED` | **BLOCKED** |
| `WITHDRAWAL_CORRELATION_CERTIFIED` | **PASS (synthetic, DARK)** — genuine blocked |
| `WITHDRAWAL_METRICS_CERTIFIED` | **PASS (synthetic, DARK)** — genuine blocked |
| `WITHDRAWAL_HISTORY_CERTIFIED` | **PASS (synthetic, DARK)** — genuine blocked |
| `WITHDRAWAL_V1_INTERNAL_PILOT_READY` | **BLOCKED** |
| `WITHDRAWAL_V1_PRODUCTION_READY` | **BLOCKED** |

*No genuine-evidence certification was substituted with synthetic. No financial/trading action was performed. No production credential, cursor, schema, container, or evidence row was changed by this packet; all work is DARK behind off flags on a feature branch.*
