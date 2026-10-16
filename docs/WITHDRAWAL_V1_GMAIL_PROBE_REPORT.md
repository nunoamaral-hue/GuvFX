# GUVFX_WITHDRAWAL_V1_GMAIL_PROBE_REPORT

**Packet:** GUVFX_WITHDRAWAL_V1_GMAIL_DIAGNOSTIC_PROBE (Sponsor-approved controlled read-only diagnostic)
**Author:** Principal Engineer (Claude)
**Executed:** 2026-10-16 ~08:53:36 UTC, against production ConnectedMailbox 1
**Request budget:** 2 Gmail GETs (used exactly 2)

The probe ran as an ephemeral one-shot inside the existing prod backend container, using only the approved read-only
credential **load** (`credential_store.load_token`) + a direct TLS-verified `urllib` GET with the already-stored
access token. It deliberately did **not** use `GmailApiClient._access_token()` (which can refresh), `refresh_access_token`,
`store_token`, `commit_cursor`, or `run_mailbox_ingest`. No production code was modified; nothing was written to disk.

---

## Result (secret-free machine output)

```
classification        = GMAIL_HISTORY_CURSOR_ACCEPTED   (outcome E)
probe_a  profile       http_status = 200  transport = OK
probe_b  history.list   http_status = 200  transport = OK
request_count = 2  (within budget)   refresh_invoked = false
credential_write = false   cursor_write = false
after: cursor_unchanged = true, last_successful_sync_unchanged = true,
       status_unchanged = true, credential_ref_present_unchanged = true
```

## Required report (§13)

1. **Production preflight verdict:** PASS. Mailbox 1 found; owner is the expected GuvFX user; provider Gmail; status
   CONNECTED; credential reference present; credential loaded read-only into memory; scope check passed.
2. **Mailbox connection status:** CONNECTED.
3. **Access-token expiry state:** **VALID** (not expired at probe time; the live, separately-authorized timer keeps the
   stored token fresh as a side effect of each pass calling `_access_token`, so refresh is evidently working).
4. **Gmail read-only scope validation:** PASS — `gmail.readonly` present; **no** write/modify/send/compose/insert/
   settings/full-mailbox scope.
5. **Probe A (`GET /users/me/profile`) HTTP status:** **200** — the existing access token is accepted by Gmail.
6. **Probe B (`GET /users/me/history`, `startHistoryId` = existing cursor, `historyTypes=messageAdded`, `maxResults=1`)
   HTTP status:** **200** — Gmail accepts the existing history cursor; it has **not** aged out / is **not** invalid.
7. **Controlled failure classification:** **E — `GMAIL_HISTORY_CURSOR_ACCEPTED`.**
8. **Proof request count ≤ 2:** `request_count = 2`, `request_count_within_budget = true`. Exactly Probe A then Probe B;
   no third request, no pagination, no retry.
9. **Proof no token refresh occurred:** `refresh_invoked = false`. The probe called only `load_token` (read-only decrypt
   into transient memory) and issued GETs with that token; it never invoked `_access_token`, `refresh_access_token`, or
   `store_token`. Token expiry was checked **before** any network use (would have STOPPED if expired/unknown).
10. **Proof no mailbox cursor was modified by the probe:** `cursor_write = false`; `after.cursor_unchanged = true`. The
    probe performed zero DB writes.
11. **Production ingestion state before/after (read-only):** cursor `3204241` unchanged; `last_successful_sync`
    2026-10-14 20:12:54 UTC unchanged; status CONNECTED unchanged; credential reference present, unchanged. The
    separately-authorized 5-minute timer continued running; no successful pass was observed during the probe window
    (consistent with the ongoing failure). No change is attributable to the probe.
12. **Remaining uncertainty:** The probe proves the access token is **valid** and the **base history cursor is
    accepted** — so the worker's `RuntimeError` is **NOT** token expiry, **NOT** auth rejection (401), **NOT** a
    permission/policy failure (403), and **NOT** an invalid history cursor (404). A 200 on `history.list` does **not**
    prove the full pass succeeds. The exact failing call is still unconfirmed: it lies in the worker's **fuller
    multi-request pass**, which a 2-GET probe cannot reproduce. Most plausible, given the ruled-out causes:
    - a per-message **`messages.metadata`** (the allowlist sender pre-check) or **`messages.get`** (raw fetch) returning
      a non-200 (e.g. 429 from the per-pass request burst over a 35h backlog, or a 404/403 on a specific message); or
    - a non-200 on a **later paginated `history.list` page** (the worker omits `maxResults` and walks up to 20 pages;
      the probe used `maxResults=1`, a single page).
    - **Ruled out:** a >20-page backlog raises `MessageCaptureError` (not `RuntimeError`), so the page ceiling is not
      the cause; and the base `history.list` call itself is healthy.
13. **Smallest safe corrective next step:** **Deploy R1 (PR #500).** Its per-operation, secret-free diagnostics will
    self-label the exact failing `operation` (`messages.metadata` / `messages.get` / `history.list`) and HTTP status on
    the next real pass — which is now the fastest way to the precise cause, because the probe has eliminated the
    re-auth and cursor-recovery causes and the real failure is in the multi-request pass. R1 is DARK (additive logging +
    health + a reversible, additive migration 0011); it mutates no data and changes no cursor/exit semantics.
    *Alternative (if diagnosis-before-deploy is preferred):* a separately-authorized **expanded read-only probe** with a
    larger GET budget, replicating the worker's history-pagination + per-message metadata/get path to localize the
    failing call without deploying.
14. **Next Sponsor authorization required:** EITHER (recommended) **approve deploying PR #500** (apply migration 0011 +
    recreate backend — no re-auth, no cursor change, no financial action); OR authorize an **expanded read-only Gmail
    probe** (budget > 2 GETs). **Re-authorization and cursor recovery are NOT indicated by this evidence** and must not
    be undertaken on the strength of the earlier (now-superseded) hypotheses.

## Caution statements honored (§11)

- A 200 on `profile`/`history.list` is reported ONLY as "token accepted" / "cursor accepted" — **not** as proof that
  ingestion is healthy or that the backlog has been recovered.
- No claim is made that any single status proves a specific Google-side cause; the remaining cause is explicitly
  stated as unconfirmed and localized to the multi-request pass.
