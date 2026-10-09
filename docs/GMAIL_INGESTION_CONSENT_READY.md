# GMAIL_INGESTION_CONSENT_READY — Withdrawal Intelligence V1 (WP3b)

**Status date:** 2026-10-09
**Packet:** Withdrawal Intelligence V1 — Gmail OAuth connection + encrypted credential store + standalone
ingestion worker (DARK).
**Scope of this document:** everything needed to decide whether to authorize the real mailbox
`nrfda1111@googlemail.com`, and exactly how to do it safely. **No real Google consent has been requested.**
The flow is built and proven end-to-end against synthetic fixtures only.

---

## 1. Production status (what is live, what is dark)

**PR #477 merged** (`bf7f3ed`, squash) and **DARK-deployed** to production 2026-10-09 (backend image
`8cfda006dc33`; rollback image `guvfx-prod-guvfx-backend:rollback-preGMAILWORKER` = `aac6984a2b3b`). The MT5
trade-ingest / node2-order / shadow / validate workers were **not** recreated (left on their running
containers) — LIVE execution/recovery untouched.

| Component | State on production |
|---|---|
| Gmail OAuth flow (`gmail_oauth.py`) | Deployed; **inert** — no client credentials provisioned |
| Encrypted credential store (`credential_store.py`, #476) | Deployed; **inert** — `BROKER_INTELLIGENCE_CREDENTIAL_KEY`/`_ROOT` unset |
| `GmailMailSource` / `GmailApiClient` (#476) | Deployed; exercised by unit tests only |
| Connect / callback / revoke endpoints | **Deployed DARK** — routes wired (401 unauth) but **404 when authed** (`BROKER_MAILBOX_CONNECT_ENABLED` unset) |
| Standalone ingestion worker (`run_mailbox_ingest`) | **Deployed DARK** — command refuses unless `BROKER_INTELLIGENCE_INGEST_ENABLED` or `--force` (verified on prod); no scheduler wired |
| Withdrawal correlation (#473) | Deployed DARK (`broker_withdrawal_correlation_enabled` unset) |
| Withdrawal metrics API + member UX (#474/#475) | Deployed DARK (`broker_withdrawal_ux_enabled` unset) → 404 / self-hiding panel |
| Broker-email alias master (`BROKER_EMAIL_IDENTITY_ENABLED`, #464) | Deployed DARK — unset |

**Net:** no mailbox can be connected, no email is polled, no credential key exists, and no member-facing
surface renders. Verified on prod post-deploy: CSRF `200`; mailbox routes return `401` unauthenticated (wired,
auth-protected, not 404/500); all DARK flags unset; ingestion command refuses. Arming requires an explicit,
separate Sponsor action on each flag below.

**Rollback (one step):**
`docker tag guvfx-prod-guvfx-backend:rollback-preGMAILWORKER guvfx-prod-guvfx-backend:latest && cd /home/ubuntu/guvfx-prod && docker compose up -d --force-recreate --no-deps guvfx-backend`

---

## 2. OAuth client type + redirect URI

- **Client type:** **Web application.** The authorization code is exchanged **server-side** with the
  `client_secret` at `oauth2.googleapis.com/token`; this is not an installed/PKCE public client.
- **Authorized redirect URI (exact, must match byte-for-byte in Google Cloud):**
  `https://api.guvfx.com/api/broker-intelligence/mailboxes/callback/`
  (configured at runtime via `GMAIL_OAUTH_REDIRECT_URI`; the callback view and the consent URL both read
  the same value, so they can never drift.)
- **Authorized JavaScript origins:** none required (no browser-side token handling).

---

## 3. Google Cloud Console setup (one-time, by the operator)

1. Create (or reuse) a Google Cloud **project** dedicated to GuvFX ingestion.
2. **APIs & Services → Enable APIs →** enable the **Gmail API**. (No other API is needed; profile is read
   through the Gmail API's own `users/me/profile`.)
3. **OAuth consent screen:**
   - User type **External**, publishing status **In production** *or* keep **Testing** and add
     `nrfda1111@googlemail.com` as a **Test user** (fastest path for the pilot — a Testing-mode app can be
     authorized by listed test users without Google verification).
   - Add **only** the scope `https://www.googleapis.com/auth/gmail.readonly` (a restricted scope). If the app
     is set to production rather than Testing, this scope triggers Google's **restricted-scope verification**
     (security assessment) — for a single pilot mailbox, **Testing mode + test user is the recommended route**
     and avoids that review.
4. **Credentials → Create credentials → OAuth client ID → Web application.**
   - Add the redirect URI from §2 exactly.
   - Record the generated **client ID** and **client secret** for secure provisioning (§5) — do **not** paste
     them into chat, the repo, Notion, or any ticket.

---

## 4. Scopes requested (minimal, read-only, fail-closed)

- **Requested:** `https://www.googleapis.com/auth/gmail.readonly` — **only**.
- The consent URL sets `access_type=offline` + `prompt=consent` (to obtain a refresh token for the unattended
  worker) and **deliberately omits `include_granted_scopes`**, so a prior broader grant to the same client can
  never widen the returned scope.
- **Fail-closed enforcement:** `_assert_readonly_scope()` rejects any token whose granted scope contains a
  write/modify/full capability (`mail.google.com`, `gmail.modify`, `gmail.send`, `gmail.compose`,
  `gmail.insert`, `gmail.settings.*`). A write-capable token is **never stored or used** — the connection
  fails instead. A connected mailbox can therefore only ever be **read**.

---

## 5. Secure credential provisioning (no secret in DB / repo / logs / config)

Provision these as environment variables on the backend service only (production uses
`/home/ubuntu/guvfx-prod/beta.env`); never commit them, never log them, never place them in Notion:

| Variable | Purpose |
|---|---|
| `GMAIL_OAUTH_CLIENT_ID` | Web OAuth client ID (§3) |
| `GMAIL_OAUTH_CLIENT_SECRET` | Web OAuth client secret (§3) |
| `GMAIL_OAUTH_REDIRECT_URI` | `https://api.guvfx.com/api/broker-intelligence/mailboxes/callback/` |
| `BROKER_INTELLIGENCE_CREDENTIAL_KEY` | Fernet key (`cryptography`) encrypting stored OAuth tokens at rest |
| `BROKER_INTELLIGENCE_CREDENTIAL_ROOT` | Directory (created `0700`) holding the encrypted token files |
| `BROKER_INTELLIGENCE_MAX_RAW_BYTES` *(optional)* | Per-message raw capture ceiling in bytes (default 25 MiB). A larger message is **never silently dropped** — the fetch fails loud and the cursor does not advance; raise this to admit a legitimately large email. |

- The DB stores only an **opaque `credential_ref`** (`"cr" + token_hex(16)`), never the token.
- Tokens are written with `tempfile.mkstemp` (atomic, `0600`) then `os.replace`; the root is `0700`.
- `BROKER_INTELLIGENCE_CREDENTIAL_KEY` is **fail-closed**: storing a token without a key raises; loading an
  existing token file without the key raises (a missing ref returns `None` without needing the key).
- The OAuth `client_id`/`client_secret` are read **settings-then-env** at call time — never from the DB/repo.
- On an exchange/profile failure the callback returns a generic error; the code and token are **never** logged
  or echoed.

---

## 6. How to authorize `nrfda1111@googlemail.com` (operator procedure)

Arming is **two independent gates** — connect first, verify the connection, poll only later:

1. Provision §5 variables on the backend.
2. Arm the connect flow only: set `BROKER_MAILBOX_CONNECT_ENABLED=1` (recreate the backend). Leave
   `BROKER_INTELLIGENCE_INGEST_ENABLED` **unset** — no polling yet.
3. Signed in as the member who owns the TradersWay account, `GET /api/broker-intelligence/mailboxes/connect/`
   → returns the Google consent URL (CSRF `state` signed + bound to that user, 600 s TTL).
4. The **human operator** (Sponsor) opens that URL in a browser, signs in as `nrfda1111@googlemail.com`,
   grants the read-only consent. Google redirects to the callback with `code` + `state`.
5. The callback verifies the state (signature + age + issued-to-this-user), exchanges the code read-only,
   stores the encrypted token, and records a `CONNECTED` `ConnectedMailbox`. **No email is read yet.**
6. Verify the connection is read-only and correctly attributed (mailbox row `CONNECTED`, scope exactly
   `gmail.readonly`, credential present in the store not the DB) **before** any polling.
7. Only when the Sponsor decides to begin polling: set `BROKER_INTELLIGENCE_INGEST_ENABLED=1` and run
   `manage.py run_mailbox_ingest` (or schedule it). The cursor advances only after a batch is durably
   ingested.

> Claude never performs steps 4 or the real consent. The real OAuth consent is a human action by the Sponsor.
> Claude will not request Gmail credentials through chat.

---

## 7. Revocation (member- and operator-controlled)

- **In-app:** `POST /api/broker-intelligence/mailboxes/<id>/revoke/` (owner-scoped) destroys the stored
  credential and marks the mailbox `REVOKED`, independently of any other mailbox the member owns.
- **At Google:** the account owner can revoke GuvFX's access at myaccount.google.com → Security → Third-party
  access at any time; the next token refresh then fails closed and the worker stops reading that mailbox.
- **Operationally:** unset `BROKER_INTELLIGENCE_INGEST_ENABLED` to stop all polling; unset
  `BROKER_MAILBOX_CONNECT_ENABLED` to withdraw the connect surface (→ 404).

---

## 8. End-to-end certification evidence (synthetic)

- **Command:** `cd backend && .venv/bin/python manage.py test broker_intelligence`
- **Result:** `Ran 136 tests ... OK` (includes 8 new `tests_mailbox` + the pre-existing WP1–WP6 suites).
- **Migration check:** `makemigrations --check --dry-run broker_intelligence` → `No changes detected`
  (additive code only; no schema change).
- **Synthetic flow proven (no network, no consent):**
  - `FakeGmail → GmailMailSource → EvidenceBlob + BrokerEvent`, attributed to the mailbox owner, cursor
    committed only after durable ingestion.
  - **Cross-user firewall:** a message spoofing another member's alias, ingested for owner *U*, is **not**
    attributed to the other member (`trading_account` is `None`).
  - **Correlation leg:** a labelled-SYNTHETIC `EXTERNAL_WITHDRAWAL` event → `run_correlation` → one
    `Withdrawal` → `withdrawal_metrics` reports `completed_count=1`, `{"USD": "200.00"}`.
  - **Connect/callback/revoke:** consent URL carries the read-only scope; callback stores the token in the
    encrypted store (not the DB) and rejects a foreign/forged CSRF state; revoke is owner-scoped and destroys
    the credential; all three 404 while the flag is OFF.
- **Defect caught + fixed during this packet:** the ingestion worker did not register the deterministic
  parsers (the registry is empty at import by design); an armed worker would have quarantined every message
  as unparseable and produced **zero** events. The worker now calls `register_default_parsers()` at start-up
  (idempotent).
- **Adversarial review (pre-merge):** a 4-lens review (security / correctness+cursor+txn / isolation+DARK+evidence /
  test-quality) with 3 independent refute-by-default verifiers per finding surfaced 6 confirmed findings
  (deduped to 4 real defects): worker per-mailbox isolation (a `commit_cursor()`/construction error aborting
  the whole pass), a callback cursor fast-forward that would skip un-ingested mail on re-connect, a cross-user
  mailbox clash returning 500 + an orphaned credential file, and a DARK revoke test passing for the wrong
  reason. **All four were fixed** (commit `c2676af`) with added coverage; 3 further coverage nitpicks were
  adversarially refuted (the behaviour was already correct). No HIGH finding; the cross-user attribution
  firewall, CSRF state verification, read-only scope, and DARK gating all held.
- **Post-deploy safety sweep (EMAIL_CAPTURE_READY):** a 3-agent adversarial sweep proved **no email-derived
  financial action is possible** and **withdrawal metrics cannot be polluted by UNKNOWN/INTERNAL/DEPOSIT**
  (both CONFIRMED structurally), and found **one real durable-preservation defect**: the DARK Gmail fetch layer
  silently dropped an un-capturable message (empty `raw`, or raw > the 5 MB cap) **and advanced the cursor past
  it** → permanent silent loss. **Fixed:** `GmailApiClient._fetch_message` now **raises `MessageCaptureError`**
  instead of dropping (the cursor is not advanced; the message resurfaces as a visible, retryable failure), the
  per-message ceiling is **configurable** (`BROKER_INTELLIGENCE_MAX_RAW_BYTES`, default raised to 25 MiB), and
  initial-sync is documented as deliberately **forward-only** (capture begins at connect; no historical
  backfill — a privacy + scope choice). Tests updated to assert fail-closed (oversize + empty-raw both raise).

---

## 9. Remaining `TRADERSWAY_WITHDRAWAL_PILOT_READY` conditions (NOT yet met)

This packet does **not** declare `TRADERSWAY_WITHDRAWAL_PILOT_READY`. Mandatory remaining conditions:

1. **A genuine external-withdrawal sample.** The deterministic TradersWay parser **structurally has no
   prose→`EXTERNAL_WITHDRAWAL` path** (three review rounds showed any prose heuristic is bypassable, and
   over-claiming external would inflate investor-facing metrics). Until the positive classifier is built and
   certified against a **real** external-withdrawal email, a parsed TradersWay email classifies as
   `UNKNOWN`/`INTERNAL_TRANSFER` and **does not** produce a `Withdrawal`. This is the single biggest gap.
2. Google Cloud OAuth client provisioned (§3) and §5 credentials installed on the backend.
3. Real read-only consent granted for `nrfda1111@googlemail.com` by the Sponsor (§6).
4. Connection verified read-only + correctly owner-attributed before any polling.
5. Correlation + metrics + UX flags armed deliberately and verified against the connected mailbox.
6. A real (not synthetic) end-to-end observation of at least one genuine withdrawal lifecycle, reconciled.
7. The real withdrawal itself is **initiated by the Sponsor** — GuvFX never places, sizes, or approves a
   withdrawal; the system only observes and reports.

---

## 10. Forecast / recommended next action

**One bounded next step:** obtain a **genuine TradersWay external-withdrawal email sample** (sanitised) so the
positive `EXTERNAL_WITHDRAWAL` classifier can be built and certified (condition §9.1). Everything else
(connection, encryption, worker, correlation, metrics, UX) is built and DARK-proven; the pilot is blocked on
that one real sample plus the human consent + provisioning steps, none of which Claude performs.
