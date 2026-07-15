"""WS-G — soak-test evidence collection (passive, VPS-side, durable).

``build_soak_snapshot(window_hours)`` aggregates the production reliability picture over a window,
BY SOURCE (never combined), from data the platform already records — no new hot-path instrumentation.
It is read-only; ``persist=True`` writes a durable ``SoakSnapshot`` row. Driven by a cron (see
``deploy/soak-report/``), so the soak evidence accrues with no developer/Claude process running.

Covered per source: signals received / rejected (+reasons) / plans / promoted / orders / fills /
closes / TP outcomes (WIN/LOSS/BE) / breakeven modifications / provider commands / realised PnL /
cards delivered / retries / alerts — plus global component uptime.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db.models import Count
from django.utils import timezone

from intelligence.display_labels import source_display_label


def _sources():
    from execution.models import SignalSourceConfig
    from strategies.models import StrategyAssignment
    armed = {a.signal_source for a in StrategyAssignment.objects.filter(is_active=True) if a.signal_source}
    cfg = set(SignalSourceConfig.objects.values_list("source", flat=True))
    return sorted(armed | cfg | {"wayond", "ti_signals"})


def _provider_command_counts(src, since):
    """Defensive: ProviderCommand (WS-E) may not exist yet when this ships ahead of E."""
    try:
        from signal_intake.models import ProviderCommand, SignalProvider
    except Exception:
        return {}
    prov = SignalProvider.objects.filter(slug=src).first()
    if prov is None:
        return {}
    qs = ProviderCommand.objects.filter(provider=prov, created_at__gte=since)
    return dict(qs.values_list("status").annotate(n=Count("id")).values_list("status", "n"))


def _source_soak(src, since):
    from execution.models import (
        ExecutionJob, ProposedOrderLeg, SignalExecutionPlan, TradeOutcomeRecord,
        NotificationDelivery,
    )
    plans = SignalExecutionPlan.objects.filter(source=src, created_at__gte=since)
    rejected = plans.filter(status__in=("VOIDED", "HELD"))
    outcomes = TradeOutcomeRecord.objects.filter(signal_source=src, created_at__gte=since)
    # breakeven + close jobs attributed to the source via the job payload's signal_source
    be_jobs = ExecutionJob.objects.filter(
        job_type="MODIFY_POSITION", created_at__gte=since, payload__signal_source=src)
    close_jobs = ExecutionJob.objects.filter(
        job_type="CLOSE_TRADE", created_at__gte=since, payload__signal_source=src)
    order_jobs = ExecutionJob.objects.filter(
        job_type="PLACE_ORDER", created_at__gte=since, payload__signal_source=src)
    deliveries = NotificationDelivery.objects.filter(
        candidate__signal_source=src, created_at__gte=since)
    return {
        "source": src,
        "source_label": source_display_label(src),
        "signals_received": plans.count(),
        "rejected": rejected.count(),
        "rejection_reasons": dict(rejected.exclude(hold_reason="")
                                  .values_list("hold_reason").annotate(n=Count("id"))
                                  .values_list("hold_reason", "n")),
        "plans_promoted": plans.filter(status__in=("PROMOTED", "CLOSED")).count(),
        "orders_placed": order_jobs.count(),
        "orders_filled": order_jobs.filter(status="SUCCESS").count(),
        "trades_closed": outcomes.count(),
        "wins": outcomes.filter(outcome="WIN").count(),
        "losses": outcomes.filter(outcome="LOSS").count(),
        "breakevens": outcomes.filter(outcome="BREAKEVEN").count(),
        "realised_pnl": str(sum((o.net_pnl for o in outcomes), Decimal("0"))),
        "breakeven_modifications": be_jobs.count(),
        "breakeven_verified": be_jobs.filter(status="SUCCESS").count(),
        "provider_close_jobs": close_jobs.count(),
        "provider_commands": _provider_command_counts(src, since),
        "cards_delivered": deliveries.filter(transmitted=True).count(),
        "delivery_retries": max(0, deliveries.count() - deliveries.filter(transmitted=True).count()),
    }


def build_soak_snapshot(*, window_hours: int = 24, persist: bool = False) -> dict:
    from reliability.models import AlertEvent, Heartbeat, SoakSnapshot
    now = timezone.now()
    since = now - timedelta(hours=window_hours)

    alerts_window = AlertEvent.objects.filter(created_at__gte=since)
    snapshot = {
        "generated_at": now.isoformat(),
        "window_hours": window_hours,
        "since": since.isoformat(),
        "by_source": [_source_soak(s, since) for s in _sources()],
        "alerts": {
            "opened": alerts_window.count(),
            "critical": alerts_window.filter(severity="CRITICAL").count(),
            "open_now": AlertEvent.objects.filter(status="OPEN").count(),
        },
        "heartbeats": [
            {"source": h.source,
             "age_s": None if h.last_beat_at is None else round((now - h.last_beat_at).total_seconds()),
             "interval_s": h.expected_interval_s}
            for h in Heartbeat.objects.all().order_by("source")
        ],
    }
    if persist:
        SoakSnapshot.objects.create(window_hours=window_hours, data=snapshot)
    return snapshot
