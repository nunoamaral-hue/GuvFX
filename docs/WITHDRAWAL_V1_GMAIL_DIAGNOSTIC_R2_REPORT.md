# GUVFX_WITHDRAWAL_V1_GMAIL_DIAGNOSTIC_R2_REPORT

**Packet:** GUVFX_WITHDRAWAL_V1_GMAIL_DIAGNOSTIC_R2 (Sponsor-approved controlled read-only diagnostic + authorized DARK fix)
**Author:** Principal Engineer (Claude)
**Executed:** 2026-10-16 ~09:15 UTC, against production ConnectedMailbox 1
**Gmail request budget:** 12 additional GETs authorized; **3 used**

The R2 probe ran as an ephemeral one-shot in the prod backend container, reproducing the worker's pre-raw acquisition
path (`history.list` pagination → `messages.get?format=metadata` sender check) using only the read-only credential
**load** + direct TLS `urllib` GETs. It did **not** use `_access_token`/`refresh_access_token`/`store_token`/
`commit_cursor`/`run_mailbox_ingest`, never called `format=raw`, wrote nothing, and stopped at the first non-200.

---

## Result (secret-free machine output)

```
stage_a history.list : http_status=200, pages_examined=1, history_drained=true, ids_collected=33
stage_b messages.metadata : request #2 -> http_status=404  (GMAIL_HTTP_404)
failure_classification = GMAIL_MESSAGE_METADATA_HTTP_ERROR   diagnostic_complete = true
request_count = 3  (budget_remaining 9)   refresh_invoked = false
cursor_write = false   credential_write = false
after: cursor_unchanged=true, last_successful_sync_unchanged=true, status_unchanged=true,
       evidenceblob_delta=0, brokerevent_delta=0
```

## Required report (§15)

1. **Production preflight verdict:** PASS (mailbox 1; owner the expected GuvFX user; provider Gmail; status CONNECTED;
   credential loaded read-only; scope check passed).
