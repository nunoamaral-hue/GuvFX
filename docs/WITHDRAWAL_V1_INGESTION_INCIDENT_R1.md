# GUVFX_WITHDRAWAL_V1_INGESTION_INCIDENT_R1_REPORT

**Packet:** GUVFX_WITHDRAWAL_V1_INGESTION_INCIDENT_R1
**Author:** Principal Engineer (Claude)
**Date:** 2026-10-16
**Scope:** GREEN diagnostic/health subsystem, DARK (no deploy, no re-auth, no cursor reset, no exit-code change).
**Branch / PR:** `fix/broker-ingest-diagnostics` (see §10 of this report for CI/PR status).

All production inspection here was READ-ONLY (SSH + `docker exec … manage.py shell` SELECT-only + `journalctl`). No
production mutation, no Gmail call, no credential decryption, no flag/activation change, no Docker recreation.

---

## 1. Current production incident status

The broker-email ingestion is **still wedged** and has been continuously since **2026-10-14 20:12:54 UTC**. As of the
2026-10-16 inspection (~07:50 UTC, ~35.6 h), every 5-minute pass fails identically.

| § | Check (read-only) | Observed 2026-10-16 |
|---|---|---|
| A | Timer active/enabled | `guvfx-broker-ingest.timer` **active + enabled**; next trigger 07:53:32 UTC |
| B | Last ~10 executions | Every pass `Starting → mailbox ingest failed for mailbox 1: RuntimeError → failed=1 → Finished` (unit still exits 0) |
| C | Per-run counts | `events=0 ingested=0 failed=1` every pass |
| D | Last successful sync | `2026-10-14 20:12:54 UTC` (frozen; ~35.6 h stale) |
| E | Cursor | `cursor_state = 3204241` — **unchanged** (fail-closed; not advanced on failure) |
| F | ConnectedMailbox | id=1, `status=CONNECTED` |
| G | Credential reference | present (`credential_ref` set) |
| H | OAuth client config | `GMAIL_OAUTH_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` all **present** (so NOT the `oauth_not_configured` path) |
| I | Last BrokerEvent | id=1 `WITHDRAWAL_CONFIRMATION_REQUIRED` (received 2026-10-12 06:36:12); **no new events** |
| J | EvidenceBlob count | 6 |
| K | Withdrawal count | 0 |
| L | Last failure classification | **`RuntimeError` (class only)** — the HTTP status was discarded; the precise cause is unconfirmed (this is the gap R1 fixes) |

## 2. Last successful ingestion timestamp

**2026-10-14 20:12:54 UTC** (`ConnectedMailbox.last_successful_sync`, matching `updated_at`). No pass has fully
succeeded since; the cursor has not advanced.

## 3. Actual observed error classification (if determinable)

**Unconfirmed — and deliberately not guessed.** The production worker predates this fix and logs only
`type(exc).__name__` = `RuntimeError`, discarding the HTTP status. From the code paths, a bare `RuntimeError` on the
incremental path (a present cursor always takes `history.list` first) is raised at exactly these sites:

- `gmail_source._get_json` → `gmail api http {status}` on `history.list` (a non-200: **401** token/consent revoked, or
  **404** = history cursor too old/invalid, or **403**); **or**
- `gmail_source._access_token` → `no stored credential` (CREDENTIAL_MISSING) — **unlikely**: the credential reference is
  set and a decrypt failure would raise `CredentialStoreError` (logged as that, not `RuntimeError`), and the file was
  not removed.

A token **refresh** failure is ruled out (it raises `GmailOAuthError`, which would log as `GmailOAuthError`, not
`RuntimeError`); a network/timeout failure is ruled out (it would log `URLError`/`timeout`). So the field narrows to a
**persistent non-200 from `history.list`** — most plausibly **401 (consent/token revoked)** given the abrupt onset and
unbroken persistence, with **404 (history cursor invalid)** the key alternative that must NOT be auto-recovered.

Per RULE 11, this is stated as unconfirmed: confirming the exact status requires either the R1 fix deployed (the next
pass then self-labels the code + status) **or** a live diagnostic probe / token decryption — neither of which is
authorized under this packet.

## 4. What remains uncertain without a live diagnostic probe

