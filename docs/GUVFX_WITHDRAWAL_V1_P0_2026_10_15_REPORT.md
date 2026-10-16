# GUVFX_WITHDRAWAL_V1_P0_2026_10_15_REPORT

**Packet:** GUVFX_WITHDRAWAL_V1_P0_2026_10_15
**Author:** Principal Engineer (Claude)
**Date/time of inspection:** 2026-10-15, ~08:51–08:56 UTC (production), read-only throughout.
**Scope of authority used:** READ-ONLY production inspection (SSH `ubuntu@100.119.23.29`; `docker exec guvfx-backend … manage.py shell` SELECT-only; `journalctl` read) + LOCAL synthetic test run. **No** production mutation, **no** Gmail replay/live API call, **no** credential decryption, **no** flag/activation change, **no** Docker recreation.

---

## 0. Headline

1. **Housekeeping (§2):** PR #498 (DB Recovery Foundation, DARK) was merged under the granted conditional authorization → `main` squash `26c8eeb`. PR #499 (deflake) previously merged `556335b`. Repo is clean on `main`.
2. **NEW MATERIAL FINDING (overrides the earlier "ingestion operational now" read):** the `guvfx-broker-ingest.timer` is active and the systemd **unit exits 0**, but **every ingestion pass since 2026-10-14 20:12:54 UTC has FAILED** at the Gmail fetch layer (`mailbox ingest failed for mailbox 1: RuntimeError`, events=0, ingested=0). `last_successful_sync` is frozen at **2026-10-14 20:12:54 UTC (~12.7h stale)**. The pipeline behaved **correctly and fail-closed** (cursor NOT advanced, no data loss, no mis-ingest) — but the unit-level green masked the mailbox-level hard failure. See §3-A/B/C and §8.
3. **Genuine external-withdrawal evidence: STILL ABSENT.** Classification outcome `EXTERNAL_WITHDRAWAL_EVIDENCE_BLOCKED`. The only withdrawal-lifecycle observation captured is one `WITHDRAWAL_CONFIRMATION_REQUIRED` / category `UNKNOWN` (a confirmation-request precursor that TradersWay also uses for internal transfers). Zero `Withdrawal` rows; zero `EXTERNAL_WITHDRAWAL` events; `CERTIFIED_BROKERS` is empty (fail-closed).
4. **Product logic (sample-independent):** `CERTIFIED_WITH_SYNTHETIC` — 348/348 `broker_intelligence` tests pass locally.
5. **`TRADERSWAY_WITHDRAWAL_PILOT_READY`: BLOCKED** on two things, in order: (a) restore live ingestion (operational re-auth — Sponsor/ops), then (b) the Sponsor performs one genuine smallest-safe external withdrawal to mint the first real sample. Exact Sponsor action in §9.

---

## 1. §2 — PR #498 merge (housekeeping, completed)

| Gate | Result |
|---|---|
| Base unchanged at authorization | `main` was `556335b`; unchanged at merge |
| Reviewed head | `365af143` |
| CI (all required checks) | SUCCESS |
| Mergeable / clean | Yes |
| Scope DARK (ops/recovery + docs only) | Confirmed |
| Governed squash merge | `26c8eeb` (final `main`) |
| Auto-merge | left OFF |

Production activation of the recovery foundation remains **NO-GO / Sponsor-gated** (unchanged): no backup active, no off-host copy, P0 root FS ~96% full. Nothing in this packet changed that.

---

## 2. §3 — Current production state (READ-ONLY)

Evidence commands: `systemctl status guvfx-broker-ingest.timer`; `journalctl -u guvfx-broker-ingest.service`; `docker exec -i guvfx-backend python manage.py shell` (two SELECT-only snippets, archived in the session scratchpad).

