"""WP3b — standalone mailbox ingestion worker.

For each CONNECTED mailbox: list new messages (incremental, via the stored cursor), ingest each through the SAME
append-only pipeline (``ingest_message``), and advance the cursor ONLY after the whole batch is durably ingested —
so an interrupted run safely re-fetches (ingestion is content-hash idempotent) and no message is lost or skipped.

SECURITY: every ``ingest_message`` call passes ``owner_user=mailbox.user`` so recipient-header resolution is scoped
to the mailbox owner — a spoofed To:/Delivered-To/X-Original-To can never cross-attribute a message to another
member. One mailbox's failure never aborts the others (each is isolated). Read-only w.r.t. trading: NO order, NO MT5,
NO strategy — only the evidence store + BrokerEvent (and later the correlation projection) are written.

DARK: the live loop is gated by ``broker_intelligence_ingest_enabled()`` (the management command refuses unless armed
or --force). The Google OAuth client id/secret come from the ingestion service's secret store (settings-then-env),
never the DB/repo.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("guvfx.broker_intelligence.mailbox_ingest")


def _conf(name: str) -> str:
    from django.conf import settings
    v = getattr(settings, name, None)
    if v is None:
        v = os.getenv(name, "")
    return str(v or "")


def _record_mailbox_health(mailbox, **fields) -> None:
    """INGESTION_INCIDENT_R1 (§5) — persist ONLY the health-bookkeeping fields on the mailbox's OWN row. It NEVER
    writes ``cursor_state`` or ``last_successful_sync`` (so the §6 acquisition-safety contract is untouched), fields
    absent from the model (unmigrated) are skipped, and ANY write failure is swallowed — health bookkeeping must NEVER
    change the ingestion outcome or crash the pass (fail-OPEN for health; the ingestion path stays fail-closed)."""
    try:
        model_fields = {f.name for f in mailbox._meta.fields}   # concrete local columns only
        to_write = []
        for k, v in fields.items():
            if k in model_fields:
                setattr(mailbox, k, v)
                to_write.append(k)
        if not to_write:
            return
        if "updated_at" in model_fields:
            to_write.append("updated_at")
        mailbox.save(update_fields=to_write)
    except Exception:  # noqa: BLE001 — never let health bookkeeping affect ingestion
        logger.warning("mailbox %s: health bookkeeping write skipped", getattr(mailbox, "id", None))


def ingest_mailbox(mailbox, *, client=None) -> dict:
    """Ingest one CONNECTED mailbox. ``client`` is injectable (a GmailClient) for tests; otherwise a GmailApiClient is
    built from the configured OAuth client. Cursor advances ONLY after the batch is ingested. Returns a summary that
    now carries a secret-free ``diagnostic`` (R1 §4) on failure so the per-mailbox cause is visible without decrypting
    a credential or reading the exception message."""
    from django.utils import timezone
    from .gmail_source import GmailApiClient, GmailMailSource
    from .ingest_diagnostics import Code, IngestDiagnostic, Recovery, classify_exception
    from .ingestion import ingest_message
    from .models import Provenance
    from .parsers_tradersway import register_default_parsers

    # Register the deterministic broker parsers (idempotent by name+version). The registry is intentionally EMPTY at
    # import (DARK) and populated only by the armed worker — without this the pipeline selects no parser and every
    # message is quarantined as unparseable (no event ever produced). Per-mailbox call is safe (idempotent).
    register_default_parsers()

    attempted_at = timezone.now()
    old_cursor = str(getattr(mailbox, "cursor_state", "") or "")
    prior_failures = int(getattr(mailbox, "consecutive_failures", 0) or 0)

    if client is None:
        client_id, client_secret = _conf("GMAIL_OAUTH_CLIENT_ID"), _conf("GMAIL_OAUTH_CLIENT_SECRET")
        if not client_id or not client_secret:
            diag = IngestDiagnostic(Code.OAUTH_CLIENT_NOT_CONFIGURED, retryable=False, blocking=True,
                                    recovery=Recovery.OPERATOR_REVIEW)
            logger.warning("mailbox ingest failed for mailbox %s: code=%s", mailbox.id, diag.code)
            _record_mailbox_health(mailbox, last_attempted_sync=attempted_at, last_error_code=diag.code,
                                   last_error_http_status=None, last_error_at=attempted_at,
                                   consecutive_failures=prior_failures + 1, last_pass_cursor_advanced=False)
            return {"mailbox_id": mailbox.id, "ok": False, "error": "oauth_not_configured", "ingested": 0,
                    "events": 0, "no_event": 0, "diagnostic": diag.safe_dict()}
        client = GmailApiClient(mailbox, client_id=client_id, client_secret=client_secret)

    source = GmailMailSource(mailbox, client)
    ingested = 0
    events = 0
    try:
        for message in source.fetch():
            # SECURITY: owner-scoped resolution — never cross-attribute via a spoofed recipient header.
            ev = ingest_message(message, owner_user=mailbox.user, provenance=Provenance.REAL)
            ingested += 1
            if ev is not None:
                events += 1
        # Durable processing complete -> NOW advance the cursor. INSIDE the try so a commit failure (e.g. a transient
        # DB error on the mailbox UPDATE) is ISOLATED: the cursor simply isn't advanced -> the next pass safely
        # re-fetches (ingestion is content-hash idempotent), and the exception never propagates to abort the whole
        # run (the per-mailbox isolation contract).
        source.commit_cursor()
    except Exception as exc:  # noqa: BLE001 — a read/parse/commit failure must not advance the cursor or crash the run
        # R1 §4: classify to a STABLE, secret-free diagnostic and log ONLY the code + HTTP status + operation + mailbox
        # id — never the exception message, a URL, a Gmail message id, an address, a token, or a raw Google body.
        diag = classify_exception(exc)
        logger.warning("mailbox ingest failed for mailbox %s: code=%s http_status=%s operation=%s",
                       mailbox.id, diag.code, diag.http_status, diag.operation or "-")
        _record_mailbox_health(mailbox, last_attempted_sync=attempted_at, last_error_code=diag.code,
                               last_error_http_status=diag.http_status, last_error_at=attempted_at,
                               consecutive_failures=prior_failures + 1, last_pass_cursor_advanced=False)
        return {"mailbox_id": mailbox.id, "ok": False, "error": "fetch_failed", "ingested": ingested,
                "events": events, "no_event": ingested - events, "diagnostic": diag.safe_dict()}

    # SUCCESS: the cursor was advanced iff it changed; record health (resets the failure streak + clears last error).
    cursor_advanced = old_cursor != str(getattr(mailbox, "cursor_state", "") or "")
    # INGESTION_INCIDENT_R2: history ids skipped because the message was deleted from the mailbox (Gmail 404). Surfaced
    # so a non-zero count is visible at the run level (it is a normal, benign condition, not a failure).
    messages_gone = int(getattr(client, "messages_gone", 0) or 0)
    if messages_gone:
        logger.info("mailbox %s: %s message(s) skipped as gone (Gmail 404 between history.list and fetch)",
                    mailbox.id, messages_gone)
    _record_mailbox_health(mailbox, last_attempted_sync=attempted_at, last_error_code="",
                           last_error_http_status=None, last_error_at=None, consecutive_failures=0,
                           last_pass_cursor_advanced=cursor_advanced)
    # ``no_event`` = messages acquired that produced NO BrokerEvent (authenticated-but-quarantined, unparseable,
    # parser-declined, or a parser that RAISED — each already logged secret-free in ``ingest_message``). Surfaced in
    # the summary so a rising no-event volume (e.g. a broker template whose parser is crashing) is visible to an
    # operator at the run level, not only in the logs. (A parser crash is additionally WARNING-logged, distinct from
    # routine INFO; routine non-withdrawal broker mail would still produce an event with a non-withdrawal category.)
    return {"mailbox_id": mailbox.id, "ok": True, "ingested": ingested, "events": events,
            "no_event": ingested - events, "diagnostic": None, "cursor_advanced": cursor_advanced,
            "messages_gone": messages_gone}


def run_mailbox_ingest(*, limit_mailboxes: Optional[int] = None) -> dict:
    """Ingest all CONNECTED Gmail mailboxes. Each mailbox is isolated (one failure never aborts the others). NO flag
    check here — the caller (the DARK management command) gates the live run; this stays callable/testable."""
    from .models import ConnectedMailbox
    qs = (ConnectedMailbox.objects
          .filter(provider=ConnectedMailbox.Provider.GMAIL, status=ConnectedMailbox.Status.CONNECTED)
          .order_by("id"))
    if limit_mailboxes is not None:
        qs = qs[:limit_mailboxes]
    # ``failure_codes`` (code -> count) surfaces the R1 secret-free classification at the run level, so the journal /
    # command output names WHY a pass failed (e.g. {'GMAIL_HTTP_401': 1}) instead of only "failed = 1".
    summary = {"mailboxes": 0, "ingested": 0, "events": 0, "no_event": 0, "failed": 0, "messages_gone": 0,
               "failure_codes": {}}
    for mailbox in qs:
        # Per-mailbox isolation (contract): NOTHING a single mailbox does — including a GmailApiClient/source
        # construction error or any unexpected raise before/around ingest — may abort the pass. A failure is
        # counted and the loop continues to the next mailbox.
        try:
            res = ingest_mailbox(mailbox)
        except Exception as exc:  # noqa: BLE001
            from .ingest_diagnostics import classify_exception
            diag = classify_exception(exc)
            logger.warning("mailbox %s aborted: code=%s", getattr(mailbox, "id", None), diag.code)
            res = {"mailbox_id": getattr(mailbox, "id", None), "ok": False, "error": "mailbox_error",
                   "diagnostic": diag.safe_dict()}
        summary["mailboxes"] += 1
        summary["ingested"] += res.get("ingested", 0)
        summary["events"] += res.get("events", 0)
        summary["no_event"] += res.get("no_event", 0)   # acquired-but-no-event (quarantine / parse failure) volume
        summary["messages_gone"] += res.get("messages_gone", 0)   # history ids skipped as deleted (Gmail 404)
        if not res.get("ok"):
            summary["failed"] += 1
            code = (res.get("diagnostic") or {}).get("code", "UNKNOWN_FAILURE")
            summary["failure_codes"][code] = summary["failure_codes"].get(code, 0) + 1
    return summary