- The exact HTTP status (401 vs 403 vs 404-cursor-invalid).
- Whether the Gmail history is still replayable from cursor `3204241` (an unchanged cursor is **not** proof the history
  has not aged out — §6). If the cause is `GMAIL_HISTORY_CURSOR_INVALID`, recovery requires a separate authorized
  review, never an automatic re-anchor to the current profile `historyId`.

## 5. GREEN diagnostic implementation (this PR)

A single coherent Withdrawal-V1 subsystem, DARK, no behaviour change to the live cursor/exit contract:

- **`broker_intelligence/ingest_diagnostics.py`** (new, pure, secret-free): the stable code vocabulary + `IngestDiagnostic`
  + `classify_exception()`. Classifies by exception **type + structured fields**, never by parsing a message string.
  `safe_dict()` emits only `{code, http_status, operation, retryable, blocking, recovery}` — never a message, URL,
  message id, address, token, or raw Google body. A bare `RuntimeError` is `UNKNOWN_FAILURE` (never inferred to HTTP).
- **`broker_intelligence/gmail_source.py`**: replaced the generic `RuntimeError("gmail api http …")` /
  `"no stored credential"` with typed exceptions carrying structured, secret-free fields —
  `GmailApiError(status, operation)`, `CredentialMissingError`, `GmailConnectionError`, `GmailTimeout`,
  `MessageTooLargeError(MessageCaptureError)`. `_get_json` now threads an `operation` label
  (`history.list` / `messages.get` / `messages.metadata` / `messages.list` / `profile`) so a 404 on `history.list`
  (cursor invalid) is distinguishable from a 404 on `messages.get`. `_default_http_get` now maps a network
  failure/timeout to `GmailConnectionError` / `GmailTimeout` instead of an unlabelled raise.
- **`broker_intelligence/gmail_oauth.py`**: `GmailOAuthError` now carries the token-endpoint HTTP `status` so a 400/401/403
  (permission denied → re-auth) is distinguishable from a 5xx/malformed transient refresh failure.
- **`broker_intelligence/mailbox_ingest.py`**: on failure the worker classifies and logs ONLY `code + http_status +
  operation + mailbox id` (secret-free), records the result onto the mailbox (health bookkeeping, fail-open), and the
  run summary carries `failure_codes` (so the journal names WHY, e.g. `{'GMAIL_HTTP_401': 1}`). **The cursor-advance
  contract is unchanged** (§6): the cursor still advances only after a fully successful batch.

## 6. Health / staleness reporting (this PR)

- **`broker_intelligence/health.py`** (new, read-only, secret-free): `mailbox_health()` / `service_health()` classify each
  mailbox into **SERVICE_RUNNING / MAILBOX_SYNC_SUCCESSFUL / MAILBOX_SYNC_FAILED / MAILBOX_STALE / RECOVERY_REQUIRED**
  from `status`, `last_successful_sync`, staleness thresholds, the last diagnostic code, and `consecutive_failures`.
  It **degrades gracefully without the R1 columns** (unmigrated prod): staleness alone already flags the current
  mailbox 1 as `RECOVERY_REQUIRED` (~35 h) — i.e. a mailbox failing for hours is identifiable **without decrypting any
  credential**, the exact gap the outage exposed.
- **`broker_mailbox_health` management command** (new, read-only): prints the secret-free report (masked addresses,
  stable codes). No Gmail call, no credential read, no write.
- **Minimal schema (DARK, separately gated)**: migration `0011` adds 6 nullable/defaulted columns to `ConnectedMailbox`
  (`last_attempted_sync`, `last_pass_cursor_advanced`, `last_error_code`, `last_error_http_status`, `last_error_at`,
  `consecutive_failures`). Pure additive column-add, reversible, no backfill. **Written only** by the worker's own
  per-pass bookkeeping on its own row (never evidence, never financial, never a secret). Application to production is
  separately gated; the read-model works before it is applied.

**Invariant preserved (§5):** a successful systemd unit exit does **not** imply mailbox health; `service_health` says
so explicitly, and the per-mailbox state is authoritative.

## 7. Cursor / evidence safety verification

