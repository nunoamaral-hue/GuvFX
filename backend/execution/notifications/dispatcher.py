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
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from execution.models import NotificationCandidate, NotificationDelivery
from execution.notifications.transport import NotificationTransport, TelegramDryRunTransport

logger = logging.getLogger("guvfx.execution.notifications")

DEFAULT_LIMIT = 500
# A PROCESSING candidate older than this was orphaned (crash/DB error between the claim and
# the finalize) — reclaim it to FAILED so it is retried. Env-overridable.
PROCESSING_TIMEOUT_SECONDS = int(os.getenv("NOTIFICATION_PROCESSING_TIMEOUT_SECONDS", "300") or 300)
_RETRYABLE = (NotificationCandidate.Status.PENDING, NotificationCandidate.Status.FAILED)


def dispatch_enabled() -> bool:
    """Feature flag (default OFF). Even when ON, the only transport is dry-run — nothing sends."""
    return os.getenv("NOTIFICATION_DISPATCH_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _reap_stuck_processing(now) -> int:
    """Reclaim orphaned PROCESSING rows (older than the timeout) back to FAILED (retryable).

    Fail-closed: a reclaimed row is simply retried on this run; it never over-delivers, never
    transmits, and never touches SENT/SUPPRESSED. Concurrency-safe via the age filter.
    """
    cutoff = now - timedelta(seconds=PROCESSING_TIMEOUT_SECONDS)
    return NotificationCandidate.objects.filter(
        status=NotificationCandidate.Status.PROCESSING, updated_at__lt=cutoff,
    ).update(status=NotificationCandidate.Status.FAILED, updated_at=now)


def dispatch_pending(*, transport: NotificationTransport = None, limit: int = DEFAULT_LIMIT) -> dict:
    """Dispatch up to ``limit`` PENDING/FAILED candidates. Returns a counts dict.

    No-op (nothing claimed) when the feature flag is off. Creates only status updates +
    NotificationDelivery audit rows — no order, no Telegram transmission.
    """
    counts = {"enabled": dispatch_enabled(), "claimed": 0, "sent": 0, "failed": 0,
              "skipped": 0, "reaped": 0}
    if not counts["enabled"]:
        return counts
    transport = transport or TelegramDryRunTransport()

    counts["reaped"] = _reap_stuck_processing(timezone.now())

    ids = list(
        NotificationCandidate.objects.filter(status__in=_RETRYABLE)
        .order_by("created_at", "id").values_list("id", flat=True)[:limit]
    )
    for cid in ids:
        now = timezone.now()
        # ATOMIC claim — only one runner can move PENDING/FAILED → PROCESSING.
        claimed = NotificationCandidate.objects.filter(
            id=cid, status__in=_RETRYABLE
        ).update(status=NotificationCandidate.Status.PROCESSING, updated_at=now)
        if not claimed:
            counts["skipped"] += 1
            continue

        candidate = NotificationCandidate.objects.get(id=cid)
        attempt = candidate.deliveries.count() + 1
        try:
            result = transport.deliver(candidate)
        except Exception:  # a transport error → FAILED (retryable next run)
            logger.exception("dispatch: transport error cand=%s", cid)
            result = None

        ok = result is not None and result.ok
        final = NotificationCandidate.Status.SENT if ok else NotificationCandidate.Status.FAILED
        try:
            with transaction.atomic():
                NotificationCandidate.objects.filter(id=cid).update(
                    status=final, updated_at=timezone.now(),
                )
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
        except Exception:
            # Finalize failed (e.g. DB error): demote PROCESSING → FAILED so it is retried
            # instead of orphaned, and continue the batch (never abort the whole run).
            logger.exception("dispatch: finalize failed cand=%s", cid)
            try:
                NotificationCandidate.objects.filter(
                    id=cid, status=NotificationCandidate.Status.PROCESSING,
                ).update(status=NotificationCandidate.Status.FAILED, updated_at=timezone.now())
            except Exception:  # DB still unavailable — the timeout reaper reclaims it later
                logger.exception("dispatch: demote failed cand=%s (reaper will reclaim)", cid)
            counts["failed"] += 1
            continue

        counts["claimed"] += 1
        counts["sent" if ok else "failed"] += 1

    return counts