| # | Item | Observed | Verdict |
|---|---|---|---|
| A | Ingestion armed | `guvfx-broker-ingest.timer` **active + enabled**; 5-min cadence; flag `BROKER_INTELLIGENCE_INGEST_ENABLED=1` | ARMED |
| A′ | Ingestion **healthy** | **NO — failing every pass since 2026-10-14 20:12:54 UTC** (`RuntimeError`, events=0). Unit exits 0 (masks it). | **DEGRADED** |
| B | Last successful ingestion | `ConnectedMailbox.last_successful_sync = 2026-10-14 20:12:54 UTC` (mailbox `updated_at` matches) | ~12.7h stale |
| C | Last failure | Continuous since ~20:12 UTC 2026-10-14; latest observed pass 08:54:56 UTC 2026-10-15 → `failed=1` | Ongoing |
| D | Gmail OAuth validity | Token *refresh* path is NOT the failing one (error class is `RuntimeError`, not `GmailOAuthError`). Most-probable cause = persistent non-200 from the Gmail REST call ⇒ **consent/token likely revoked server-side**. Not decrypted/confirmed (Red line). | Suspect — re-auth likely |
| E | OAuth scope | `scopes = https://www.googleapis.com/auth/gmail.readonly` (read-only; no write/modify scope) | PASS |
| F | Cursor / processing | `cursor_state = 3204241` (Gmail historyId) — **not advanced** during failures (fail-closed; idempotent re-fetch on recovery) | CORRECT |
| G | TradersWay identity / acct46 | `BrokerEmailIdentity id=1` broker=`TradersWay`, acct=46, user=29, status=**VERIFIED**, origin=BROKER_REGISTERED, mailbox=None | PASS |
| H | IS6 identity / acct47 | `BrokerEmailIdentity id=2` broker=`IS6`, acct=47, user=29, status=**VERIFIED**, mailbox=1. (acct47 `is_active=False` per standing state.) | PASS |
| I | Parser registration | `register_default_parsers()` registers `('tradersway','v1')` + `('is6','v1')` | PASS |
| I′ | Profile parser field | `BrokerProfile pk=1 (tradersway)` has `parser_name='tradersway'`; **`pk=2 (is6)` has `parser_name=''`** (registration is code-driven so ingest still works, but the catalogue row is incomplete) | Minor gap |
| J | Evidence / events | `EvidenceBlob=6` (ids 1–6; pk6 new 2026-10-13 21:09 UTC), `BrokerEvent=1`, `Withdrawal=0`. Blobs **with** event: `[1]`; **without**: `[2,3,4,5,6]` | See §3 detail |
| K | Withdrawal projection | `Withdrawal=0` | None yet |
| L | Correlation / metrics | Flags `BROKER_WITHDRAWAL_CORRELATION_ENABLED` and `BROKER_WITHDRAWAL_UX_ENABLED` both **unset (OFF/DARK)**; logic present + tested | DARK |
| M | Feature flags | INGEST=1, MAILBOX_CONNECT=1, CORRELATION=unset, UX=unset, IDENTITY_VERIFICATION=unset, **MT5_ALLOW_LIVE=unset** | As expected |
| N | Audit / provenance | `BrokerProfileAudit=2`, `BrokerEmailIdentityAudit=1`; the one event is `provenance=REAL`; `EvidenceBlob`/`BrokerEvent` immutable at app + DB-trigger layers | PASS |

**The one BrokerEvent (pk=1):** `event_type=WITHDRAWAL_CONFIRMATION_REQUIRED`, `transaction_category=UNKNOWN`, `provenance=REAL`, `correlation_status=UNRESOLVED`, acct=46, owner=None, broker=TradersWay, `amount=None`, `currency=''`, `source/destination=''`, `ref=''`, parser=`tradersway/v1`, confidence=0.800, received 2026-10-12 06:36:12 UTC, evidence=blob 1.

---

## 3. §4 — Genuine external-withdrawal evidence check (per broker)

> Determined **without opening raw blob bodies** (privacy / do-not-disclose). The determination rests on the deterministic parser outputs + the structural classifier invariants, which is sufficient and conservative (under-claiming is safe; over-claiming is the forbidden error).

**TRADERSWAY — ACCOUNT 46:** one REAL captured email classified `WITHDRAWAL_CONFIRMATION_REQUIRED` / `UNKNOWN`. Per the TradersWay parser's own documented grammar, "confirm your withdrawal request" is the wording TradersWay **also uses for an internal transfer** — so this is a *confirmation-request precursor with no positive external signal*, not a completed external withdrawal. No amount, no external destination, no settlement rail. → **No genuine external-withdrawal evidence.**

**IS6 — ACCOUNT 47:** **zero** `BrokerEvent` rows of any kind (no deposit, no withdrawal). acct47 `is_active=False`. → **No genuine withdrawal evidence.**

**New blob pk=6 (2026-10-13 21:09 UTC):** captured as immutable evidence but produced **no** lifecycle event (not in `BLOBS_WITH_EVENT`). Since ingestion runs the parsers and emits an event for any recognisable withdrawal/deposit lifecycle message, a no-event capture is a non-transactional / non-recognisable broker email. Even had it parsed, the parsers can only ever emit `INTERNAL_TRANSFER`/`UNKNOWN`/`DEPOSIT_*` — **there is no code path from parser text to `EXTERNAL_WITHDRAWAL`** (structural, three-round-reviewed). → does not change the picture.