Preserved exactly (tested, §7 scenarios): the cursor advances only after a successful durable batch; on **any**
failure it is **not** advanced; on `GMAIL_HISTORY_CURSOR_INVALID` it is **not auto-anchored** (recorded as
`RECOVERY_REQUIRED` + `ESCALATE_CURSOR`, escalation deferred); capture failures still raise (no silent drop); evidence
remains content-addressed/immutable and de-dups on safe re-delivery (no duplicate `EvidenceBlob`/`BrokerEvent`);
health bookkeeping is fail-open and never touches `cursor_state` / `last_successful_sync`.

## 8. Production incident decision matrix (operator response per confirmed condition)

| Diagnostic code | Likely cause | Operator response (authorized boundary) |
|---|---|---|
| `GMAIL_HTTP_401`, `OAUTH_PERMISSION_DENIED` | token/consent expired or revoked | Determine expiry vs revocation; **if re-auth is genuinely needed, request Sponsor approval** (read-only scope only). No auto-reauth. |
| `GMAIL_HTTP_403` | access policy / scope removed | Sponsor review; re-auth only if policy restored. |
| `GMAIL_HISTORY_CURSOR_INVALID` (history.list 404) | stored `startHistoryId` too old/invalid | **Do NOT auto-anchor.** Assess whether history is recoverable without losing broker evidence; escalate any reset/recovery decision (separate authorization). |
| `GMAIL_HTTP_404` (messages.get) | message deleted between list and get | Preserve cursor; review; a persistently-missing single message may need a bounded operator decision. |
| `GMAIL_HTTP_429` | rate limit | Preserve cursor; the existing 5-min cadence **is** the bounded retry — do not add an uncontrolled loop. |
| `GMAIL_HTTP_5XX`, `GMAIL_CONNECTION_ERROR`, `GMAIL_TIMEOUT` | transient Google/network | Preserve cursor + evidence; bounded, non-destructive retry (cadence). |
| `CREDENTIAL_MISSING` | credential file absent | Do **not** silently recreate a grant; operator review (re-connect is Sponsor/ops). |
| `CREDENTIAL_STORE_ERROR` | key missing / ciphertext corrupt | Operator review; never expose the credential; check key configuration. |
| `OAUTH_CLIENT_NOT_CONFIGURED` | app client id/secret unset | Operator config fix (backend-only `broker_intel.env`). |
| `MESSAGE_CAPTURE_FAILED` | empty raw / history backlog > page ceiling | Preserve cursor; for the page ceiling, raise `BROKER_INTELLIGENCE_MAX_HISTORY_PAGES` for a bounded supervised catch-up. |
| `MESSAGE_TOO_LARGE` | raw exceeds ceiling | Deliberately raise `BROKER_INTELLIGENCE_MAX_RAW_BYTES`. |
| `DATABASE_COMMIT_FAILED` | DB / evidence-store write failed | Cursor not advanced (unchanged); idempotent retry preserves guarantees. |
| `UNKNOWN_FAILURE` | unrecognized | Operator review; never claim healthy; never auto-recover. |

**Most-likely current condition:** `GMAIL_HTTP_401` (consent/token revoked → `SPONSOR_REAUTH`), with
`GMAIL_HISTORY_CURSOR_INVALID` the alternative that changes the response (escalate, do not re-anchor). Confirm by
deploying R1 (the next pass self-labels) **before** any operator action.

## 9. Test results

- New suite `broker_intelligence.tests_ingest_diagnostics`: **35/35 PASS** (maps all §7 scenarios 1–25).
- Full `broker_intelligence` suite: **383/383 PASS** (348 prior + 35 new; no regressions). The existing
  `tests_mailbox` fake raising a bare `RuntimeError` now classifies `UNKNOWN_FAILURE` — the §4 guard, confirmed.
- `makemigrations --check`: no pending broker_intelligence changes (migration 0011 complete). The migration
  reverse-graph test `execution.tests_tb1_migration`: **PASS** (0011 reverses cleanly).
- Full backend suite + repository CI: see §10.

## 10. GitHub PR and CI status / deployment requirements

- Branch `fix/broker-ingest-diagnostics` off `main` `26c8eeb`; §2 collision gate clean (no concurrent Withdrawal V1
  PRs). PR + CI status appended on push.
- **Deployment: NOT performed and NOT authorized under this packet.** Activating the subsystem requires (separately
  gated): apply migration `0011`, rebuild/recreate the backend image, and the ingestion worker picks up the new
  logging automatically. No worker-config change, no re-auth, no cursor reset is performed here.

