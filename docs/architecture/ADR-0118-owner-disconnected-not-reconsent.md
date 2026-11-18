# ADR-0118 - A member-disconnected mailbox is OWNER_DISCONNECTED, not a reconsent fault

- **Status:** Accepted (repository-only; additive; migration-free; corrects an ADR-0113 classification; no production
  deploy, no flag enablement, no Google Cloud Console change, no mailbox reconnect, no cursor mutation, no withdrawal
  creation/correlation by this packet).
- **Packet:** GFX-DEADLINE-PRIORITY-REENTRY-158 (selected work item: withdrawal truthful-state observability).
- **Date:** 2026-11-18.
- **Amends (never replaces):** ADR-0113 (value-blind mailbox OAuth health). Every other ADR-0113 contract — the
  value-blindness, the alert dedup/cooldown/recovery model, the owner-facing `health.customer_mailbox_status` banner,
  and the thresholds — is unchanged. The seven ADR-0113 statuses are unchanged; this ADR adds an **eighth**.

## Context

ADR-0113 classifies each mailbox from its secret-free `health.mailbox_health` dict. Its `classify_mailbox_oauth_health`
mapped `conn_status in ("REVOKED", "ERROR") or code in REAUTH_CODES` to `RECONSENT_REQUIRED` — floor severity
`CRITICAL`, `recovery_required=True`. `aggregate_oauth_health` counts those into `reconsent_required_count` /
`any_recovery_required` / `worst_alert_severity`, and `core/operational_health.probe_mailbox_ingest` maps
`RECONSENT_REQUIRED -> MISCONFIGURED` (a FAULT), degrading the whole subsystem and (via `decide_alert`) opening a
CRITICAL "an owner must re-authorize Gmail" operator alert.

## Verified facts (read-only trace, 2026-11-18)

- `ConnectedMailbox.status = REVOKED` has **exactly one writer**: `broker_intelligence/views.py` `MailboxRevokeView`
  — the member's own "disconnect" action, which also deletes the stored credential (`credential_ref`). **No automated
  or error path ever writes `REVOKED`** (nor `ERROR`); the recurring 7-day Testing-mode grant expiry keeps
  `status = CONNECTED` and surfaces as a **REAUTH code** (`OAUTH_PERMISSION_DENIED` / 401 / 403), not as `REVOKED`.
- In the INGESTION / membership sense `REVOKED` is already treated as "disconnected / excluded": the ingestion worker
  skips it (`ingestion.py`), several queries exclude it (`views.py`), and the live-mailbox count excludes it
  (`views.py`). The OWNER-facing read-model deliberately differs: `health.mailbox_health` maps `REVOKED` to
  `RECOVERY_REQUIRED` so the owner's own banner says "reconnect to resume" — correct, owner-scoped, and preserved
  unchanged. The defect was confined to the OPERATOR OAuth-health classifier escalating a `REVOKED` row to an incident.
- Therefore the ADR-0113 operator path **inverted the module's own purpose**: a routine, intentional member disconnect
  was reported as an incident-grade reconsent fault + CRITICAL alert — crying wolf, which trains operators to ignore
  the very alert meant to catch the genuine ~weekly outage. (Latent until the operator health is armed:
  `probe_mailbox_ingest` and the operator endpoint are both DARK, but the defect is in deployed code and would
  mis-fire the moment the flags are enabled.)

## Decision

Introduce an eighth, truthful status and keep it strictly separate from the reconsent signal.

1. **`OWNER_DISCONNECTED`** (`broker_intelligence/oauth_health.py`): a mailbox with `conn_status == "REVOKED"` is
   classified `OWNER_DISCONNECTED` — severity `SEV_OK`, `recovery_required=False`, `reconsent_required=False`, a
   "No action" recommended action. A member's deliberate disconnect **wins even over a leftover re-auth code** (the
   member's intent is authoritative). `conn_status == "ERROR"` and the `REAUTH_CODES` remain `RECONSENT_REQUIRED` — the
   genuine owner-reconsent / 7-day-expiry case is unchanged.
2. **No alert.** `decide_alert` treats `OWNER_DISCONNECTED` exactly like `HEALTHY`: it never OPENs an alert, and if the
   mailbox was previously alerting (e.g. the member resolves a reconsent condition by disconnecting), it emits exactly
   one RECOVER and clears. A disconnect can never raise an operator alert.
3. **Not a fault.** `core/operational_health._MAILBOX_OAUTH_HEALTH_MAP` maps `OWNER_DISCONNECTED -> HEALTHY` (a
   non-fault), so `probe_mailbox_ingest` never rolls the subsystem to `MISCONFIGURED` because of a disconnected mailbox.
4. **Truthful, not hidden.** `aggregate_oauth_health` adds `owner_disconnected_count` and keeps the mailbox visible in
   the per-mailbox list and `counts_by_status` (quarantine-don't-destroy: the operator still sees the disconnect
   truthfully, it just isn't a fault). The operator page renders it in a muted, deliberately non-alert colour with its
   own summary cell.

## Scope and non-goals

- **Operator-health surface only.** The owner-facing `health.customer_mailbox_status` / `MailboxIngestionNotice` banner
  is deliberately unchanged: an owner seeing "reconnect to resume" for a mailbox they disconnected is correct and
  owner-scoped. This ADR does not touch it.
- **CLI surfaces.** `broker_mailbox_health --oauth-health` now reports `owner_disconnected_count` (so the summary
  reconciles: monitored = healthy + owner_disconnected + reconsent + degraded + …), and `--exit-nonzero-on-reconsent`
  no longer trips on a member disconnect (it is no longer `RECONSENT_REQUIRED`). The PRE-EXISTING R1 gates
  `--exit-nonzero-on-recovery` / `--exit-nonzero-on-degraded` read the owner read-model (`service_health`), where a
  disconnected mailbox is truthfully "not syncing"; they are intentionally NOT changed — that is an accurate "a mailbox
  is not producing data" signal, distinct from the false INCIDENT (reconsent / CRITICAL / MISCONFIGURED) this ADR
  removes. Re-pointing those pre-pkt-149 gates at the OAuth classification would be a separate decision.
- **Additive + migration-free.** No model, migration, schedule, flag enablement, or production mutation. The merged code
  is DARK (the operator probe/endpoint flags stay OFF); nothing in production changes on merge.
- **Behaviour change (recorded):** the ADR-0113 test `test_reconsent_from_status_error_and_revoked` is split — `ERROR`
  stays `RECONSENT_REQUIRED`; `REVOKED` becomes `OWNER_DISCONNECTED`. This is the one intentional behaviour change.

## Consequences

- A member disconnecting a mailbox no longer rolls the OAuth-health subsystem to `MISCONFIGURED` or fires a false
  CRITICAL "owner must re-authorize" operator alert (and no longer trips `--exit-nonzero-on-reconsent`) — the reconsent
  alert regains its signal value for the real 7-day-expiry outage.
- A future reader must keep `OWNER_DISCONNECTED` (member intent) distinct from `RECONSENT_REQUIRED` (grant died on the
  owner). If a NEW code path is ever added that sets `status = REVOKED` to mean "Google revoked the grant" rather than
  "the member disconnected", this mapping must be revisited (today no such path exists).
