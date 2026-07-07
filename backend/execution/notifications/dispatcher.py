"""TELEGRAM-TRANSPORT-FOUNDATION — the transport dispatcher.

Consumes PENDING/FAILED ``NotificationCandidate`` rows behind a feature flag (default OFF),
runs the selected transport (``select_transport()`` — **dry-run by default**; the real transport
only when explicitly selected), and drives the ATOMIC status lifecycle
``PENDING → PROCESSING → SENT | FAILED``, appending a ``NotificationDelivery`` audit per attempt.

Idempotency: ``SENT`` and ``SUPPRESSED`` are ignored, and a candidate is never delivered twice.
The atomic ``PENDING/FAILED → PROCESSING`` claim lets exactly one runner proceed; and a real
transmission is recorded durably (``_persist_transmission``) BEFORE the candidate can be
re-claimed, so even a finalize failure cannot cause a duplicate real send (the transport's belt
skips any already-transmitted candidate). ``FAILED`` candidates are retried on a later run.

BOUNDARY: the dispatcher only updates ``NotificationCandidate.status`` and appends
``NotificationDelivery`` rows. It never places an order, never mutates a Trade or
TradeOutcomeRecord, and imports no execution business logic. It transmits ONLY when an operator
has explicitly enabled dispatch AND selected the real transport (both default OFF/dry-run).
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from execution.models import NotificationCandidate, NotificationDelivery
from execution.notifications.real_transport import select_transport
from execution.notifications.transport import NotificationTransport

logger = logging.getLogger("guvfx.execution.notifications")

DEFAULT_LIMIT = 500
# A PROCESSING candidate older than this was orphaned (crash/DB error between the claim and
# the finalize) — reclaim it to FAILED so it is retried. Env-overridable.
PROCESSING_TIMEOUT_SECONDS = int(os.getenv("NOTIFICATION_PROCESSING_TIMEOUT_SECONDS", "300") or 300)
_RETRYABLE = (NotificationCandidate.Status.PENDING, NotificationCandidate.Status.FAILED)


def dispatch_enabled() -> bool:
    """Feature flag (default OFF). Even when ON, the transport is dry-run UNLESS an operator also
    selects the real transport via ``NOTIFICATION_DISPATCH_TRANSPORT`` — both default to no-send."""
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


def _persist_transmission(candidate, transport_name, attempt, result) -> None:
    """Durably record a SUCCESSFUL transmission in its OWN committed transaction, BEFORE the
    candidate can be demoted or re-claimed.

    This is what keeps delivery idempotent across a finalize failure: the transport's belt skips
    any candidate that already has a ``transmitted=True`` delivery row, so a rolled-back finalize
    (which previously erased the only record of the send) can never cause a duplicate send.
    """
    with transaction.atomic():
        NotificationDelivery.objects.create(
            candidate=candidate, transport=transport_name,
            result=NotificationDelivery.Result.SENT, transmitted=True, attempt=attempt,
            correlation_id=candidate.correlation_id,
            rendered_message=result.rendered_message, detail=result.detail,
        )


def _finalize_result(cid, candidate, transport_name, attempt, result, ok, delivery_written) -> None:
    """Set the candidate's terminal status and (unless the transmission was already durably
    recorded) write its delivery audit row — atomically. Raises on DB failure (the caller repairs).
    """
    final = NotificationCandidate.Status.SENT if ok else NotificationCandidate.Status.FAILED
    with transaction.atomic():
        NotificationCandidate.objects.filter(id=cid).update(status=final, updated_at=timezone.now())
        if not delivery_written:
            NotificationDelivery.objects.create(
                candidate=candidate, transport=transport_name,
                result=(NotificationDelivery.Result.SENT if ok
                        else NotificationDelivery.Result.FAILED),
                transmitted=(result.transmitted if result is not None else False),
                attempt=attempt, correlation_id=candidate.correlation_id,
                rendered_message=(result.rendered_message if result is not None else ""),
                detail=(result.detail if result is not None else "transport raised"),
            )


def dispatch_pending(*, transport: NotificationTransport = None, limit: int = DEFAULT_LIMIT) -> dict:
    """Dispatch up to ``limit`` PENDING/FAILED candidates. Returns a counts dict.

    No-op (nothing claimed) when the feature flag is off. Creates status updates +
    NotificationDelivery audit rows and no order. It transmits a real Telegram message ONLY when
    dispatch is enabled AND the real transport is selected (default: dry-run, transmits nothing).
    """
    counts = {"enabled": dispatch_enabled(), "claimed": 0, "sent": 0, "failed": 0,
              "skipped": 0, "reaped": 0}
    if not counts["enabled"]:
        return counts
    # DEFAULT = dry-run (select_transport only returns the real transport when explicitly selected
    # via NOTIFICATION_DISPATCH_TRANSPORT). An explicit transport arg still overrides both.
    transport = transport or select_transport()

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
        transmitted = bool(result is not None and result.transmitted)

        # DURABILITY FIRST — a real transmission is recorded in its own committed transaction
        # BEFORE the candidate can be demoted/re-claimed, so a later finalize failure can never
        # cause a duplicate send. Non-transmitted results (dry-run / failures) are recorded by the
        # finalize below.
        delivery_written = False
        if transmitted:
            try:
                _persist_transmission(candidate, transport.name, attempt, result)
                delivery_written = True
            except Exception:  # narrow residual window: DB down on a single-row insert
                logger.exception("dispatch: post-transmit audit write failed cand=%s", cid)

        try:
            _finalize_result(cid, candidate, transport.name, attempt, result, ok, delivery_written)
        except Exception:
            # Finalize failed (e.g. DB error). A TRANSMITTED candidate must NEVER be re-sent — its
            # durable delivery row + the transport belt already block a re-send, so force it to SENT
            # to leave the retry set. A non-transmitted attempt is demoted to FAILED to retry.
            logger.exception("dispatch: finalize failed cand=%s", cid)
            repair = (NotificationCandidate.Status.SENT if transmitted
                      else NotificationCandidate.Status.FAILED)
            try:
                NotificationCandidate.objects.filter(
                    id=cid, status=NotificationCandidate.Status.PROCESSING,
                ).update(status=repair, updated_at=timezone.now())
            except Exception:  # DB still unavailable — the reaper reclaims; the belt blocks re-send
                logger.exception("dispatch: demote failed cand=%s (reaper will reclaim)", cid)
            counts["sent" if transmitted else "failed"] += 1
            continue

        counts["claimed"] += 1
        counts["sent" if ok else "failed"] += 1

    return counts