## 11. AMBER proposal — persistent mailbox hard failure → non-zero exit and/or alert (DEFERRED, not implemented)

Prepared for a separate Programme Director/Sponsor decision; **not implemented and live exit semantics unchanged.**

- **Current semantics:** the oneshot unit exits 0 even when the only mailbox hard-fails, so "timer green" ≠ "ingestion
  healthy". `consecutive_failures` + `last_error_code` + staleness now make the condition *detectable*; escalation is
  the open decision.
- **Design tension — multi-mailbox isolation:** per-mailbox isolation must be preserved, so the unit must NOT fail just
  because one of several mailboxes transiently fails. A safe escalation policy keys on **aggregate** state, e.g. exit
  non-zero (or emit an alert) only when a mailbox is `RECOVERY_REQUIRED` (a blocking code, or staleness >
  `recovery_seconds`), never on a single transient `429/5xx`.
- **False-positive guards:** threshold on `consecutive_failures` (e.g. ≥ 3) and/or staleness, so a single transient pass
  never pages; retry cadence stays the existing 5 minutes.
- **Monitoring destination + privacy:** any alert carries only masked addresses + stable codes (never a token/address/
  subject). Destination (log sink, healthcheck endpoint, or pager) is an operator decision.
- **Recommended shape:** a SEPARATE health-check timer (or an opt-in `--exit-nonzero-on-recovery-required` flag on a
  dedicated check), leaving the ingestion unit's exit semantics untouched. Requires Programme Director/Sponsor review.

## 12. Exact Sponsor decision required

1. **Approve deploying R1** (apply migration 0011 + recreate backend) so the next ingestion pass self-labels the exact
   failure code/status — turning the unconfirmed cause (§3) into a one-glance fact. (No re-auth / cursor change.)
2. **Then, based on the revealed code:** if `GMAIL_HTTP_401`/`OAUTH_PERMISSION_DENIED` → authorize a read-only Gmail
   **re-auth** (Sponsor/ops); if `GMAIL_HISTORY_CURSOR_INVALID` → authorize a separate cursor-recovery review (no
   auto-anchor).
3. **Decide the Amber escalation** (§11) separately.

## 13. TradersWay pilot readiness

Unchanged and **BLOCKED**: no certified positive external-withdrawal template, no genuine Withdrawal record, 0
`Withdrawal` rows; correlation/metrics/UX remain synthetic-certified + DARK. A genuine pilot still requires, in order:
ingestion health restored (re-auth), identity verified (already VERIFIED for acct 46/47), the broker's capture path
confirmed operational, and any history-cursor gap safely reconciled — then the Sponsor may independently perform one
genuine external withdrawal. GuvFX remains an observer; it never initiates/confirms/cancels a withdrawal.

---

## Certification markers

| Marker | Verdict | Evidence |
|---|---|---|
| `WITHDRAWAL_INGESTION_DIAGNOSTICS_CERTIFIED` | **PASS (in tests)** | `ingest_diagnostics` + 35 tests; all §4 classes mapped; history.list-404 ≠ messages.get-404; bare RuntimeError → UNKNOWN |
| `WITHDRAWAL_MAILBOX_HEALTH_CERTIFIED` | **PASS (in tests)** | `health.py` + command; 5 states; staleness flags the real ~35 h stale without credentials |
| `WITHDRAWAL_CURSOR_SAFETY_CERTIFIED` | **PASS (in tests)** | cursor unchanged on failure; cursor-invalid not auto-anchored; evidence de-dup; §6 intact |
| `WITHDRAWAL_SECRET_FREE_LOGGING_CERTIFIED` | **PASS (in tests)** | `safe_dict` stable-fields-only; log test asserts no address/token/URL/host/subject/message-id |
| `WITHDRAWAL_INGESTION_PRODUCTION_HEALTHY` | **BLOCKED** | prod failing every pass since 2026-10-14 20:12 UTC; fix not deployed; restoration is Sponsor/ops re-auth |
| `TRADERSWAY_WITHDRAWAL_PILOT_READY` | **BLOCKED** | gated on ingestion health + genuine withdrawal; unchanged |

*Production is NOT labelled healthy merely because the timer is active — it is failing every pass; see §1.*