**Conclusion:** `GENUINE_EXTERNAL_WITHDRAWAL_EVIDENCE_AVAILABLE = FALSE` → **`EXTERNAL_WITHDRAWAL_EVIDENCE_BLOCKED`** (see §6 path). This is unchanged from the 2026-10-13 baseline; the one new capture (pk6) is not withdrawal-relevant.

---

## 4. §7 — Sample-independent product certification (synthetic)

Local run: `cd backend && .venv/bin/python manage.py test broker_intelligence` → **Ran 348 tests … OK** (17.1s). The emitted `RuntimeError` / `unauthenticated broker sender` / `Not Found` lines are **negative-path fixtures**, not failures. This certifies, against labelled synthetic fixtures, the full chain: Gmail-source fetch error handling (incl. the exact `fetch_failed`/`RuntimeError` branch now firing in prod), authentication gate, evidence capture + immutability, both parsers, lifecycle + category classification, the external-withdrawal classifier invariants (internal≠external, deposit≠withdrawal, ambiguous→UNKNOWN, uncertified broker→UNKNOWN), correlation, metrics, and the read-model/UX views.

**State vocabulary:** product logic = `CERTIFIED_WITH_SYNTHETIC`; nothing here is `GENUINE` (no genuine external sample) or `IN_PRODUCTION` for the positive-withdrawal path.

---

## 5. §8 — Verified sample-independent blockers

**BLOCKER-1 (operational, Red — Sponsor/ops): live ingestion is wedged.** Gmail fetch returns a persistent non-200 ⇒ `RuntimeError` every pass since 2026-10-14 20:12 UTC. Most-probable cause = Gmail consent/token revoked. **Fix is a re-authorisation of the read-only Gmail grant — a credential/ops action I must not perform.** The pipeline recovers on its own once the grant is valid (idempotent re-fetch from the unadvanced cursor `3204241`; no data lost).

**BLOCKER-2 (product observability, in-scope code — proposed, NOT yet implemented): the failure is both silent and undiagnosable.**
- *Silent:* a persistent hard failure of the **only** mailbox does not fail the systemd unit (exit 0) — "timer green" ≠ "ingestion healthy." A 12h outage went unsurfaced.
- *Undiagnosable:* the worker logs only `type(exc).__name__` ("RuntimeError"), discarding the HTTP status that `gmail_source.py` already puts in the message — so an operator cannot tell `no stored credential` (line 362) from `gmail api http 401/403/5xx` (line 379) from `gmail profile http …` (line 495). Per RULE 11, a negative operational finding must be diagnosable; the HTTP status and error class are **not secrets**.

Proposed remediation (governed DARK PR, adversarial review), split by decision colour:
- **Green (additive, behaviour-preserving):** log `error class + HTTP status + mailbox id` secret-free (never the token/body); surface `last_error`/`consecutive_failures`/staleness on `ConnectedMailbox` for operator visibility.
- **Amber (Sponsor decision — changes live operational semantics):** make a persistent single-mailbox hard-failure escalate (non-zero unit exit **or** an alert) so a wedged ingestion is not green. This touches the live timer's success semantics and so needs an explicit decision, not an in-passing edit.

**Minor (not a blocker):** `BrokerProfile pk=2 (is6)` has an empty `parser_name` catalogue field (registration is code-driven, so ingest is unaffected). Tidy in the same PR if BLOCKER-2 proceeds.

> I did **not** implement BLOCKER-2 in this packet: modifying the live DARK ingestion worker during an active outage, and changing unit exit semantics, warrants Sponsor sign-off first (Amber). Awaiting go.

---

## 6. §10 — Milestone matrix

