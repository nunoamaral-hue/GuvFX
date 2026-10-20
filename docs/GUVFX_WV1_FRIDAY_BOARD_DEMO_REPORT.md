# GUVFX_WV1_FRIDAY_BOARD_DEMO_REPORT

**Packet:** GUVFX_WV1_BOARD_DEMO_CRITICAL_PATH
**Engineering deadline:** Friday 2026-10-23 · **Board demo:** Monday 2026-10-26
**Baseline:** canonical `main` @ `991477df` · **Report date:** 2026-10-20
**Status:** Friday acceptance **PASS** for the engineering critical path (functioning, visible demonstration delivered on an isolated demo environment). Production activation remains **NO-GO / gated** exactly as before this packet.

---

## 1. Executive summary

Withdrawal Intelligence is now a **functioning, visible demonstration**, not a set of dark components. The genuine pilot withdrawal — an **owner-attributed, account-UNRESOLVED** external (Ethereum) withdrawal whose TradersWay account is not registered in GuvFX — flows all the way through **parser → correlation → owner-scoped member API → dashboard UI**, and renders on screen (logged in as a demo member) as:

> `250.00 USD · TradersWay · ⚠ Account unresolved | 33 min · ref DEMO-7654321 ✓ · Completed`

…under a prominent **"DEMONSTRATION DATA"** banner, with truthful metrics (`Genuine samples: 0`). See `docs/wv1_demo_evidence/`.

Four PRs were delivered, each **DARK** (no production migration, deploy, flag-flip, publication, or financial/credential action), each through an adversarial find→verify review with every confirmed defect fixed:

| PR | Scope | Status | Merge SHA |
|----|-------|--------|-----------|
| #530 | Certified TradersWay external-withdrawal **parser** (§3) | **MERGED** | `0f2b4730` |
| #531 | Owner-attributed, account-optional **Withdrawal schema + correlation** (§4, ADR-0050) | **MERGED** | `983561ab` |
| #532 | Owner-scoped member **API + UI** + public-aggregation guard (§4/§5) | **MERGED** | `ece05793` |
| #533 | **Board-demo environment** — labelled synthetic seed + demo-visible surface + env plumbing (§5/§6) | **OPEN** (review + CI in flight) | — |

Production remains at `broker_intelligence` schema **0010**; migrations 0011–0016 are unapplied; all WV1 flags are OFF; the member API is `404`. With every WV1/demo env var unset, `settings.py` is byte-identical to today's production.

---

## 2. §2 — Genuine evidence certification (read-only, nothing exfiltrated)

Performed authorized **read-only** checks against the production evidence store (`ssh ubuntu@100.119.23.29` → `guvfx-backend`), printing **only booleans / lengths / classification strings** — never raw content, tokens, addresses, or account/reference digits. The genuine raw RFC822 remains **only** in the production evidence store; nothing was copied into Git/CI/logs/docs.

**Certified facts:**
- The genuine external-withdrawal pair is present and authenticated: **REQUEST** (EvidenceBlob sha `da3c63a5…`, received 2026-10-19 07:55:04) + **COMPLETION** (sha `397e1f4d…`, received 2026-10-19 08:28:05) — **33m01s apart** (email-to-email interval, *not* a system latency).
- Both: content-hash intact (`sha_ok=True`), from `tradersway.com`, `dkim=pass spf=pass`.
- REQUEST carries `Payment method: Ethereum` (line-start), `request number #…`, `to withdraw`, an amount + USD, and **no** `deposit` / `trading account #` token. COMPLETION carries `Payment request #…`, `status changed from Confirmed to Completed`, and **no** rail / direction wording (fewer fields).
- The two share one 7-digit broker reference; both are **owner-29, account-UNRESOLVED** (the TradersWay account is not registered in GuvFX).

**RULE-11 positive control:** the shipped parser logic, run read-only against the genuine bytes, classifies REQUEST = `EXTERNAL_WITHDRAWAL` / `CONFIRMATION_REQUIRED` and COMPLETION = `UNKNOWN` / `COMPLETED` — no under-claim of the real withdrawal, no over-claim.

---

## 3. Separated certifications (§3/§7)

**PARSER CERTIFICATION — PASS.** The single certified parser→`EXTERNAL_WITHDRAWAL` path is the structured, **line-anchored** `Payment method: <whitelisted rail>` field, gated by **four** independent conditions (eligible non-terminal lifecycle + rail label + anchored withdrawal-direction wording + money-in/terminal-negative veto). Three adversarial review rounds fixed every over-claim vector (completion-with-rail, deposit-boilerplate, rejected-withdrawal mis-route, settings-footer prose, money-in `credited`) and a cubic ReDoS. The neutral completion stays `UNKNOWN` — its externality is inherited only by authenticated-reference correlation. Negative regressions (internal transfer, deposit, prose battery) preserved. Validated against the genuine bytes (RULE-11). `broker_intelligence` 620/620 at merge.

