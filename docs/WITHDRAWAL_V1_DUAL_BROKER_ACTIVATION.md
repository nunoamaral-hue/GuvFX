# Withdrawal Intelligence V1 — Dual-Broker Ingestion Activation (PR4)

**Status: PREPARED, NOT ARMED.** Continuous Gmail ingestion is DARK
(`BROKER_INTELLIGENCE_INGEST_ENABLED` unset/OFF). This document is the exact production activation plan.
**Arming is a separate, deliberate Sponsor act** — nothing here enables ingestion. Verify readiness with the
read-only report first:

```
docker compose exec -T guvfx-backend python manage.py withdrawal_ingest_readiness
```

That command checks every precondition and **enables nothing** (it never polls, ingests, or contacts a broker).

## Two distinct readiness levels

| Level | What it means | Requires |
|---|---|---|
| **CAPTURE-ready** | GuvFX acquires + stores authenticated broker mail as immutable evidence, append-only, owner-attributed. UNKNOWN-category authenticated messages are preserved as evidence **without** creating a financial record. | the 5 preconditions below |
| **Withdrawal-surfacing-ready** | A genuine *external withdrawal* is classified and projected into a `Withdrawal`. | CAPTURE-ready **plus** a per-broker positive external-withdrawal classifier **certified against a genuine external-withdrawal email** (`external_withdrawal_classifier.CERTIFIED_BROKERS`, currently empty — no sample exists yet) |

Capturing evidence does **not** require the positive classifier (§6). A genuine external-withdrawal email may initially
be captured as category `UNKNOWN` — that is acceptable: the evidence is preserved, append-only, and can never be
misclassified as a deposit or internal transfer.

## Architecture (already built + certified DARK)

- **One mailbox, one cursor, one worker.** `mailbox_ingest.run_mailbox_ingest` polls each CONNECTED `ConnectedMailbox`
  via the stored Gmail `historyId` cursor; the cursor advances **only after** the batch is durably ingested, so an
  interrupted run safely re-fetches (ingestion is content-hash idempotent) — no message lost or double-counted.
- **Broker-specific authenticated routing from one stream.** `register_default_parsers()` registers **both** the
  TradersWay and IS6 deterministic parsers; `select_parser` routes each message by content (`can_parse`). Both brokers
  share the one mailbox and stay independently authenticated + attributed.
- **Sender filtering / personal-email exclusion (fail-closed).** `broker_senders.is_allowlisted_broker_sender` gates
  acquisition + retention on the EXACT registrable-domain allowlist (union of active `BrokerProfile` approved domains);
  an empty allowlist retains **nothing**, so a personal inbox cannot leak into evidence.
- **Sender authenticity (fail-closed).** For REAL mail an event is created only on a passing `Authentication-Results`
  verdict from the trusted receiver (`mx.google.com`; RFC 8601 — forged foreign authserv-ids ignored). A spoofed
  broker-domain email is retained as quarantined evidence but yields **no** event.
- **Immutable evidence.** `EvidenceBlob` is content-addressed + write-once (app-layer + DB triggers); `BrokerEvent` is
  append-only. Suspect/unparseable mail is quarantined, never dropped.
- **Owner-scoped attribution.** Every ingest passes `owner_user=mailbox.user`; attacker-controllable recipient headers
  can never cross-attribute to another member. The transaction account is resolved from the broker-stated
  source/destination accounts (unambiguous match only), never from the recipient email.
- **Per-mailbox isolation + retry-safe.** One mailbox's failure never aborts the others; a failed pass does not
  advance the cursor.

## Monitoring / alerting

- Each pass logs a secret-free summary (`guvfx.broker_intelligence.mailbox_ingest`): `mailboxes/ingested/events/failed`.
- Retention/authenticity drops log at INFO with the evidence sha prefix (no body, no secret).
- Operational alert signals to watch once armed: `failed > 0` in a pass, a mailbox stuck `ERROR`, zero `ingested`
  across many passes for a CONNECTED mailbox (cursor/credential problem), or a growing count of quarantined-no-event
  evidence (possible spoofing or an unparsed template).

## EXACT production activation plan (DO NOT run without Sponsor authorization)

1. **Confirm readiness** (read-only): `… manage.py withdrawal_ingest_readiness` → `verdict: CAPTURE-READY`.
2. **Confirm OAuth credentials** are provisioned in the ingestion secret store (settings-then-env), never the DB/repo:
   `GMAIL_OAUTH_CLIENT_ID`, `GMAIL_OAUTH_CLIENT_SECRET`, `GMAIL_OAUTH_REDIRECT_URI`.
3. **Arm the flag** (the single DARK gate) in `/home/ubuntu/guvfx-prod/beta.env`:
   `BROKER_INTELLIGENCE_INGEST_ENABLED=1`, then recreate only the backend
   (`docker compose up -d --force-recreate --no-deps guvfx-backend`; **never** `--remove-orphans`).
4. **Schedule the pass** (bounded cadence, e.g. every 5 min) as a cron/scheduled service running
   `manage.py run_mailbox_ingest` — never a long-running process started from an interactive SSH session (RULE 1).
5. **Observe one cycle** read-only: the log summary, the new `EvidenceBlob`/`BrokerEvent` rows (owner-attributed,
   category as the parser emits — DEPOSIT/INTERNAL_TRANSFER/UNKNOWN), and `Withdrawal.objects.count()` (stays 0 until a
   certified external classifier + a genuine external-withdrawal sample exist).

**STOP.** Do not arm continuous ingestion without explicit Sponsor authorization. Withdrawal *surfacing* additionally
requires a Sponsor-initiated genuine external withdrawal (§8) to certify a positive classifier; GuvFX must never
initiate/confirm/cancel/modify any financial transaction.

## Rollback

Set `BROKER_INTELLIGENCE_INGEST_ENABLED` back to OFF (or remove the scheduled pass) and recreate the backend. Already
captured evidence is immutable and append-only and is retained (quarantine, never destroy). No schema change is
involved in arming.
