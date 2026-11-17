# Withdrawal V1 — Mailbox OAuth Observability & Alerting (pkt-149 / ADR-0113)

**Status:** Implemented (repository-only; additive; migration-free; DARK by default — no production deploy, no Google
Console change, no mailbox reconnect, no cursor mutation, no withdrawal creation/correlation by this packet).
**Packet:** GFX-WITHDRAWAL-OAUTH-OBSERVABILITY-HARDENING-149 · **Date:** 2026-11-16.

## Why

Twice — **2026-11-08** and again **2026-11-15** — both pilot Gmail grants expired together (the OAuth app's Testing-mode
7-day refresh-token expiry; see `WITHDRAWAL_V1_OAUTH_PRODUCTION_READINESS_CHECKLIST.md` §1 for the VERIFIED-vs-inference
labelling) and Withdrawal Intelligence ingestion went blind for ~8 days while `consecutive_failures` silently climbed to
797. Nothing distinguished "the owner must re-consent" from a transient Google blip, and no early signal fired before a
prolonged blindness. This capability closes that gap **without** touching Gmail, a token, a credential or a cursor.

## Health model — the seven statuses

`broker_intelligence/oauth_health.py` classifies each mailbox from the **secret-free** `health.mailbox_health` dict
(masked email, stable diagnostic `Code`, HTTP status, counts, booleans) — it is strictly additive and reads no field the
existing read-model does not already expose. The seven explicit statuses:

| Status | Meaning | Derived from |
|---|---|---|
| `HEALTHY` | recent successful sync; counter 0; no blocking code (incl. "no new mail / cursor did not advance") | base state `MAILBOX_SYNC_SUCCESSFUL` |
| `TRANSIENT_FAILURE` | a retryable Google error not yet persisted | `GMAIL_HTTP_429/5XX`, `GMAIL_CONNECTION_ERROR`, `GMAIL_TIMEOUT`, `OAUTH_REFRESH_FAILED`, below the persist thresholds |
| `DEGRADED` | a failure that has persisted, or a bounded/cursor/other error blocking progress but not re-consent/config | transient aged past stale or ≥3 consecutive; `GMAIL_HISTORY_CURSOR_INVALID`; `MESSAGE_*`/`DATABASE_COMMIT_FAILED`; stale-no-error (no stall) |
| `RECONSENT_REQUIRED` | the owner must re-authorize Gmail (the 7-day-expiry signature) | mailbox `status` ERROR/REVOKED, or a re-auth code (`OAUTH_PERMISSION_DENIED`/`GMAIL_HTTP_401`/`403`) — the `REAUTH_CODES` set, defined once in `health.py` |
| `SECURITY_CONFIGURATION_ERROR` | server-side OAuth client / credential posture fault — an operator fix, never an owner reconnect | `OAUTH_CLIENT_NOT_CONFIGURED`, `CREDENTIAL_MISSING`, `CREDENTIAL_STORE_ERROR` |
| `SYNCHRONIZER_STALLED` | the ingestion worker itself is not running | fleet-wide: newest `last_attempted_sync` across connected mailboxes older than the recovery window |
| `UNKNOWN_FAIL_CLOSED` | an unrecognised / bare failure — fail closed, never silently HEALTHY | `UNKNOWN_FAILURE` or any code in no known bucket |

The crucial distinction the incident lacked — **RECONSENT_REQUIRED vs TRANSIENT_FAILURE** — is drawn here structurally,
by code, never by parsing a message string. "No new mail" (a connected, synced mailbox whose cursor simply did not move)
is `HEALTHY` and never alerts.

## Alert severity & thresholds (value-blind, pure)

Severity vocabulary `OK < INFO < WARNING < DEGRADED < CRITICAL`. Thresholds (reusing the health read-model's own
`BROKER_INTELLIGENCE_STALE_SECONDS`=15 min and `BROKER_INTELLIGENCE_RECOVERY_SECONDS`=60 min, so there is one source of
truth for the cadence maths):

- First blocking failure → recorded (INFO). **3 consecutive** failures → **WARNING**.
- **15 min** without success → **DEGRADED**. **60 min** without success → **CRITICAL**.
- `invalid_grant` / permission denied → **RECONSENT_REQUIRED** immediately (CRITICAL), regardless of count.
- Synchronizer absent/stalled → **CRITICAL** (`SYNCHRONIZER_STALLED`).
- Cursor present but not advanced while there is genuinely no new mail → **no alert**.
- Recovery → exactly **one** recovery event, then the active alert clears.
- De-duplication + **30-min cooldown** via the pure `decide_alert(prev, current, now)` (NONE / OPEN / UPDATE / REMIND /
  SUPPRESS / RECOVER); the dedup key is status-scoped so a change of condition is a new alert, not a suppressed one.

