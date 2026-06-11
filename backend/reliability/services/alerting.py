"""Alert lifecycle + advisory recommendations.

- Opens an AlertEvent when a component is unhealthy; auto-RESOLVES when it
  returns OK. ACKNOWLEDGED is set only via the acknowledge API.
- Generates advisory RecoveryRecommendation rows. NEVER executes recovery.
- Optional delivery to an internal webhook (no external monitoring platform).
"""
import json
import os
import urllib.request

from django.utils import timezone

from ..constants import Component, HealthStatus, CRITICAL_COMPONENTS
from ..models import AlertEvent, ComponentHealth, RecoveryRecommendation

# Component → recommended action mapping (advisory only).
_ACTION_FOR = {
    Component.MT5_BROKER: RecoveryRecommendation.Action.MT5_RELOGIN,
    Component.MT5_TERMINAL: RecoveryRecommendation.Action.RESTART_BRIDGE,
    Component.SNAPSHOT_FEED: RecoveryRecommendation.Action.INVESTIGATE_SNAPSHOT,
    Component.INGEST_WORKER: RecoveryRecommendation.Action.RESTART_WORKER,
    Component.VALIDATE_WORKER: RecoveryRecommendation.Action.RESTART_WORKER,
    Component.EXECUTION_PIPELINE: RecoveryRecommendation.Action.FORCE_FAIL_JOB,
}


def _dedup_key(row: ComponentHealth) -> str:
    return f"{row.component}:{row.terminal_node_id or 0}:{row.trading_account_id or 0}"


def _severity(row: ComponentHealth) -> str:
    if row.status == HealthStatus.FAILED and row.component in CRITICAL_COMPONENTS:
        return AlertEvent.Severity.CRITICAL
    if row.status in (HealthStatus.FAILED, HealthStatus.STALE, HealthStatus.DEGRADED):
        return AlertEvent.Severity.WARN
    return AlertEvent.Severity.INFO


def _deliver(alert: AlertEvent):
    """Optional internal webhook delivery; persist-only if unconfigured."""
    url = (os.getenv("RELIABILITY_ALERT_WEBHOOK_URL") or "").strip()
    if not url:
        alert.delivery_status = "SKIPPED"
        alert.delivery_detail = {"reason": "no_webhook_configured"}
        alert.save(update_fields=["delivery_status", "delivery_detail"])
        return
    try:
        body = json.dumps({"severity": alert.severity, "title": alert.title, "body": alert.body,
                           "component": alert.component, "created_at": alert.created_at.isoformat()}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8)
        alert.delivery_status = "SENT"
    except Exception as e:  # noqa: BLE001
        alert.delivery_status = "FAILED"
        alert.delivery_detail = {"error": type(e).__name__}
    alert.save(update_fields=["delivery_status", "delivery_detail"])


def reconcile(orphan_jobs=None):
    """Open/resolve alerts + recommendations from current ComponentHealth.

    Returns counts: {"opened": n, "resolved": n, "recommended": n}.
    """
    orphan_jobs = orphan_jobs or []
    opened = resolved = recommended = 0
    rows = list(ComponentHealth.objects.all())

    for row in rows:
        key = _dedup_key(row)
        open_qs = AlertEvent.objects.filter(dedup_key=key).exclude(status=AlertEvent.Status.RESOLVED)

        if row.status == HealthStatus.OK:
            # Auto-resolve any open/acknowledged alert for this scope.
            for a in open_qs:
                a.status = AlertEvent.Status.RESOLVED
                a.resolved_at = timezone.now()
                a.save(update_fields=["status", "resolved_at"])
                resolved += 1
            RecoveryRecommendation.objects.filter(dedup_key=key, status=RecoveryRecommendation.Status.OPEN).update(
                status=RecoveryRecommendation.Status.SUPERSEDED)
            continue

        if row.status == HealthStatus.UNKNOWN:
            continue  # do not alert on unknown (e.g. endpoint not yet deployed)

        # Unhealthy → ensure one OPEN alert (dedup).
        if not open_qs.exists():
            label = dict(Component.CHOICES).get(row.component, row.component)
            alert = AlertEvent.objects.create(
                severity=_severity(row), component=row.component,
                terminal_node_id=row.terminal_node_id, mt5_instance_id=row.mt5_instance_id,
                trading_account_id=row.trading_account_id,
                title=f"{label} {row.status}", body=json.dumps(row.detail)[:1000],
                dedup_key=key, status=AlertEvent.Status.OPEN, detail=row.detail,
            )
            opened += 1
            _deliver(alert)

        # Advisory recommendation (no execution).
        action = _ACTION_FOR.get(row.component)
        if action:
            if row.component == Component.EXECUTION_PIPELINE and orphan_jobs:
                for jid in [j.id for j in orphan_jobs]:
                    rkey = f"{key}:job:{jid}"
                    if not RecoveryRecommendation.objects.filter(dedup_key=rkey, status=RecoveryRecommendation.Status.OPEN).exists():
                        RecoveryRecommendation.objects.create(
                            component=row.component, recommended_action=action, target_ref=f"job:{jid}",
                            rationale=f"ExecutionJob {jid} is RUNNING past its lease; recommend manual force-fail (operator).",
                            severity=_severity(row), dedup_key=rkey)
                        recommended += 1
            else:
                if not RecoveryRecommendation.objects.filter(dedup_key=key, status=RecoveryRecommendation.Status.OPEN).exists():
                    RecoveryRecommendation.objects.create(
                        component=row.component, recommended_action=action,
                        terminal_node_id=row.terminal_node_id, trading_account_id=row.trading_account_id,
                        target_ref=key, rationale=f"{row.component} is {row.status}; recommended operator action: {action}.",
                        severity=_severity(row), dedup_key=key)
                    recommended += 1

    return {"opened": opened, "resolved": resolved, "recommended": recommended}
