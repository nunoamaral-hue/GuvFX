# ADR-0113 - Value-blind mailbox OAuth health, early alerting and operator projection

- **Status:** Accepted (repository-only; additive; migration-free; default behaviour backward-compatible; no production
  deploy, no Google Cloud Console change, no mailbox reconnect, no cursor mutation, no withdrawal creation/correlation,
  no scope change by this packet).
- **Packet:** GFX-WITHDRAWAL-OAUTH-OBSERVABILITY-HARDENING-149.
- **Date:** 2026-11-16.
- **Builds on (never replaces):** INGESTION_INCIDENT_R1 health read-model (`broker_intelligence/health.py`,
  `ingest_diagnostics.py`), ADR-0035 Operational Readiness (`core/operational_health.py`), ADR-0088/0089/0090 (generic
  detection + `_guard_reference`), ADR-0092 (staff-gated correlation). Every one of those contracts is unchanged.

## Context

The Withdrawal Intelligence Gmail OAuth app is **believed to be in Google "Testing" publishing status** (a
HIGH-CONFIDENCE INFERENCE pending a Google Cloud Console read — see
`WITHDRAWAL_V1_OAUTH_PRODUCTION_READINESS_CHECKLIST.md` §1); Google's documented Testing-mode behaviour of expiring
refresh tokens roughly every 7 days is the VERIFIED part. Both pilot grants expired together on 2026-11-08 and again on
2026-11-15 (consistent with that mechanism); ingestion went blind for ~8 days while `consecutive_failures` climbed to
797, with nothing distinguishing "the owner must re-consent" from a transient Google error and no early signal before
prolonged blindness. The recovery (pkt-147) proved the pipeline
is sound; the gap is **observability**: a failing mailbox was identifiable only by a staff reading a read-only command,
and that read did not classify re-consent vs transient, nor alert before a long outage.

## Verified facts

- The health read-model (`health.mailbox_health` / `service_health`) already emits secret-free per-mailbox dicts
  (masked email, stable `Code`, HTTP status, counts, booleans) and is unmigrated-safe.
- `ingest_diagnostics.Code` is the authoritative 20-code taxonomy; `BLOCKING_CODES` and `health.REAUTH_CODES` already
  encode "cannot progress without an out-of-band action" and "the owner must re-consent", respectively.
- Production schedules neither `run_mailbox_ingest` nor `run_withdrawal_correlation` (confirmed repo-wide); the only
  installed-style broker artifact is the DARK health timer.

## Decision

Add a **strictly additive, value-blind** observability layer on top of the existing read-model — introducing no
migration and changing no existing contract.

1. **Seven-status model** (`broker_intelligence/oauth_health.py`): `classify_mailbox_oauth_health` maps the secret-free
   `mailbox_health` dict into `HEALTHY / TRANSIENT_FAILURE / DEGRADED / RECONSENT_REQUIRED /
   SECURITY_CONFIGURATION_ERROR / SYNCHRONIZER_STALLED / UNKNOWN_FAIL_CLOSED`, plus a fine-grained failure class, an
   alert severity against bounded thresholds (3 consecutive → WARNING; 15 min → DEGRADED; 60 min → CRITICAL; re-auth →
   CRITICAL immediately; stall → CRITICAL; no-new-mail → no alert), and a safe recommended operator action.
   `aggregate_oauth_health` rolls this into a fleet coverage projection + the synchronizer-stall determination.
2. **Pure alert gate:** `decide_alert(prev, current, now)` encodes de-duplication, a 30-min cooldown, and exactly-once
   recovery — with no store, so the delivery layer owns persistence. The health layer **never** reconnects, rotates, or
   mutates anything.
3. **Reuse existing infra:** a `mailbox_ingest` probe in `core/operational_health.py` (surfaced by the existing
   `OperationalReadinessView` + `operational_health` CLI); additive `--oauth-health` / `--exit-nonzero-on-reconsent` /
   `--exit-nonzero-on-synchronizer-stalled` flags on `broker_mailbox_health` (the DARK `ops/monitoring` timer is the
   delivery vehicle, not enabled here); a new staff-only, DARK-gated operator endpoint
   (`operator/mailbox-health/`, `BROKER_MAILBOX_OPERATOR_HEALTH_ENABLED`) + an Operations-console page; and a truthful,
   additive owner banner derived from the existing `/mailboxes/status/` payload.

## Consequences

- An expired grant now surfaces as `RECONSENT_REQUIRED` (CRITICAL) and a dead worker as `SYNCHRONIZER_STALLED`
  (OFFLINE) — both before prolonged blindness — in an operator view, a CLI alert signal, and the ADR-0035 rollup; and
  the owner is truthfully warned that new notifications may be delayed, without the withdrawal cards ever being zeroed.
- Value-blindness is preserved by construction (every input is an already-masked health dict) and asserted in tests.
- Nothing is armed in production: the operator endpoint and alert signals are DARK/opt-in; no schedule, flag, or Google
  Console state is changed.

## Risks and controls

- *Over-alarming on transient blips* → thresholds + a 30-min cooldown + status-scoped dedup; "no new mail" is explicitly
  HEALTHY.
- *Mis-telling an owner to reconnect on a server-side fault* → `SECURITY_CONFIGURATION_ERROR` is operator-only and never
  sets `reconsent_required`; only `REAUTH_CODES`/ERROR/REVOKED drive RECONSENT_REQUIRED (consistent with the existing
  `customer_mailbox_status`).
- *Secret leakage* → no token/cursor/credential/body/full-address in any output; the operator endpoint is staff-only and
  DARK-gated; tests assert the absence.
- *Scope creep into correlation scheduling* → explicitly deferred to a separate, independently-reviewed follow-up
  (ADR-0090 preserved; no production schedule added).

## Evidence / validation

- `broker_intelligence/tests_oauth_health.py` (classifier matrix, severity thresholds, synchronizer stall, alert
  dedup/cooldown/recovery, no-new-mail non-alert, value-blindness, operator endpoint staff-gating + DARK 404 +
  cross-owner); `core/tests_operational_readiness.py::MailboxIngestProbeTests`; `tests_ops_commands.py` command flags;
  frontend `MailboxIngestionNotice.test.tsx`. Backend `broker_intelligence` + `core.tests_operational_readiness` =
  1259 passed / 15 skipped. Frontend build + ESLint clean; new vitest green. **Not covered:** no production run, no live
  mailbox, no Google Console verification, no alert-delivery wiring (DARK), no correlation scheduling.

## Reversal path

Repository-only and migration-free: revert the PR. Nothing is enabled in production, so there is nothing to disable on a
host; the DARK flags simply never turn on.

## Revisit trigger

When the Google OAuth app is published (ending the 7-day expiry), when alert delivery is wired, or when
`run_withdrawal_correlation` is scheduled — each its own decision record.
