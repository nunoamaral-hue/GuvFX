# OAuth Sustainability & Ingestion-Monitoring Recommendation (Withdrawal V1, §11)

**Status:** implementation-ready recommendation. One item is SHIPPED in this packet (alertable health exit code);
the schema-touching items are DESIGNED and left **PD-gated** so the production schema boundary is not widened under
recovery. No OAuth scope is broadened and no Google publishing status is changed here — those are operator/governance
actions.

---

## 1. Root cause (recap, high confidence)

The 2026-10-14→17 outage was a Google OAuth **"Testing"-mode 7-day refresh-token expiry** (consent 2026-10-09 →
refresh failing ~2026-10-16; re-consent 2026-10-17 restored it). The code was healthy; the grant expired. A weekly
manual re-consent is **not** an acceptable model for unattended ingestion.

## 2. The sustainable fix (operator/governance — not code)

Publishing status and app verification are **not determinable or changeable from the host** (Google Cloud Console).
Recommended, in order:

1. **Confirm publishing status** in Google Cloud Console → APIs & Services → OAuth consent screen. The 7-day timing
   strongly implies External/**Testing**.
2. If Testing: **publish the app to Production.** Production-published refresh tokens do not expire on the 7-day
   Testing cadence (they still expire on revocation / inactivity / password change / scope change).
3. **Complete Gmail restricted-scope verification.** `gmail.readonly` is a *restricted* scope; production use by an
   External app generally requires Google's verification (app home page, privacy policy, scope justification, and a
   likely CASA security assessment). Keep the scope exactly `gmail.readonly` — do **not** broaden it.
4. Keep the single read-only mailbox + per-mailbox encrypted credential; nothing about the credential store changes.

**Governance:** route any publishing/scope/verification change through Programme Director security review. Do not
change it under incident recovery. Until then the manual re-consent remains the stopgap — which makes the monitoring
below essential so a lapse is caught *before* it silences ingestion.

## 3. Monitoring — what already exists

The R1 health model is strong and DARK: `service_health()` / `mailbox_health()` classify SUCCESSFUL / STALE /
RECOVERY_REQUIRED from DB metadata only (no credential decrypt, no Gmail call), the worker records
`consecutive_failures` / `last_error_code` / `last_error_http_status` fail-open, and a blocking `OAUTH_PERMISSION_DENIED`
forces RECOVERY_REQUIRED. The gap the outage exposed was that **a green systemd unit hid a dead mailbox** — the health
command only *printed*, so a scheduler could not alert on it.

## 4. SHIPPED in this packet (schema-free, behaviour-preserving)

- **Alertable exit code on `broker_mailbox_health`.** New opt-in flags `--exit-nonzero-on-recovery` and
  `--exit-nonzero-on-degraded` raise a non-zero exit (after the report prints) when any mailbox is RECOVERY_REQUIRED
  (resp. degraded). Default invocation still exits 0. A scheduled
  `broker_mailbox_health --exit-nonzero-on-recovery` now makes "no silent outage" consumable by ordinary cron /
  monitoring with zero new infrastructure. Tested (`tests_ops_commands.MailboxHealthExitCode`).

**Recommended ops wiring:** run it on a timer distinct from the ingest timer (e.g. hourly) so a dead mailbox pages
even while the ingest unit keeps exiting 0.

## 5. DESIGNED, PD-GATED (do NOT implement under recovery)

These would catch the 7-day cliff *before* it fails, but each touches schema or an observable state field; prod is at
schema 0010 with 0011 unapplied, so adding more is an explicit decision, not a recovery convenience.

### 5a. Forward-looking refresh-credential-age / expiry-proximity signal
- **Need:** surface "the current refresh credential is N days old, approaching the 7-day Testing cliff" as a health
  state (e.g. `REFRESH_EXPIRY_IMMINENT`) against a configurable `BROKER_INTELLIGENCE_REFRESH_MAX_AGE_SECONDS`.
- **Design:** add a nullable, additive, reversible `ConnectedMailbox.credential_obtained_at` (migration **0012**,
  DARK). Set it at the OAuth callback on **both** create and reconnect/update paths, and **not** on an access-token
  refresh (a refresh does not reset the Testing refresh-token clock). `health.py` emits a secret-free
  `refresh_credential_age_seconds` + proximity state, read via the existing `.only()` / `r1_columns_present()`
  unmigrated-safe pattern so prod (0010/0011) is never degraded.
- **ANTI-GAP (correctness guard — enforce, do not shortcut):** do **not** build this on `created_at` (INSERT-only, not
  refreshed on reconnect) or `updated_at` (churns every pass). Either silently under-reports age after a reconnect and
  would produce a false "healthy" proximity. A dedicated column is required; a `created_at`-based stopgap, if ever
  demanded, must be labelled explicitly APPROXIMATE/known-wrong-after-reconnect and must never gate health.

### 5b. Flip `ConnectedMailbox.Status` to `ERROR` ("Needs re-auth") on a blocking re-auth diagnostic
- **Need:** on `OAUTH_PERMISSION_DENIED` the worker records diagnostics but leaves `status = CONNECTED`, so the
  member/admin-visible status reads "Connected" while the mailbox is dead. The `Status.ERROR` enum exists for exactly
  this.
- **Design:** when `classify_exception` yields `blocking=True` + `recovery=SPONSOR_REAUTH`, include `status=ERROR` in
  the fail-open health write-set; reconnect already resets `status=CONNECTED`, so recovery self-heals.
- **Why AMBER (needs a decision):** this is a state transition other code reads — the ingest queryset polls
  CONNECTED-only, so a mailbox flipped to ERROR would drop out of the poll set. That is arguably desirable (stop
  hammering a dead grant) but must be a deliberate, verified change, separate from the schema-free work. Verify the
  poll-set interaction (and any member-facing surface) before implementing.

## 6. Out of scope / hygiene (noted, not actioned)

- Orphaned `.enc` credential files accumulate on each reconnect (previous ref left in place — the safe behaviour). A
  bounded, later cleanup of files referenced by no `ConnectedMailbox` is advisable; never a blind delete.
- Forged-From broker mail that fails auth is still written to the immutable store (quarantine-don't-drop). Message
  COUNT is unbounded (per-message size is bounded). Consider a quarantine-partition / growth-monitoring note; do not
  "fix" by dropping evidence.
