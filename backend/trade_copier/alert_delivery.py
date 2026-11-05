"""trade_copier.alert_delivery — governed outbound alert-DELIVERY boundary for copier alerts (ADR-0074 Phase-9).

There is no reusable governed ``reliability.AlertEvent`` delivery mechanism (the only inline push covers
ComponentHealth-derived alerts), so this is a NEW, additive, default-OFF delivery outbox + dispatcher +
pluggable transport, mirroring the audited ``execution.notifications`` pattern (outbox row + per-attempt audit +
atomic claim + bounded retry + dry-run-by-default transport):

* **Enqueue** (``enqueue_delivery``) — called from ``failure_alerts.raise_copier_alert`` when (and only when)
  ``copier_alerts_enabled`` is on and a NEW alert row was created. Idempotent via the unique ``dedup_key``
  (= the alert's dedup_key), so a re-raise never double-enqueues. The payload is sanitised (ids + reason codes).
* **Dispatch** (``dispatch_pending``) — gated SEPARATELY by ``copier_alert_dispatch_enabled`` (default OFF).
  Atomically claims PENDING/FAILED rows, sends through an INJECTED transport that DEFAULTS to an inert dry-run
  with NO real destination, writes an immutable per-attempt audit, and advances the durable state + attempt
  counter (FAILED retried until ``max_attempts`` → terminal DEAD). When the dispatch flag is OFF the rows sit
  PENDING — an operator-visible backlog surfaced on the operations summary.

Nothing here recovers a trade, creates an ExecutionJob, or contacts a broker. No real destination is
configured; a real transport is a conscious, separate operator step.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryOutcome:
    """Result of one transport attempt. ``transmitted`` True ⇒ actually delivered to a real destination."""
    transmitted: bool
    retryable: bool = True
    detail: str = ""
    transport: str = ""


class DryRunAlertTransport:
    """The DEFAULT transport: delivers NOWHERE (no real destination). It never transmits, so an armed dispatcher
    with no configured destination produces visibly-unsuccessful deliveries (operator signal), never a silent
    'sent'. It is retryable so the posture is 'awaiting a real destination', not a hard dead-letter on attempt 1
    (it will still DEAD after max_attempts, which is itself the signal that dispatch was armed with no sink)."""
    name = "dry_run"

    def deliver(self, delivery) -> DeliveryOutcome:   # noqa: D401
        return DeliveryOutcome(transmitted=False, retryable=True, detail="dry_run_no_destination",
                               transport=self.name)


def select_alert_transport():
    """Transport factory. DEFAULTS to the inert dry-run transport. No real destination is wired in this packet;
    a real transport is a conscious, separate operator configuration step (``COPIER_ALERT_TRANSPORT``)."""
    choice = str(getattr(settings, "COPIER_ALERT_TRANSPORT", "") or "").strip().lower()
    # Only 'dry_run' (default) is recognised here — deliberately NO real destination in this DARK packet.
    if choice in ("", "dry_run", "null", "none"):
        return DryRunAlertTransport()
    logger.warning("copier alert transport %r not recognised — falling back to inert dry-run", choice)
    return DryRunAlertTransport()


def enqueue_delivery(alert, *, category: str, severity: str, payload: dict) -> bool:
    """Enqueue ONE durable outbox row for a raised copier alert (idempotent via dedup_key). Returns True iff a
    new row was created. Caller guarantees ``copier_alerts_enabled`` is on. Fail-open (never raises)."""
    try:
        from trade_copier.models import CopierAlertDelivery
        dedup_key = str(getattr(alert, "dedup_key", "") or "")[:200]
        if not dedup_key:
            return False
        _row, created = CopierAlertDelivery.objects.get_or_create(
            dedup_key=dedup_key,
            defaults={
                "alert": alert, "category": (category or "")[:32], "severity": (severity or "")[:16],
                "status": CopierAlertDelivery.Status.PENDING,
                "payload": _sanitise(payload),
            })
        return bool(created)
    except Exception:  # noqa: BLE001 — enqueue is fail-open; alerting must never break a projection/ingest
        logger.exception("copier alert delivery enqueue failed (category=%s)", category)
        return False


def _sanitise(payload: dict) -> dict:
    """Keep ids + reason codes only — never a secret, login or raw body."""
    out = {}
    for k in ("category", "reason", "ticket", "lifecycle_id", "retryable", "correlation_id", "account_id"):
        if isinstance(payload, dict) and k in payload:
            out[k] = payload[k]
    return out


def _claim(delivery_id: int, *, now):
    """Atomically claim a PENDING/FAILED row → PROCESSING (skip if already taken). Returns the locked row or None."""
    from trade_copier.models import CopierAlertDelivery
    with transaction.atomic():
        row = (CopierAlertDelivery.objects.select_for_update(skip_locked=True)
               .filter(id=delivery_id,
                       status__in=[CopierAlertDelivery.Status.PENDING, CopierAlertDelivery.Status.FAILED])
               .first())
        if row is None:
            return None
        row.status = CopierAlertDelivery.Status.PROCESSING
        row.claimed_at = now
        row.attempts = int(row.attempts) + 1
        row.save(update_fields=["status", "claimed_at", "attempts", "updated_at"])
        return row


def _finalise(delivery_id: int, outcome: DeliveryOutcome, *, now) -> str:
    """Persist the attempt audit + the next durable state. Returns the resulting status."""
    from trade_copier.models import CopierAlertDelivery, CopierAlertDeliveryAttempt
    with transaction.atomic():
        row = CopierAlertDelivery.objects.select_for_update().get(id=delivery_id)
        CopierAlertDeliveryAttempt.objects.create(
            delivery=row, attempt=int(row.attempts), transport=(outcome.transport or "")[:32],
            transmitted=bool(outcome.transmitted), detail=(outcome.detail or "")[:128])
        if outcome.transmitted:
            row.status = CopierAlertDelivery.Status.SENT
            row.delivered_at = now
            row.last_error = ""
        elif outcome.retryable and int(row.attempts) < int(row.max_attempts):
            row.status = CopierAlertDelivery.Status.FAILED
            row.last_error = (outcome.detail or "")[:128]
        else:
            row.status = CopierAlertDelivery.Status.DEAD
            row.last_error = (outcome.detail or "retries_exhausted")[:128]
        row.save(update_fields=["status", "delivered_at", "last_error", "updated_at"])
        return row.status


STUCK_PROCESSING_SECONDS = 300   # a PROCESSING row older than this is presumed crashed mid-send → re-queue


def _reap_stuck_processing(*, now) -> int:
    """Return orphaned PROCESSING rows (process died between claim and finalise) to FAILED so they retry — never
    a silent delivery loss. Bounded by the attempt counter (a reaped row still DEADs after max_attempts)."""
    from datetime import timedelta
    from trade_copier.models import CopierAlertDelivery
    cutoff = now - timedelta(seconds=STUCK_PROCESSING_SECONDS)
    return (CopierAlertDelivery.objects
            .filter(status=CopierAlertDelivery.Status.PROCESSING, claimed_at__lt=cutoff)
            .update(status=CopierAlertDelivery.Status.FAILED, last_error="reaped_stuck_processing"))


def dispatch_pending(*, limit: int = 200, transport=None, now=None) -> dict:
    """One gated outbound-delivery pass. Inert unless ``COPIER_ENABLED`` AND ``COPIER_ALERT_DISPATCH_ENABLED``.

    Claims PENDING/FAILED rows, sends through ``transport`` (default: inert dry-run — no real destination),
    records a per-attempt audit, and advances durable state. Creates NO order/job; contacts NO broker. Returns
    a secret-free summary."""
    from trade_copier.flags import copier_alert_dispatch_enabled
    if not copier_alert_dispatch_enabled():
        return {"enabled": False, "sent": 0, "failed": 0, "dead": 0, "considered": 0}

    from trade_copier.models import CopierAlertDelivery
    transport = transport or select_alert_transport()
    now = now or timezone.now()
    _reap_stuck_processing(now=now)
    ids = list(CopierAlertDelivery.objects
               .filter(status__in=[CopierAlertDelivery.Status.PENDING, CopierAlertDelivery.Status.FAILED])
               .order_by("id").values_list("id", flat=True)[:limit])
    sent = failed = dead = considered = 0
    for did in ids:
        row = _claim(did, now=now)   # claim (atomic) — released before the send (never hold a lock across send)
        if row is None:
            continue
        considered += 1
        try:
            outcome = transport.deliver(row)
            if not isinstance(outcome, DeliveryOutcome):
                outcome = DeliveryOutcome(transmitted=bool(outcome), transport=getattr(transport, "name", ""))
        except Exception as e:  # noqa: BLE001 — a transport fault is a retryable failure, never a crash
            outcome = DeliveryOutcome(transmitted=False, retryable=True, detail=type(e).__name__,
                                      transport=getattr(transport, "name", ""))
        status = _finalise(did, outcome, now=now)
        if status == CopierAlertDelivery.Status.SENT:
            sent += 1
        elif status == CopierAlertDelivery.Status.DEAD:
            dead += 1
        else:
            failed += 1
    return {"enabled": True, "sent": sent, "failed": failed, "dead": dead, "considered": considered}


def delivery_backlog() -> dict:
    """Read-only delivery-state counts for the operations summary (operator-visible status). Never a secret."""
    try:
        from trade_copier.models import CopierAlertDelivery
        from django.db.models import Count
        counts = {r["status"]: r["n"] for r in
                  CopierAlertDelivery.objects.values("status").annotate(n=Count("id"))}
        return {
            "pending": int(counts.get(CopierAlertDelivery.Status.PENDING, 0)),
            "failed": int(counts.get(CopierAlertDelivery.Status.FAILED, 0)),
            "dead": int(counts.get(CopierAlertDelivery.Status.DEAD, 0)),
            "sent": int(counts.get(CopierAlertDelivery.Status.SENT, 0)),
        }
    except Exception:  # noqa: BLE001
        return {"pending": 0, "failed": 0, "dead": 0, "sent": 0}