2. **Access-token expiry + scope validation:** expiry **VALID**; scope `gmail.readonly` only; no write/modify/send/full scope.
3. **Exact number of requests used:** **3** (1 `history.list` + 2 `messages.get?format=metadata`). Budget remaining 9.
4. **History pages examined:** **1** (drained; `nextPageToken` absent), yielding **33** message-added ids.
5. **Metadata requests examined:** **2** (ids #1 and #2 of the 33).
6. **First observed failure:** the **2nd** `messages.get?format=metadata` request → **HTTP 404**.
7. **Operation and HTTP status:** `operation = messages.metadata`, `http_status = 404`
   (`GMAIL_MESSAGE_METADATA_HTTP_ERROR` / R1 `GMAIL_HTTP_404`).
8. **What is definitively established:** `history.list` from cursor `3204241` succeeds (200, single page, 33 added ids)
   — so the cursor is **not** invalid and auth is **not** the problem. The worker's failure is a **per-message 404**: a
   message that `history.list` reported has since been **deleted** from the mailbox, so `messages.get` returns 404. The
   old worker raised `RuntimeError` on any non-200 → the whole batch failed → the cursor never advanced → every 5-minute
   pass re-walked the same frozen history (`3204241`) → the same 33 ids → the same deleted message → the same 404. That
   is a **deterministic poison-message deadlock** and exactly explains "fails every pass for 35h." It is **not** token
   expiry, **not** 401/403, **not** an invalid history cursor (404-on-history), and **not** a rate limit (3 requests
   succeeded).
9. **What remains uncertain:** how many of the 33 backlog ids are deleted vs. allowlisted-broker vs. personal (only ids
   #1 — a non-broker sender — and #2 — gone — were probed within the stop-at-first-failure rule). Whether the backlog
   contains any genuine broker withdrawal email is not yet known; it will be captured once the fix lets the batch
   complete. The `format=raw` stage was not exercised (not authorized), but the fix tolerates a 404 there too.
10. **Confirmation of zero probe writes:** `evidenceblob_delta = 0`, `brokerevent_delta = 0`, `cursor_write = false`,
    `credential_write = false`, `refresh_invoked = false`.
11. **Cursor preservation evidence:** `cursor_unchanged = true` (`3204241`), `last_successful_sync_unchanged = true`
    (2026-10-14 20:12:54 UTC), `status_unchanged = true`.
12. **Existing timer status:** `guvfx-broker-ingest.timer` active + enabled, `ActiveEnterTimestamp = 2026-10-13 10:06:13
    UTC` (unchanged; not modified by the probe; the timer continued its own scheduled passes).
13. **PR #500 repository status:** **MERGED** (governed squash `bb053ab`) under the §13 authorization — R1 secret-free
    diagnostics + mailbox health are now on `main` (DARK; migration 0011 NOT applied; not deployed).
14. **Smallest safe corrective action:** tolerate a per-message **404** (message gone): skip that id, count it
    (`messages_gone`), continue the batch; **every other non-200 (401/403/429/5xx) and every `MessageCaptureError`
    still fails closed** (cursor held). This unjams the cursor WITHOUT losing any recoverable mail (a 404 message no
    longer exists, so nothing capturable is skipped — the §12 "do not skip messages" rule is honored).
15. **Engineering work completed (DARK, PR #501, branch `fix/broker-ingest-404-tolerance` off `main bb053ab`):**
    `GmailApiClient._acquire_if_broker` (the 404 tolerance) routing both `list_new_messages` loops; a `messages_gone`
    counter surfaced to the worker result + run summary (secret-free, INFO-logged). 6 regression tests incl. the
    incident reproduction (history 200 + a metadata 404 → skip + continue + cursor advances), a raw-stage 404, the
    fail-closed cases (401/403/429/5xx + `MessageCaptureError`), and initial-sync tolerance. `broker_intelligence`
    **390/390**; full backend suite + adversarial review (5 lenses × 3 skeptics) — results appended on completion.
16. **Remaining production deployment dependencies (separately gated — NOT under this packet):** deploy requires
    applying **migration 0011** (from R1, now on main) + recreating the backend container so the worker picks up both
    the diagnostics and the 404 tolerance. On the first post-deploy pass the worker will walk the 33-id backlog, skip
    the deleted message(s), capture any allowlisted broker emails, and **advance the cursor** — ingestion resumes. No
    re-auth and no cursor reset are needed.
17. **Current TradersWay pilot readiness:** **BLOCKED** — gated on ingestion health restored (deploy the fix) **then** a
    genuine external withdrawal. 0 `Withdrawal` rows; positive classifier / correlation / UX remain DARK.

## Certification markers

| Marker | Verdict | Evidence |
|---|---|---|
| `GMAIL_DIAGNOSTIC_R2_COMPLETED` | **PASS** | Probe executed within budget (3/12 GETs), zero writes, secret-free |
| `GMAIL_FAILURE_OPERATION_IDENTIFIED` | **PASS** | `messages.metadata` HTTP 404 (deleted message); deterministic poison-message deadlock |
| `GMAIL_CURSOR_PRESERVED` | **PASS** | `cursor_unchanged = true` before/after the probe |
| `GMAIL_CREDENTIAL_UNCHANGED` | **PASS** | `refresh_invoked = false`; read-only load only; no credential write |
| `WITHDRAWAL_INGESTION_PRODUCTION_HEALTHY` | **BLOCKED** | Fix is DARK/not deployed; prod still failing every pass. (Not claimed healthy — no successful end-to-end pass observed.) |
| `TRADERSWAY_WITHDRAWAL_PILOT_READY` | **BLOCKED** | Gated on deployed ingestion health + a genuine external withdrawal |

*No probe result is represented as proof that ingestion is healthy or that the backlog has been recovered.*
