"""RX-2D — Telemetry Freshness Monitoring (read-only ages).

Snapshot freshness is evaluated from the live bridge tick in mt5_supervision
(SNAPSHOT_FEED). This module provides DB-side data-flow ages (latest trade,
latest execution job) used as supporting detail on EXECUTION_PIPELINE health.
"""
from django.utils import timezone


def latest_ages():
    now = timezone.now()
    out = {}
    try:
        from trading.models import Trade
        t = Trade.objects.order_by("-id").first()
        if t is not None:
            ref = getattr(t, "close_time", None) or getattr(t, "open_time", None)
            out["latest_trade_age_s"] = round((now - ref).total_seconds(), 1) if ref else None
        else:
            out["latest_trade_age_s"] = None
    except Exception as e:  # noqa: BLE001
        out["latest_trade_error"] = type(e).__name__
    try:
        from execution.models import ExecutionJob
        j = ExecutionJob.objects.order_by("-id").first()
        out["latest_job_age_s"] = round((now - j.created_at).total_seconds(), 1) if j else None
    except Exception as e:  # noqa: BLE001
        out["latest_job_error"] = type(e).__name__
    return out
