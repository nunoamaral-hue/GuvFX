# GUVFX_WV1_MONDAY_DEMO_FINAL_ACCEPTANCE

**Directive:** WV1 Friday Board-Demo Final Integration · **Baseline:** canonical `main` @ `48ecd20a` (verified)
**Engineering deadline:** Fri 2026-10-23 · **Board demo:** Mon 2026-10-26 · **Date:** 2026-10-20
**Posture:** DARK / isolated demo only. No production deploy, migration, flag flip, publication, live trading, or new
Sponsor withdrawal. The MT5 Copier workstream (PR #528) is untouched.

This closes the six presentation-critical gaps from the PD directive + closeout review on PR #534, without reopening
the architecture. One coherent bounded delivery on top of the merged WV1 foundation.

---

## 1. Per-feature acceptance (Friday)

| # | Item | Verdict | Evidence |
|---|------|---------|----------|
| 1 | **WTM broker-metrics preview** (not the strategy card) | **PASS** | New `/brokers/wtm` reusing `broker_public`; authentic `INSUFFICIENT_VERIFIED_DATA` + labelled SIMULATED scorecard + flow + badges + broker filter. `docs/wv1_demo_evidence/wtm_preview_*.jpg` |
| 2 | **Genuine TradersWay evidence case study** (private, accurate) | **PASS** | New `/brokers/case-study`: real **$24.12** · Ethereum · **33m01s** · owner-attributed/account-UNRESOLVED · DKIM+SPF · broker-reported completion only; redacted; separate from synthetic. `genuine_case_study.jpg` |
| 3 | **Ten ACTUAL MT5 account inventory/status** (≥2 same-broker) | **PASS (truthful) / PARTIAL on same-broker** | Read-only estate inventory below. Real same-broker *external* accounts do **not** exist; two same-broker accounts **with empirical HEALTHY data** are the hosted-MT5 terminals 25 & 33. Synthetic seeded 10 are **not** claimed as connectivity. |
| 4 | **Frontend cleanup CI fix** (PR#531 source) | **PASS** | `AccountWithdrawalMonitoring.tsx` unmount guard (`mountedRef`) + negative regression `AccountWithdrawalMonitoring.test.tsx`; final-head CI to confirm on the PR. |
| 5 | **Reliable isolated demo (A–F) + rehearsal + fallback** | **PASS** | All six screens verified live on the isolated demo; six screenshots in `docs/wv1_demo_evidence/` serve as the offline fallback. Sequence below. |
| 6 | **Governance / final CI** | **PASS (pending CI + review on this PR)** | DARK; prod untouched; tests green; adversarial review + CI on the PR before governed squash-merge (auto-merge OFF). |

**Production activation: NO-GO (unchanged).** See §6.

---

## 2. Environment & access

- **Isolated demo environment:** the same `settings.py` driven by a demo `.env` against a **separate** database
  `guvfx_wv1_demo` (never production). Stand-up + teardown + env vars: `docs/WV1_BOARD_DEMO_RUNBOOK.md`.
- **Local URLs (as rehearsed):** frontend `http://localhost:3000`, backend `http://localhost:8000`
  (`NEXT_PUBLIC_API_BASE=http://localhost:8000`). A hosted isolated demo uses its own origin via the additive
  `CORS_ALLOWED_ORIGINS` / `CSRF_TRUSTED_ORIGINS` / `AUTH_COOKIE_*` env (prod byte-identical when unset).
- **Demo member login:** username `demo-board`, email `demo-board@example.invalid` (a throwaway account on the
  isolated demo DB). **Credentials handoff (no password in Git/chat):** the operator sets the password locally, once,
  with the `manage.py shell` snippet in the runbook (`u.set_password(...)`), and shares it **out-of-band** (e.g. a
  password manager / secure channel) with whoever drives Monday's screenshare. No password is committed or printed
  in this report, the runbook, or any tracked file.
- **Flags armed in the demo env only** (all default OFF; prod leaves them unset): `BROKER_WITHDRAWAL_UX_ENABLED`,
  `BROKER_WITHDRAWAL_DEMO_INCLUDE_SYNTHETIC`, `BROKER_MAILBOX_STATUS_ENABLED`, `BROKER_PUBLIC_WTM_PREVIEW_ENABLED`.

---

## 3. Real vs synthetic sources (explicit)

- **REAL (authenticated, production-only):** exactly ONE withdrawal — the genuine TradersWay external pilot pair
  (request 2026-10-19 07:55:04 + completion 08:28:05, 33m01s, **$24.12**, Ethereum, owner-29, account-UNRESOLVED;
  DKIM+SPF pass; content-hash intact). It lives immutably in the **production** evidence store and was confirmed by
  **read-only** checks (booleans/values only, nothing exfiltrated). It is presented ONLY as the redacted
  `/brokers/case-study` page — never intermingled with synthetic dashboard or WTM figures.
- **SYNTHETIC / SIMULATED (isolated demo DB / in-code):** everything else — the 10 seeded demo accounts, the 8 seeded
  withdrawals (incl. the `DEMO-7654321` / `$250` reconstruction), the demo mailbox, and the WTM scorecards. All are
  `provenance=SYNTHETIC` and/or `simulated: true` and labelled "DEMONSTRATION" / "SIMULATED" in the UI. The synthetic
  `$250` reconstruction is **not** the real withdrawal and is never presented as a live GuvFX transaction.

### Read-only production account-estate inventory (item 3)
Confirmed read-only on production (`ssh ubuntu@100.119.23.29`), masked:

- **16 TradingAccounts total: 3 REAL (`is_demo=False`), 13 demo.** All 3 real accounts belong to user-29 and are
  **inactive**: IS6FX NR `…3830`, TradersWay `…5442`, DominionMarkets `…8736` — **one per broker, so NO real
  same-broker pair**, and none has a HEALTHY health snapshot (not live-connected).
- **Accounts with real empirical HEALTHY observations** (`reliability.TradingHealthSnapshot`, ~891k rows): **25 & 33
  (both "Hosted Workspace"), 35 (Pepperstone), 36 (Taurex), 1 (IS6 Technologies)** — all active + `PROVISIONED`.
- **Two distinct same-broker accounts with empirical data = hosted-MT5 terminals 25 & 33** (both "Hosted Workspace",
  both HEALTHY) — the honest same-broker demonstration (real connection/health; demo funds).
- **Conclusion:** the demo's ten accounts prove the **UI/data plumbing**, not ten healthy real MT5 terminals. Two
  same-broker accounts *with real data* exist only as the hosted-MT5 pair (25/33). This is stated plainly to the
  Board; actual and seeded accounts are kept unmistakably separate.

---

## 4. Monday presentation sequence (rehearsed) — A–F

Stand up the isolated demo per the runbook, then, logged in as `demo-board`:

1. **A — Broker account overview** (`/accounts`): the ten demo accounts incl. two at the same broker (Pepperstone
   ×2). *State plainly: these are demo rows illustrating the roster UI, not ten live MT5 terminals.* `broker_accounts.jpg`
2. **B — Connected email management** (`/profile`): the read-only Gmail mailbox, "Connected & monitoring", "cannot
   send email or move funds". `connected_email.jpg`
3. **C/E — Account-level withdrawal monitoring + charts/history** (`/dashboard`): the DEMONSTRATION banner, the
   owner-attributed/account-UNRESOLVED row, per-currency totals, typical processing time, `Genuine samples: 0`.
   `dashboard_withdrawal_intelligence.jpg`
4. **D — Genuine TradersWay evidence case study** (`/brokers/case-study`): the ONE real withdrawal ($24.12, Ethereum,
   33m01s, account-unresolved) — *real evidence, authenticated on production, redacted, separate from the synthetic
   totals.* `genuine_case_study.jpg`
5. **F — WAYOND Trader Match preview** (`/brokers/wtm`): authentic `INSUFFICIENT_VERIFIED_DATA` + the SIMULATED
   broker scorecard + the GuvFX-evidence→aggregate→WTM flow. *A preview of future analytics — not a deployed WAYOND
   integration, no public rating.* `wtm_preview_scorecard.jpg`, `wtm_preview_flow_methodology.jpg`

**Fallback:** the six screenshots in `docs/wv1_demo_evidence/` are an offline record if the live env is unavailable;
a screen recording can be captured from the same click-through.

---

## 5. What shipped (DARK)

- **Frontend reliability (item 4):** `AccountWithdrawalMonitoring.tsx` — `mountedRef` unmount guard on all async
  setState (mount fetches + user actions); regression `AccountWithdrawalMonitoring.test.tsx`.
- **WTM preview (item 1):** `broker_public` — `broker_public_wtm_preview_enabled()` flag (default OFF),
  `wtm_preview.py` (pure, deterministic, synthetic — reads NO member/withdrawal/broker data, touches NO
  `CERTIFIED_BROKERS`), `PublicWtmPreviewView` at `GET /api/public/wtm-preview/` (404 when off); frontend
  `/brokers/wtm` page. Tests in `tests_public_metrics.py` + `wtm/page.test.tsx`.
- **Genuine case study (item 2):** static frontend `/brokers/case-study` (redacted facts only; no API, no PII).
  Test `case-study/page.test.tsx`.
- **Demo completeness:** `seed_wv1_board_demo` also seeds a CONNECTED demo mailbox (screen B); runbook updated with
  the two new flags + the two new screens.

Tests: `broker_public` incl. WTM (26) + seed (4, incl. mailbox) + withdrawal-history (all) green; frontend new tests
+ production build (parity 52 routes, tsc, next) green. No migration.

---

## 6. Remaining blocked acceptance criteria (human-gated — unchanged)

- **G1 — Production migration NO-GO.** Prod `broker_intelligence` is at 0010; main is through 0016 (+ `broker_public`
  0002). The off-host encrypted backup + PG15/Linux restore is **not** certified (`DATABASE_RECOVERY_READINESS`
  BLOCKED). No prod migration/schema write until it clears (Sponsor + cost).
- **G2 — Production certification of the live withdrawal** needs a genuine Sponsor-initiated withdrawal observed live;
  **GuvFX never initiates/confirms/cancels.** No new Sponsor withdrawal is required for the demo.
- **G3 — Google OAuth production publishing** unresolved (human, Google Console).
- **G4 — WAYOND preview surface: RESOLVED** by Sponsor = WAYOND Trader Match broker metrics (delivered). No further
  Sponsor question.
- **Item-3 same-broker (empirical, REAL external broker):** BLOCKED — no two real external-broker accounts exist;
  demonstrated instead with the hosted-MT5 same-broker pair (25/33), stated truthfully.

No public broker rating is published; no aggregate derived from one customer's withdrawal; no real WAYOND domain used;
no NAS production mutation; no live trading. All demo flags + demo CORS/CSRF/AUTH_COOKIE env remain unset in
production (deploy-time checklist: `GUVFX_WV1_FRIDAY_BOARD_DEMO_REPORT.md` §7a).

---

## 7. One bounded next action
Run the Board demo on the isolated demo DB per the runbook on Monday 2026-10-26; keep production untouched until the
G1 DB-recovery gate clears.
