# GUVFX Withdrawal Intelligence — Production General-Release Plan

**Objective:** a genuine, owner-scoped Withdrawal Intelligence feature in production for **every authenticated** GuvFX
user — operating on genuine, user-owned evidence (no support-only demo, no synthetic production data, no public page
standing in for the real feature). **Trading impact: none.** GuvFX never initiates, confirms, cancels or moves a
withdrawal.

**Posture:** DARK. This packet delivers the safe engineering + release preparation. **Production deploy, migration,
flag activation, and OAuth publishing are NOT performed here** — they are human/Sponsor/cost-gated (see §7).

**Base:** canonical `main` `d5e4f22e` (verified). Branch: `feat/wv1-withdrawal-intelligence-release`.

---

## 1. Verified current state (read-only)

**Production (`ssh ubuntu@100.119.23.29`, read-only):**
- Deployed code is **behind `main`**: `broker_public` is **not an installed app** on the deployed image; `broker_intelligence` is at migration **0010** (main is through **0016**).
- Feature flags: none of the release flags are set in the container → **all default OFF**; only `BROKER_MAILBOX_CONNECT_ENABLED` is set (the already-recovered ingestion).
- Affected-table row counts (for lock-risk): `withdrawal=0`, `brokerevent=2`, `evidenceblob=9`, `connectedmailbox=1`, `brokeremailidentity=2` — tiny.

**Implementation on `main` (audited):**
- **Owner-scoping is genuine and production-grade.** Every member endpoint filters by `request.user`; **no staff/superuser bypass**, **no IDOR** (foreign ids resolve to 404/empty), mailbox anti-hijack via a user-less `(provider, provider_mailbox_id)` unique. Account-UNRESOLVED evidence requires `owner=user AND trading_account IS NULL`.
- **Synthetic excluded by default.** `provenance` defaults `SYNTHETIC`; member projections are REAL-only unless the separate `BROKER_WITHDRAWAL_DEMO_INCLUDE_SYNTHETIC` flag is ON (never in prod). No demonstration banner / synthetic rows in the genuine production experience.
- **Gmail OAuth is least-privilege + encrypted.** Read-only scope enforced fail-closed; tokens Fernet-encrypted off-DB (DB holds only a `credential_ref`); nothing logged.

---

## 2. Completion-state matrix (§16)

| State | Status | Basis |
|---|---|---|
| `ENGINEERING_COMPLETE` | **YES (this PR, DARK)** | Permanent `/withdrawal-intelligence` page + nav + i18n + evidence/empty states; owner-scoping already complete; anonymous-denial tests added |
| `SECURITY_REVIEW_COMPLETE` | **YES (review)** | Owner-scoping / IDOR / staff-bypass / token-storage audited clean; adversarial review on the PR |
| `PRIVACY_REVIEW_COMPLETE` | **YES (review)** | Read-only scope, redaction, no secrets in logs/payloads audited; see §8 |
| `OAUTH_PRODUCTION_READY` | **BLOCKED** | Google production publishing/verification is a human console action (no access) |
| `RECOVERY_CERTIFIED` | **BLOCKED** | No off-host encrypted backup + verified PG15 restore; needs infra (possible cost) + Sponsor |
| `MIGRATIONS_APPLIED` | **NO** | Gated on RECOVERY_CERTIFIED + authorization |
| `CODE_DEPLOYED` | **NO** | Gated on production deploy authorization |
| `INGESTION_ENABLED` | **NO** | Staged activation step; gated |
| `CORRELATION_ENABLED` | **NO** | Staged activation step; gated |
| `CUSTOMER_UX_ENABLED` | **NO** | Requires migrations applied first (or endpoints 500); gated |
| `SUPPORT_ACCOUNT_VERIFIED` | **NO** | Post-activation smoke test |
| `OWNER_ISOLATION_VERIFIED` | **Code-level YES / prod NO** | Owner-scoping + cross-user + anonymous tests pass; prod verification is post-activation |
| `GENERAL_CUSTOMER_RELEASED` | **NO** | Downstream of all gates |
| `PRODUCTION_CERTIFIED` | **NO** | Downstream of all gates |

---

## 3. What this PR delivers (DARK, bounded)

