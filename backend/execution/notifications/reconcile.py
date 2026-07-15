"""WS-C NOTIFICATION RECONCILIATION — the exactly-once safety net.

The dispatch pipeline (close_monitor → outcome_router → dispatch_pending) is already idempotent for
the common path: one candidate per WIN (OneToOne), an atomic PENDING/FAILED→PROCESSING claim, a
stuck-PROCESSING reaper, and FAILED retries. What it lacked was a *reconciler* that closes the
residual LOSS gaps a per-attempt dispatcher cannot see on its own:

  (A) mark-delivered — a WIN with a ``transmitted=True`` delivery is stamped ``delivered=True`` on its
      outcome record (an indexed, fast "this WIN really went out" flag; the field was previously dead).
  (B) backfill — a WIN delivery-candidate that somehow has NO ``NotificationCandidate`` gets one
      (PENDING), mirroring outcome_router, so a create-gap can never strand a winner.
  (C) revive — a candidate marked terminal ``SENT`` but with NO ``transmitted=True`` delivery (the
      dry-run "SENT" trap, or a lost send) is reset to PENDING so dispatch re-drives it — but ONLY
      when the REAL transport is active (else it would just re-"SENT" in dry-run forever). Safe
      against duplicates by construction: such a candidate was, by definition, never transmitted.
  (D) alert — a WIN still undelivered beyond a threshold raises ONE deduped WARN so a silently stuck
      winner (e.g. a persistent render error) is surfaced to operators instead of lost.

HARD BOUNDARY: internal records + one alert only. Never sends Telegram, never places an order, never
mutates a Trade. Idempotent and safe to run every minute. Runs as a monitor-chain step BETWEEN
``route_outcomes`` and ``dispatch_pending`` so anything it backfills/revives is dispatched the same tick.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from django.db import IntegrityError
from django.utils import timezone

from execution.models import NotificationCandidate, NotificationDelivery, TradeOutcomeRecord

logger = logging.getLogger("guvfx.execution.notifications.reconcile")

DEFAULT_LIMIT = 500
# A WIN still undelivered after this long is an incident, not a lag — alert once. Env-overridable.
UNDELIVERED_ALERT_SECONDS = int(os.getenv("NOTIFICATION_UNDELIVERED_ALERT_SECONDS", "1200") or 1200)

_WIN = TradeOutcomeRecord.Outcome.WIN
_SENT = NotificationCandidate.Status.SENT
_PENDING = NotificationCandidate.Status.PENDING


def _real_transport_active() -> bool:
    """True only when dispatch is enabled AND the REAL Telegram transport is selected — the exact
    condition under which reviving a candidate leads to a real send (not a dry-run re-'SENT')."""
    try:
        from execution.notifications.dispatcher import dispatch_enabled
        from execution.notifications.real_transport import _REAL_CHOICES
    except Exception:  # pragma: no cover - defensive import guard
        return False
    choice = os.getenv("NOTIFICATION_DISPATCH_TRANSPORT", "").strip().lower()
    return dispatch_enabled() and choice in _REAL_CHOICES


def _alert_undelivered(rec, now) -> None:
    """One deduped WARN per stuck winner. Best-effort — never breaks the reconcile pass."""
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup_key = f"notify_undelivered:outcome:{rec.id}"
        if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
            return
        age_min = int((now - rec.created_at).total_seconds() // 60) if rec.created_at else None
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.WARN,
            component=Component.EXECUTION_PIPELINE,
            title=f"WIN not notified — outcome #{rec.id} ({rec.signal_source or 'unknown'})",
            body=(f"A winning trade (outcome #{rec.id}, net_pnl {rec.net_pnl}) has had no transmitted "
                  f"notification card for {age_min} min. The dispatch retry/reaper should recover it; "
                  f"a persistent failure here means a winner was not announced."),
            dedup_key=dedup_key,
            status=AlertEvent.Status.OPEN,
            detail={"outcome_id": rec.id, "net_pnl": str(rec.net_pnl),
                    "signal_source": rec.signal_source, "age_min": age_min},
        )
        logger.warning("reconcile: undelivered-WIN alert raised for outcome %s", rec.id)
    except Exception:  # pragma: no cover - alerting is best-effort
        logger.exception("reconcile: failed to alert undelivered outcome %s", rec.id)


def reconcile_notifications(*, limit: int = DEFAULT_LIMIT) -> dict:
    """One idempotent reconciliation pass. Returns a counts dict."""
    counts = {"marked_delivered": 0, "backfilled": 0, "revived": 0, "alerted": 0}
    now = timezone.now()

    # (A) Stamp delivered=True on WINs that truly transmitted (fast, indexed liveness flag).
    to_mark = list(
        TradeOutcomeRecord.objects.filter(
            outcome=_WIN, is_delivery_candidate=True, delivered=False,
            notification_candidate__deliveries__transmitted=True,
        ).distinct().values_list("id", flat=True)[:limit]
    )
    if to_mark:
        counts["marked_delivered"] = TradeOutcomeRecord.objects.filter(id__in=to_mark).update(delivered=True)

    # (B) Backfill a missing candidate for any delivery-candidate WIN (mirrors outcome_router).
    orphans = TradeOutcomeRecord.objects.filter(
        outcome=_WIN, is_delivery_candidate=True, notification_candidate__isnull=True,
    ).order_by("id")[:limit]
    for rec in orphans:
        try:
            _, created = NotificationCandidate.objects.get_or_create(
                outcome_record=rec,
                defaults={"correlation_id": rec.correlation_id,
                          "signal_source": rec.signal_source, "net_pnl": rec.net_pnl},
            )
            if created:
                counts["backfilled"] += 1
        except IntegrityError:
            continue  # a concurrent run created it — idempotent

    # (C) Revive SENT-but-never-transmitted candidates — ONLY under the real transport.
    if _real_transport_active():
        stale = list(
            NotificationCandidate.objects.filter(status=_SENT)
            .exclude(deliveries__transmitted=True)
            .order_by("id").values_list("id", flat=True)[:limit]
        )
        for cid in stale:
            counts["revived"] += NotificationCandidate.objects.filter(
                id=cid, status=_SENT
            ).update(status=_PENDING, updated_at=now)
            if counts["revived"]:
                logger.warning("reconcile: revived SENT-but-untransmitted candidate %s", cid)

    # (D) Alert on winners still undelivered beyond the threshold (tripwire for silent loss).
    cutoff = now - timedelta(seconds=UNDELIVERED_ALERT_SECONDS)
    undelivered = TradeOutcomeRecord.objects.filter(
        outcome=_WIN, is_delivery_candidate=True, delivered=False, created_at__lt=cutoff,
    ).order_by("id")[:limit]
    for rec in undelivered:
        # Belt: skip if a transmitted delivery exists but delivered flag simply lagged this pass.
        if NotificationDelivery.objects.filter(
            candidate__outcome_record=rec, transmitted=True
        ).exists():
            continue
        _alert_undelivered(rec, now)
        counts["alerted"] += 1

    return counts