`decide_alert` is a **pure** function: the caller supplies the previously-emitted alert snapshot and `now`; the delivery
layer persists state and acts. No store is added by this packet.

## Surfaces

- **Operator API:** `GET /api/broker-intelligence/operator/mailbox-health/` — staff-only (`IsAdminUser`) and DARK
  (`BROKER_MAILBOX_OPERATOR_HEALTH_ENABLED` → 404). Returns the value-blind coverage projection (counts, synchronizer
  stall, per-mailbox masked identity + status + severity + last-success age + recommended action).
- **Operations console:** Admin → Operations → **Mailbox Health** (read-only; reuses the existing admin primitives).
- **ADR-0035 rollup:** a `mailbox_ingest` subsystem probe in `core/operational_health.py` maps the model into the
  operator health vocabulary (RECONSENT/SECURITY → `MISCONFIGURED`; stalled → `OFFLINE`), so it appears in the existing
  `OperationalReadinessView` + `operational_health` CLI.
- **CLI / alert signal:** `broker_mailbox_health --oauth-health --exit-nonzero-on-reconsent
  --exit-nonzero-on-synchronizer-stalled` (additive flags; default invocation unchanged). The existing **DARK**
  `ops/monitoring/guvfx-broker-mailbox-health.*` timer is the delivery vehicle — it is **not** enabled by this packet.
- **Owner UI:** a truthful, additive "ingestion degraded / needs reconnection" banner on the Withdrawal Intelligence
  page, derived from the existing owner-scoped `/mailboxes/status/` payload. It never zeroes the withdrawal cards and
  only appears on positive evidence of degradation.

Value-blindness is enforced by construction (every input is an already-masked health dict) and asserted in tests
(`tests_oauth_health.py`): no token, refresh token, authorization code, client secret, credential ref, raw cursor, email
body, or full address ever appears in any output.

## Phase 7 — Correlation-scheduling assessment (code-only; NO production schedule added here)

**Confirmed current state (repo-wide):** production schedules **neither** `run_mailbox_ingest` **nor**
`run_withdrawal_correlation`. The only installed-style broker artifact is the DARK (not-installed) health timer. All
other references to either command are documentation or example unit files, never a deployed cron/systemd timer. Both
commands are DARK/manual (refuse without their flag or `--force`).

**Which event categories are safe for automatic correlation:**

- **Reference-carrying** `EXTERNAL_WITHDRAWAL` / completion events (a stable broker reference) are the candidates for
  safe auto-correlation: `broker_intelligence.correlation.run_correlation` is pure and idempotent, projects only
  broker-intelligence withdrawal records, and matches on the broker's own reference — so a re-run does not double-count.
- **ADR-0090 is preserved and must stay preserved:** reference-less Vantage (and any ref-less) `EXTERNAL_WITHDRAWAL`
  events are deliberately downgraded to `UNKNOWN` by `_guard_reference` and are **manual-review only**. Automatic
  correlation must **never** project a withdrawal from a ref-less/UNKNOWN event; those remain the staff-gated ADR-0092
  path.

**Idempotency / locking / overlap:** `run_correlation` is idempotent and operates only on already-ingested events, so it
is safe to re-run; a live pass additionally requires `BROKER_CROSS_INBOX_DEDUP_ENABLED` (fail-closed) unless forced. It
does not write cursors and never performs a financial action. If ever scheduled it should run **after** ingestion (not
concurrently with a cursor-advancing ingest pass on the same owner) and be singleton-guarded to avoid overlapping passes.

**Recommendation (explicitly a SEPARATE follow-up, not this PR):** combining correlation-scheduling with OAuth alerting
would mix a (safe but) state-projecting change into an observability-only, value-blind hardening PR. Per the packet,
correlation scheduling is left as a **separate, independently-reviewed follow-up**: arm `run_withdrawal_correlation` on a
bounded post-ingestion schedule (singleton-guarded, reference-carrying events only, ADR-0090 preserved) under its own
packet and decision record. **No production schedule is added by pkt-149.**
