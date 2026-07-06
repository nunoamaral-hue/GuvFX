"""TELEGRAM-TRANSPORT-FOUNDATION — the transport dispatcher.

Consumes PENDING/FAILED ``NotificationCandidate`` rows behind a feature flag (default OFF),
runs the (dry-run) transport, and drives the ATOMIC status lifecycle
``PENDING → PROCESSING → SENT | FAILED``, appending a ``NotificationDelivery`` audit per attempt.

Idempotency: ``SENT`` and ``SUPPRESSED`` are ignored, and a candidate is never delivered twice —
the atomic ``PENDING/FAILED → PROCESSING`` claim (a conditional UPDATE) lets exactly one runner
proceed. ``FAILED`` candidates are retried on a later run (a new audit row per attempt).

BOUNDARY: the dispatcher only updates ``NotificationCandidate.status`` and appends
``NotificationDelivery`` rows. It never places an order, never mutates a Trade or
TradeOutcomeRecord, never transmits, and imports no execution business logic.
"""
from __future__ import annotations

import logging
import os

from django.db import transaction

from execution.models import NotificationCandidate, NotificationDelivery
from execution.notifications.transport import NotificationTransport, TelegramDryRunTransport

logger = logging.getLogger("guvfx.execution.notifications")

DEFAULT_LIMIT = 500
_RETRYABLE = (NotificationCandidate.Status.PENDING, NotificationCandidate.Status.FAILED)


def dispatch_enabled() -> bool:
    """Feature flag (default OFF). Even when ON, the only transport is dry-run — nothing sends."""
    return os.getenv("NOTIFICATION_DISPATCH_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def dispatch_pending(*, transport: NotificationTransport = None, limit: int = DEFAULT_LIMIT) -> dict:
    """Dispatch up to ``limit`` PENDING/FAILED candidates. Returns a counts dict.

    No-op (nothing claimed) when the feature flag is off. Creates only status updates +
    NotificationDelivery audit rows — no order, no Telegram transmission.
    """
    counts = {"enabled": dispatch_enabled(), "claimed": 0, "sent": 0, "failed": 0, "skipped": 0}
    if not counts["enabled"]:
        return counts
    transport = transport or TelegramDryRunTransport()

    ids = list(
        NotificationCandidate.objects.filter(status__in=_RETRYABLE)
        .order_by("created_at", "id").values_list("id", flat=True)[:limit]
    )
    for cid in ids:
        # ATOMIC claim — only one runner can move PENDING/FAILED → PROCESSING.
        claimed = NotificationCandidate.objects.filter(
            id=cid, status__in=_RETRYABLE
        ).update(status=NotificationCandidate.Status.PROCESSING)
        if not claimed:
            counts["skipped"] += 1
            continue

        candidate = NotificationCandidate.objects.get(id=cid)
        attempt = candidate.deliveries.count() + 1
        try:
            result = transport.deliver(candidate)
        except Exception as exc:  # a transport error → FAILED (retryable next run)
            logger.info("dispatch: transport error cand=%s (%s)", cid, exc)
            result = None

        ok = result is not None and result.ok
        final = NotificationCandidate.Status.SENT if ok else NotificationCandidate.Status.FAILED
        with transaction.atomic():
            NotificationCandidate.objects.filter(id=cid).update(status=final)
            NotificationDelivery.objects.create(
                candidate=candidate,
                transport=transport.name,
                result=(NotificationDelivery.Result.SENT if ok
                        else NotificationDelivery.Result.FAILED),
                transmitted=(result.transmitted if result is not None else False),
                attempt=attempt,
                correlation_id=candidate.correlation_id,
                rendered_message=(result.rendered_message if result is not None else ""),
                detail=(result.detail if result is not None else "transport raised"),
            )
        counts["claimed"] += 1
        counts["sent" if ok else "failed"] += 1

    return counts