**EVIDENCE-CHAIN CERTIFICATION — PASS.** §2 above: genuine pair present, authenticated (dkim+spf pass), content-hash-intact, reference-linked, 33m01s apart.

**ACCOUNT ATTRIBUTION CERTIFICATION — PASS.** The genuine withdrawal is **owner-attributed / account-UNRESOLVED** and is never bound to an unrelated account; no account value is invented. The schema (ADR-0050) carries `Withdrawal.owner` independent of `trading_account` (nullable), and correlation keys owner-attributed events on the owner. Cross-user isolation proven; a same-owner cross-broker reference-collapse limitation is documented (LOW, safe under-count, unreachable in the single-broker pilot — ADR-0050).

**PRODUCTION PROMOTION — NO-GO (gated).** Promotion to production is a **separate, human-gated** decision and has **not** occurred. See §6 gates.

---

## 4. What was built (DARK)

- **Parser** (`parsers_tradersway.py`): certified external classifier + neutral completion recognition + reference linkage.
- **Schema** (`0016`, reversible, unapplied in prod): `Withdrawal.trading_account` nullable + `owner` FK (SET_NULL) + split partial-unique (account-scoped ∪ owner-scoped) + `(owner,status)` index.
- **Correlation**: owner-scoped projection of account-UNRESOLVED events; unified reference finder with an `(owner,ref)` bridge (no double-count); **advance-only completion-by-reference** (a completion can never create or inflate a withdrawal — externality is only ever asserted by the request).
- **Member API/UI**: `withdrawal_history` / `withdrawal_metrics` include owner-attributed account-NULL rows; `account_unresolved` + the "Account unresolved" label; `broker_public` explicitly excludes account-UNRESOLVED rows from per-broker public metrics.
- **Demo env** (#533): `broker_withdrawal_demo_include_synthetic` flag (labels synthetic rows "DEMONSTRATION" without relaxing prod's REAL-only barrier); `seed_wv1_board_demo` (prod-guarded, idempotent — 10 accounts incl. two same-broker + the genuine reconstruction through the real engine); additive env-driven CORS/CSRF **and** JWT-cookie attributes (prod byte-identical when unset); runbook `docs/WV1_BOARD_DEMO_RUNBOOK.md`.

**Test posture:** `broker_intelligence` 669/669 + `broker_public` green + frontend build (parity + tsc + next) green at the demo-env PR. Local end-to-end: seeded `guvfx_wv1_demo`, the account-UNRESOLVED reconstruction `COMPLETED` with `duration_seconds()==1981` (=33m01s), idempotent; the dashboard rendered it live (screenshots in `docs/wv1_demo_evidence/`).

---

## 5. Monday demo sequence (rehearsed)

Run the isolated demo per `docs/WV1_BOARD_DEMO_RUNBOOK.md` (separate DB, demo env, flags ON). Suggested narrative:

1. **Broker Accounts** (`/accounts`) — ten linked accounts including **two at the same broker** (Pepperstone ×2), each with broker/server/masked-number/Demo badge. *"GuvFX maps strategies and trades across many broker accounts, including several at one broker."*
2. **Connected Email** (`/profile`) — the connected-mailbox panel (the owner/mailbox basis of attribution). *"Withdrawal intelligence reads the member's broker emails — read-only, consented."*
3. **Withdrawal Monitoring + History** (`/dashboard`) — **the headline**. The **DEMONSTRATION DATA** banner, then the owner-attributed/account-UNRESOLVED TradersWay withdrawal (`250.00 USD · ⚠ Account unresolved · 33 min · ref DEMO-7654321 ✓ · Completed`), plus the spread of other withdrawals; truthful metrics (`Genuine samples: 0`, per-currency totals, typical processing time). *"We detected an external crypto withdrawal from an authenticated broker email, linked request→completion by the broker's own reference, and surfaced it to the member even though that broker account isn't registered in GuvFX — we never invent an account."*
4. **WAYOND preview** (`/strategies/marketplace`) — the `Wayond WIM Strategy` card (the shipped Wayond surface). *(See Open decision D-1.)*

**Truthfulness callouts for the room:** every row is synthetic and labelled "DEMONSTRATION"; the `33 min` is the real email-to-email interval, not a latency SLA; `Genuine samples: 0` because the demo data is synthetic (the *genuine* pilot withdrawal is real and lives in production, reconstructed here only in structure).

---

## 6. Friday acceptance — PASS/BLOCKED

| # | Objective (packet) | Verdict | Evidence |
|---|---------------------|---------|----------|
| 1 | Genuine evidence certified (read-only, no exfiltration) | **PASS** | §2; booleans-only host probes |
| 2 | Genuine request/completion parser | **PASS** | PR #530; RULE-11 control; 3 review rounds |
| 3 | Correct account attribution (owner-attributed / account-UNRESOLVED; never invented) | **PASS** | PR #531 ADR-0050; correlation tests |
| 4 | Customer-facing withdrawal metrics | **PASS** | PR #532; owner-scoped API + UI; rendered live |
| 5 | Ten-account monitoring presentation (incl. 2 same-broker) | **PASS** | PR #533 seed; `/accounts` + `/dashboard` screenshots |
| 6 | WAYOND integration preview | **PASS (surface = marketplace card)** | existing `Wayond WIM Strategy`; Open decision D-1 |
| 7 | Safe demo environment + rehearsal | **PASS** | runbook; local end-to-end walkthrough + screenshots |
| — | Separated certifications (parser / evidence-chain / attribution / promotion) | **PASS / PASS / PASS / NO-GO (gated)** | §3 |
| — | Production activation | **NO-GO (unchanged, gated)** | §6 gates below |

**Everything demonstrable is demonstrated on an isolated environment; nothing in production changed.**

---

## 7. Remaining Sponsor / human-gated actions (unchanged by this packet)

These are **STOP** items — Sponsor/human decisions, not engineering:

- **G1 — Production DB-recovery gate (blocks any prod migration).** `DATABASE_RECOVERY_READINESS = BLOCKED`: no off-host verified recovery point; recovery tooling certified only on synthetic PG17, not PG15/Linux. The prod migration-first deploy of `broker_intelligence` 0011→0016 (+ `broker_public` 0001/0002) is **NO-GO** until an off-host encrypted destination + a PG15/Linux restore rehearsal produce a certified recovery point. *(Sponsor + cost sign-off.)*
- **G2 — Production certification of the live withdrawal.** Promotion to a production-visible member feature requires a genuine Sponsor-initiated withdrawal observed end-to-end in production with the flags armed. **GuvFX never initiates, confirms, or cancels a withdrawal** — the Sponsor performs it at the broker.
- **G3 — Google OAuth publish-to-Production** (if customer mailbox onboarding at scale is wanted) — a human action in the Google Console.
- **G4 — Open decision D-1 (WAYOND surface):** confirm whether the Board "WAYOND preview" means the `Wayond WIM Strategy` marketplace card (default taken here) or something else. Presentation-level; no engineering blocker.

---

## 7a. Deployment dependency register (verify before ANY prod deploy of these commits)

Prod byte-equivalence holds **only while production leaves the following env vars UNSET** (confirmed: no tracked `.env`/compose sets them; the only places they appear are the demo runbook). Before deploying these commits to production, verify against the real VPS `.env`/compose that **all** of these are unset:
`CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`, `CSRF_COOKIE_DOMAIN`, `AUTH_COOKIE_SECURE`, `AUTH_COOKIE_SAMESITE`, `AUTH_COOKIE_DOMAIN`, `BROKER_WITHDRAWAL_UX_ENABLED`, `BROKER_WITHDRAWAL_DEMO_INCLUDE_SYNTHETIC`. If any is set in prod, re-evaluate: a set CORS/CSRF origin widens the credentialed allowlist, an empty `CSRF_COOKIE_DOMAIN`/`AUTH_COOKIE_DOMAIN` nulls the cross-subdomain cookie (guvfx.com↔api.guvfx.com), and an armed `BROKER_WITHDRAWAL_*` surfaces the member feature. (All are additive/override with production defaults; none narrows a prod origin.)

## 8. Governance, safety & known limitations

- **All DARK.** No production migration, deploy, flag-flip, publication, purchase, or financial/credential action occurred. Member API `404` in prod; demo flags and demo CORS/CSRF/auth-cookie env vars are unset in prod ⇒ `settings.py` byte-identical to today.
- **No real evidence leaves prod.** The demo reconstructs the genuine withdrawal's *structure* from placeholders (no real token/address/account); the genuine RFC822 stays only in the production evidence store.
- **Truthful by construction.** Demo data is SYNTHETIC and labelled "DEMONSTRATION"; member metrics are REAL-only in production (the demo-include flag is OFF there); account-UNRESOLVED withdrawals never feed public per-broker metrics.
- **AMBER decisions flagged:** the `Withdrawal` schema extension (ADR-0050) and the additive env-driven CORS/CSRF + JWT-cookie config (prod defaults preserved) — both documented, both prod-safe when env unset.
- **Known limitation (LOW):** cross-broker same-reference collapse for one owner is a safe under-count, unreachable in the single-broker pilot; the robust fix (a `Withdrawal.broker` field) is deferred (ADR-0050).
- **Adversarial review:** every PR went through a find→verify review; confirmed defects — 5 (parser) + 3 (parser re-review) + 1 (parser round-3) + 1 HIGH (correlation double-count) — were all fixed with regressions before merge. PR #533 review + CI in flight at report time.

---

## 9. One bounded next action

**Merge PR #533** once its adversarial review and CI are green (fix any confirmed finding first), completing the demo-environment deliverable on canonical `main`. All production activation remains behind the human gates in §7.
