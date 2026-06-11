"""Centralised upsert of ComponentHealth with debounce + transition tracking."""
from django.utils import timezone

from ..constants import HealthStatus, DEBOUNCE_FAILURES
from ..models import ComponentHealth


def upsert(component, status, *, detail=None, terminal_node=None, mt5_instance=None, trading_account=None):
    """Upsert one (component, scope) health row.

    Returns (row, transitioned, previous_status). Applies debounce: a non-OK
    status only becomes the recorded status after DEBOUNCE_FAILURES consecutive
    non-OK observations; OK is recorded immediately (fast recovery).
    """
    now = timezone.now()
    row, _ = ComponentHealth.objects.get_or_create(
        component=component, terminal_node=terminal_node, trading_account=trading_account,
        defaults={"mt5_instance": mt5_instance, "status": HealthStatus.UNKNOWN},
    )
    prev = row.status
    detail = detail or {}

    if status == HealthStatus.OK:
        row.consecutive_failures = 0
        row.last_ok_at = now
        effective = HealthStatus.OK
    else:
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        # Debounce: keep prior status until threshold reached (avoid flapping).
        if row.consecutive_failures >= DEBOUNCE_FAILURES:
            effective = status
        else:
            effective = prev if prev != HealthStatus.UNKNOWN else status

    transitioned = effective != prev
    if transitioned:
        row.since = now
    if mt5_instance is not None:
        row.mt5_instance = mt5_instance
    row.status = effective
    row.detail = detail
    row.save()
    return row, transitioned, prev
