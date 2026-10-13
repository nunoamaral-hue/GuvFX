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

## Verified secret/flag wiring (2026-10-13, read-only)

The backend-only broker-intel config is injected by **`docker-compose.override.yml`**, which (and ONLY for
`guvfx-backend` — the MT5 workers never load it) adds `./broker_intel.env` to the backend `env_file` and bind-mounts
the credential + evidence stores. So:
- **Gmail OAuth creds + credential/evidence roots live in `broker_intel.env` (mode 0600), NOT in `beta.env`.** The
  ingestion flag must go here too — never in the shared `beta.env`.
- `BROKER_INTELLIGENCE_INGEST_ENABLED` is currently **ABSENT** from `broker_intel.env` → ingestion is DARK.
- Credential store `/srv/guvfx/broker_intel_cred` and evidence store `/srv/guvfx/broker_intel_evidence` are mode
  **0700**, owned by the backend `appuser` (uid 10001), bind-mounted into `guvfx-backend` only.

## EXACT production activation plan (DO NOT run without Sponsor authorization)

1. **Confirm readiness** (read-only, enables nothing):
   `docker compose exec -T guvfx-backend python manage.py withdrawal_ingest_readiness` → `verdict: CAPTURE-READY`.
2. **Confirm identities + OAuth** (read-only): mailbox CONNECTED (scope `gmail.readonly`); TradersWay (acct 46) **and**
   IS6 (acct 47) `BrokerEmailIdentity` VERIFIED; OAuth creds present in `broker_intel.env`.
3. **Arm the flag — in the backend-only `broker_intel.env`, NEVER `beta.env`:** append
   `BROKER_INTELLIGENCE_INGEST_ENABLED=1` to `/home/ubuntu/guvfx-prod/broker_intel.env` (preserve mode 0600), then
   recreate only the backend: `docker compose up -d --force-recreate --no-deps guvfx-backend` (**never**
   `--remove-orphans`). Re-run the readiness command to confirm the flag now reads ON.
4. **Bounded supervised observation FIRST** (before any unattended schedule): run the pass MANUALLY a few cycles,
   operator-watched — `docker compose run --rm --no-deps guvfx-backend python manage.py run_mailbox_ingest` — and after
   each, compare against the baseline below: new rows are append-only + owner-attributed to user 29, categories are
   DEPOSIT/INTERNAL_TRANSFER/UNKNOWN only, `Withdrawal.objects.count()` stays 0 (no certified external classifier).
5. **Enable the supervised schedule — ONE worker, ONE mailbox, ONE cursor** — a host **systemd timer + oneshot**
   (supported mechanism; survives reboot; single-flight; NEVER a process launched from an interactive SSH session,
   RULE 1):
   `/etc/systemd/system/guvfx-broker-ingest.service`:
   ```
   [Unit]
   Description=GuvFX broker-email ingestion (dual-broker: one mailbox, one cursor)
   After=docker.service
   [Service]
   Type=oneshot
   WorkingDirectory=/home/ubuntu/guvfx-prod
   ExecStart=/usr/bin/flock -n /run/lock/guvfx-broker-ingest.lock docker compose run --rm --no-deps guvfx-backend python manage.py run_mailbox_ingest
   User=ubuntu
   ```
   `/etc/systemd/system/guvfx-broker-ingest.timer`:
   ```
   [Unit]
   Description=Run GuvFX broker-email ingestion every 5 minutes
   [Timer]
   OnBootSec=3min
   OnUnitActiveSec=5min
   AccuracySec=30s
   [Install]
   WantedBy=timers.target
   ```
   `sudo systemctl daemon-reload && sudo systemctl enable --now guvfx-broker-ingest.timer`. (A `cron.d` entry with the
   same `flock` is acceptable; systemd is preferred for journald supervision + `systemctl status`.)

### Supervised service characteristics (verified against the code)
- **Worker/service identity:** the backend image's `manage.py run_mailbox_ingest` in an ephemeral `--rm` container
  (loads the override → `broker_intel.env`). ONE worker; it iterates CONNECTED mailboxes = `ConnectedMailbox 1` only;
  both brokers are routed from that single acquisition stream by content (`select_parser`). The command refuses to run
  unless `BROKER_INTELLIGENCE_INGEST_ENABLED` is ON (no `--force` in production).
- **Schedule / polling interval:** every 5 min. Gmail `historyId` incremental; well within quota for one mailbox.
- **Concurrency:** `flock -n` → single-flight; a cycle is skipped if the previous is still running, so the one durable
  cursor is never advanced by two overlapping passes.
- **Retry / recovery:** the cursor advances ONLY after the batch is durably ingested; a failed/interrupted pass does
  not advance it, so the next cycle safely re-fetches (content-hash idempotent → no lost or duplicated mail). One
  mailbox's failure never aborts the run.
- **Cursor handling:** one `cursor_state` (Gmail `historyId`) on `ConnectedMailbox 1`, advanced post-batch only.
- **Resource consumption:** one short-lived container per 5-min cycle for one mailbox; negligible CPU/memory.
- **Monitoring / failure alerts:** each pass logs a secret-free summary (`guvfx.broker_intelligence.mailbox_ingest`:
  `mailboxes/ingested/events/failed`) to journald + container logs. Alert on: `failed>0` in a pass; a mailbox stuck
  `ERROR`; zero `ingested` across many passes for a CONNECTED mailbox (credential/cursor problem); a rising count of
  quarantined-no-event evidence (possible spoofing or an unparsed template).
- **Stop / disable:** `sudo systemctl disable --now guvfx-broker-ingest.timer` halts the schedule immediately; and/or
  set `BROKER_INTELLIGENCE_INGEST_ENABLED` OFF in `broker_intel.env` + recreate the backend (the command then refuses).
  Use both for belt-and-suspenders.
- **Rollback:** disable the timer, set the flag OFF, recreate the backend. Captured evidence is immutable/append-only
  and retained (quarantine, never destroy). No migration is involved in arming — rollback is config-only.

### Evidence baseline (BEFORE — 2026-10-13, read-only)
`EvidenceBlob = 3 · BrokerEvent = 1 (TradersWay, category UNKNOWN) · Withdrawal = 0 · BrokerEmailIdentity = 2
(TradersWay/46 + IS6/47, both VERIFIED)`. After arming, compare each supervised pass against this baseline.

**STOP.** Do not arm continuous ingestion without explicit Sponsor authorization. Withdrawal *surfacing* additionally
requires a Sponsor-initiated genuine external withdrawal (§8) to certify a positive classifier; GuvFX must never
initiate/confirm/cancel/modify any financial transaction.