| Milestone | Verdict | Basis |
|---|---|---|
| WITHDRAWAL_INGESTION_OPERATIONAL | **BLOCKED (DEGRADED)** | Armed + fail-closed-correct, but live fetch failing ~12.7h; operational re-auth needed. Not a code/product defect. |
| TRADERSWAY_IDENTITY_VERIFIED | **PASS** | Identity id=1, acct46, VERIFIED |
| IS6_IDENTITY_VERIFIED | **PASS** | Identity id=2, acct47, VERIFIED (acct47 is_active=False) |
| GENUINE_EXTERNAL_WITHDRAWAL_EVIDENCE_AVAILABLE | **BLOCKED** | `EXTERNAL_WITHDRAWAL_EVIDENCE_BLOCKED`; only a confirmation-request precursor exists; 0 Withdrawal rows |
| TRADERSWAY_POSITIVE_CLASSIFIER_CERTIFIED | **BLOCKED** | No genuine external sample; `CERTIFIED_BROKERS` empty (fail-closed by design) |
| IS6_POSITIVE_CLASSIFIER_CERTIFIED | **BLOCKED** | Same; plus IS6 catalogue `parser_name` blank (minor) |
| WITHDRAWAL_CORRELATION_CERTIFIED | **PASS (synthetic, DARK)** | Logic certified in tests; flag OFF; not prod-exercisable (no external events) |
| WITHDRAWAL_METRICS_CERTIFIED | **PASS (synthetic, DARK)** | Certified in tests; flag-gated |
| WITHDRAWAL_UX_CERTIFIED_IN_TESTS | **PASS (in-tests, DARK)** | Views tested; `BROKER_WITHDRAWAL_UX_ENABLED` OFF |
| TRADERSWAY_WITHDRAWAL_PILOT_READY | **BLOCKED** | Gated on BLOCKER-1 (re-auth) then the Sponsor's first genuine withdrawal (§9) |

---

## 7. §9 — Exact Sponsor action for `TRADERSWAY_WITHDRAWAL_PILOT_READY`

Two steps, in order. **GuvFX never initiates, confirms, or cancels a withdrawal — every step below is performed by the Sponsor directly.**

**Step 1 — Restore ingestion (operational).** Re-authorise the read-only Gmail grant for the connected mailbox (`nr…@googlemail.com`, provider id `nrfda1111@gmail.com`) so the Gmail API stops returning a non-200. Scope must stay `gmail.readonly` only. Once valid, ingestion self-heals from cursor `3204241` within one 5-min cycle; confirm by watching `last_successful_sync` advance and a pass log `failed=0`. (If re-consent alone does not clear it, capture the HTTP status — BLOCKER-2's logging fix makes this one-glance; otherwise an operator can read it from a single manual `--force` dry-run.)

**Step 2 — Mint the first genuine external-withdrawal sample.** From the TradersWay client area for account 46, the Sponsor performs **one smallest-safe genuine external withdrawal** (money actually leaving TradersWay to an external destination the Sponsor controls — not an internal/cabinet transfer), and lets the broker's confirmation emails arrive at the connected mailbox. GuvFX will then (read-only) capture them as immutable evidence; the engineer certifies the TradersWay positive classifier's *extraction* against that genuine email (adversarial review, DARK), adds `TradersWay` to `CERTIFIED_BROKERS`, and only then can an `EXTERNAL_WITHDRAWAL` event + `Withdrawal` row be produced. No flag/live-metric is turned on without a separate governance decision.

Until both steps complete, `TRADERSWAY_WITHDRAWAL_PILOT_READY` stays BLOCKED. Under-claiming (a real external read as UNKNOWN) is safe; the pilot exists precisely to replace that safe gap with certified positive extraction.

---

## 8. Verified vs assumed

- **Verified (observed):** timer active+enabled; unit exits 0 while every pass since 2026-10-14 20:12:54 UTC reports `failed=1`/`RuntimeError`; `last_successful_sync`/cursor values; all DB counts/rows and flags above; error class is `RuntimeError` (not `GmailOAuthError`); container `guvfx-backend` Up 47h (predates the failure boundary → not a deploy regression); 348/348 synthetic tests pass.
- **Assumed / inferred (hedged):** the *specific* non-200 (401 vs 403 vs 5xx) and therefore "consent revoked" as the exact cause — **not confirmed**, because confirming it would require decrypting the production token or a live Gmail call (both out of bounds). I narrowed it to the `gmail_source.py` RuntimeError sites and excluded a refresh failure; the precise status is unproven (RULE 11 — I do not assert it as fact).
- **Not covered:** raw blob body contents (deliberately unopened); any write/activation path; the exact Gmail HTTP status.

---

## 9. Bounded next action

**One step:** obtain Sponsor go/no-go on BLOCKER-2's **Green** remediation (secret-free diagnostic logging + `ConnectedMailbox` staleness/last-error visibility) as a governed DARK PR with adversarial review — so the next ingestion wedge is immediately diagnosable — holding the **Amber** exit-code/alerting change for a separate explicit decision. (Restoring ingestion itself, BLOCKER-1, is the Sponsor's operational re-auth and is not an engineering action.)

---

*Standing safety constraints observed in full: no production mutation, no Gmail replay, no credential decryption, no flag/activation change, no Docker recreation, no financial action, no secrets disclosed. GuvFX never initiates/confirms/cancels a withdrawal.*
