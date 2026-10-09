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


def ingest_mailbox(mailbox, *, client=None) -> dict:
    """Ingest one CONNECTED mailbox. ``client`` is injectable (a GmailClient) for tests; otherwise a GmailApiClient is
    built from the configured OAuth client. Cursor advances ONLY after the batch is ingested. Returns a summary."""
    from .gmail_source import GmailApiClient, GmailMailSource
    from .ingestion import ingest_message
    from .models import Provenance
    from .parsers_tradersway import register_default_parsers

    # Register the deterministic broker parsers (idempotent by name+version). The registry is intentionally EMPTY at
    # import (DARK) and populated only by the armed worker — without this the pipeline selects no parser and every
    # message is quarantined as unparseable (no event ever produced). Per-mailbox call is safe (idempotent).
    register_default_parsers()

    if client is None:
        client_id, client_secret = _conf("GMAIL_OAUTH_CLIENT_ID"), _conf("GMAIL_OAUTH_CLIENT_SECRET")
        if not client_id or not client_secret:
            return {"mailbox_id": mailbox.id, "ok": False, "error": "oauth_not_configured", "ingested": 0}
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
    except Exception as exc:  # noqa: BLE001 — a mailbox read/parse failure must not advance the cursor or crash the run
        logger.warning("mailbox ingest failed for mailbox %s: %s", mailbox.id, type(exc).__name__)
        return {"mailbox_id": mailbox.id, "ok": False, "error": "fetch_failed", "ingested": ingested, "events": events}
    # Durable processing complete -> NOW advance the cursor (crash before here => safe re-fetch, idempotent).
    source.commit_cursor()
    return {"mailbox_id": mailbox.id, "ok": True, "ingested": ingested, "events": events}


def run_mailbox_ingest(*, limit_mailboxes: Optional[int] = None) -> dict:
    """Ingest all CONNECTED Gmail mailboxes. Each mailbox is isolated (one failure never aborts the others). NO flag
    check here — the caller (the DARK management command) gates the live run; this stays callable/testable."""
    from .models import ConnectedMailbox
    qs = (ConnectedMailbox.objects
          .filter(provider=ConnectedMailbox.Provider.GMAIL, status=ConnectedMailbox.Status.CONNECTED)
          .order_by("id"))
    if limit_mailboxes is not None:
        qs = qs[:limit_mailboxes]
    summary = {"mailboxes": 0, "ingested": 0, "events": 0, "failed": 0}
    for mailbox in qs:
        res = ingest_mailbox(mailbox)
        summary["mailboxes"] += 1
        summary["ingested"] += res.get("ingested", 0)
        summary["events"] += res.get("events", 0)
        if not res.get("ok"):
            summary["failed"] += 1
    return summary
