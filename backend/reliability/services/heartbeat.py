"""RX-2C — Strategy/Worker Heartbeat Framework.

record_beat() is called by schedulers + workers (producers). evaluate()
reads Heartbeat rows and sets ComponentHealth for each source (consumer).
Fixes the proven inert-heartbeat gap (last_heartbeat was always None).
"""
from django.utils import timezone

from ..constants import (
    Component, HealthStatus, HEARTBEAT_EXPECTED_INTERVAL, HEARTBEAT_GRACE_MULTIPLIER,
)
from ..models import Heartbeat
from . import health_store

# Map heartbeat source key -> ComponentHealth component.
SOURCE_TO_COMPONENT = {
    "scheduler_h1": Component.SCHEDULER_H1,
    "scheduler_h4": Component.SCHEDULER_H4,
    "scheduler_m5": Component.SCHEDULER_M5,
    "ingest_worker": Component.INGEST_WORKER,
    "validate_worker": Component.VALIDATE_WORKER,
}


def record_beat(source: str, *, interval_s: int = 60, detail=None):
    """Producer side — upsert a Heartbeat. Safe to call frequently."""
    now = timezone.now()
    Heartbeat.objects.update_or_create(
        source=source,
        defaults={"last_beat_at": now, "expected_interval_s": interval_s, "detail": detail or {}},
    )


def evaluate():
    """Consumer side — set ComponentHealth per known source based on staleness."""
    now = timezone.now()
    beats = {h.source: h for h in Heartbeat.objects.all()}
    for source, component in SOURCE_TO_COMPONENT.items():
        hb = beats.get(source)
        expected = HEARTBEAT_EXPECTED_INTERVAL.get(component, 60)
        if hb is None:
            health_store.upsert(component, HealthStatus.UNKNOWN, detail={"reason": "no_heartbeat_recorded_yet"})
            continue
        age = (now - hb.last_beat_at).total_seconds()
        threshold = (hb.expected_interval_s or expected) * HEARTBEAT_GRACE_MULTIPLIER
        status = HealthStatus.OK if age <= threshold else HealthStatus.FAILED
        health_store.upsert(component, status, detail={"age_s": round(age, 1), "threshold_s": threshold})