- **Permanent authenticated page** `frontend/src/app/(app)/withdrawal-intelligence/page.tsx` + **nav entry** (`AppShell` Analytics group) + **i18n key** (`nav.withdrawalIntelligence`, EN/JA). Composes the already-owner-scoped, DARK-safe sections — `ConnectedEmailAccounts` (connection state), `WithdrawalsPanel` (overview), `WithdrawalsHistory` (history) — plus a persistent **"GuvFX never initiates/confirms/cancels/moves"** statement, the prescribed **empty/onboarding** copy, and an **evidence explanation** (what GuvFX reads / does not read / timestamp meaning / correlation / account-unresolved / broker-reported ≠ receipt). Genuine-only: no demonstration banner, no synthetic rows.
- **Backend anonymous-denial tests** `backend/broker_intelligence/tests_anonymous_denial.py` (metrics, history, mailbox-status, mailbox-revoke → 401/403, flags armed; plus a flags-OFF variant) — closes the one test gap the audit found.
- This plan + rollback packet.

No model/migration change. No flag default change. No production mutation. MT5 Copier (PR #528) untouched.

---

## 4. Production flags (record correction)

> The directive's `BROKER_WITHDRAWAL_INGEST_ENABLED` **does not exist in code** — using it would be a no-op env var.
> The real controls are below; the staged activation (§6) uses the real names.

| Purpose | Real env var (default OFF) |
|---|---|
| Mailbox connect/callback/revoke endpoints | `BROKER_MAILBOX_CONNECT_ENABLED` *(already set in prod)* |
| Mailbox status API (connection state) | `BROKER_MAILBOX_STATUS_ENABLED` |
| Read-only ingestion worker loop | `BROKER_INTELLIGENCE_INGEST_ENABLED` |
| Correlation (request→completion projection) | `BROKER_WITHDRAWAL_CORRELATION_ENABLED` |
| Withdrawals UX (metrics + history API) | `BROKER_WITHDRAWAL_UX_ENABLED` |
| (public directory — NOT required for the customer feature) | `BROKER_PUBLIC_DIRECTORY_ENABLED` |

**Must stay OFF in prod:** `BROKER_WITHDRAWAL_DEMO_INCLUDE_SYNTHETIC`. The customer feature does **not** depend on `BROKER_PUBLIC_CASE_STUDY_ENABLED` or `BROKER_PUBLIC_WTM_PREVIEW_ENABLED` (separate presentation surfaces).

---

## 5. Migration inventory + lock-risk

To apply (prod is at `broker_intelligence` 0010; `broker_public` not installed):

| Migration | Operation | Lock risk (prod rows tiny) |
|---|---|---|
| bi 0011 | `ConnectedMailbox` health columns (add nullable cols) | negligible (1 row) |
| bi 0012 | new `TradingAccountBrokerContact(+Audit)` + uniq + indexes | negligible (new empty tables) |
| bi 0013 | new `BrokerMessageObservation` + uniq | negligible (new empty table) |
| bi 0014 | drop+add `BrokerEvent` unique (owner-scoped); add `provider`/`provider_message_id`; delivery uniq | negligible (brokerevent=2) |
| bi 0015 | FK CASCADE→PROTECT; RunSQL triggers (append-only audit, fail-closed VERIFIED backstop) | negligible (tiny tables) |
| bi 0016 | `Withdrawal.owner` FK; `trading_account` nullable; partial uniq; owner/status idx | negligible (withdrawal=0) |
| broker_public 0001 | `BrokerMetricCategory`,`PublicBroker`,`BrokerMetricAggregate` | negligible (new app) |
| broker_public 0002 | `min_bucket_size` + `MetricParticipationConsent` | negligible |

**Estimate:** sub-second total; brief `ACCESS EXCLUSIVE` locks on tables with 0–9 rows; **no data backfills**, no long index builds. Operationally low-risk. **The gate is not migration duration — it is RECOVERY_CERTIFIED** (§7): the 9 evidence blobs incl. the genuine TradersWay pilot are irreplaceable and must be recoverable before any schema change.

**Hard ordering constraint:** `withdrawal_metrics`/`withdrawal_history` reference `Withdrawal.owner` (0016) **unconditionally** — arming `BROKER_WITHDRAWAL_UX_ENABLED` on a DB below 0016 returns **500**. **Migrate to 0016 before enabling UX.** (Mailbox-status safely degrades below 0011 via `r1_columns_present()`.)

---

## 6. Staged activation (real flag names; after deploy + certified migrations)

1. Deploy code with **all new flags OFF**.
2. Production health checks (app up, existing endpoints 200, no error spike).
3. Apply **only the reviewed + authorized** migrations (bi 0011–0016, broker_public 0001–0002).
4. Verify schema + app compatibility (migrations `[X]`, smoke the member endpoints while still flag-OFF → 404).
5. Enable `BROKER_MAILBOX_STATUS_ENABLED` → verify connection-state loads for a real user.
6. Enable `BROKER_INTELLIGENCE_INGEST_ENABLED` (read-only ingestion) → verify genuine evidence ownership.
7. Verify no cross-user leakage / no duplicate projection (owner-scoped checks).
8. Enable `BROKER_WITHDRAWAL_CORRELATION_ENABLED` → verify request→completion correlation without duplication.
9. Enable `BROKER_WITHDRAWAL_UX_ENABLED` (metrics + history) → authenticated UX live.
10. Support-account smoke test + a second-user isolation test + an anonymous-access test.
11. Monitor errors / rollback thresholds.

---

## 7. Hard blockers — exact action requests (STOP conditions)

1. **RECOVERY_CERTIFIED — Owner: Sponsor + infra.** Need an off-host encrypted production backup + a verified restore into a PostgreSQL 15/Linux recovery environment, with integrity checks and a recorded restore time. **Possible cost:** off-host storage/backup target. **Next command (once a target exists):** run the `ops/recovery/` tooling to take an encrypted `pg_dump|gpg`, restore into an ephemeral PG15 cert cluster, and record the result. *No production migration until this clears.*
2. **OAUTH_PRODUCTION_READY — Owner: human (Google Cloud console).** Publish/verify the production OAuth app (consent screen verified, privacy-policy + terms URLs, production redirect URI, read-only scopes). **I have no console access.** Until done, general external-user activation stays blocked even with code deployed.
3. **MIGRATION AUTHORIZATION + DEPLOY — Owner: Sponsor.** Explicit authorization to deploy current `main` code and apply the certified migrations via the established deployment mechanism.

If any of these would require purchasing infrastructure, Google console access, unavailable production credentials, an irreversible DB operation, a genuine withdrawal, or a legal/privacy decision — that is a STOP; the request above is the exact next action and owner.

---

## 8. Security & privacy review summary

- **Least-privilege / isolation:** read-only Gmail scope fail-closed; every member endpoint owner-scoped; no staff bypass; no IDOR; mailbox anti-hijack. Anonymous denial now tested.
- **Credentials:** Fernet-encrypted off-DB, `credential_ref` only in DB; nothing logged; disconnect destroys the local credential.
- **PII:** no raw email / account number / confirmation link / wallet address in customer-facing payloads; masked identifiers.
- **Escalations:** none blocking at code level.

## 9. Rollback

- First action for any customer-visible defect: **disable the relevant feature flag** (UX → correlation → ingestion → mailbox-status), never delete data.
- Application rollback: redeploy the previous image.
- Migrations: 0011–0016 are additive; reverse only where safe, else forward-fix. **Preserve evidence** (never drop evidence tables on rollback). Revoke tokens if a credential path is implicated.

## 10. Deferred work (explicit)

- **Server-side Google token revocation on disconnect.** Today revoke destroys GuvFX's local credential (so GuvFX can no longer read the mailbox) but does not call Google's `oauth2/revoke`; the grant remains until the user removes it in their Google account. Recommended as a focused, best-effort (fail-open) enhancement with its own test — **not** bundled here.
- **Orphaned `AccountWithdrawalMonitoring` / `BrokerAccountDetailContent`** (frontend) are unreachable from live routes; de-duplicate or remove in a separate cleanup — out of scope for this release.
- Public broker metrics / WAYOND remain **fail-closed** (`INSUFFICIENT_VERIFIED_DATA`); a general customer release does **not** authorize public broker rankings.
- Customer configuration controls, follower-ticket reconciliation, authorized lifecycle execution — not part of this observation-only feature.
