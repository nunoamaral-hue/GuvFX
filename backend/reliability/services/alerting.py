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
    """Push the alert to the first configured operator channel.

    Channels (in order): Telegram bot, then generic webhook. Persist-only
    (SKIPPED) if none configured. Delivery NEVER raises — a failure is recorded
    as FAILED and reliability_tick continues. No external monitoring platform.
    """
    tg_token = (os.getenv("RELIABILITY_TELEGRAM_BOT_TOKEN") or "").strip()
    tg_chat = (os.getenv("RELIABILITY_TELEGRAM_CHAT_ID") or "").strip()
    webhook = (os.getenv("RELIABILITY_ALERT_WEBHOOK_URL") or "").strip()
    text = f"[{alert.severity}] {alert.title}\n{(alert.body or '')[:600]}"

    channel = detail = None
    try:
        if tg_token and tg_chat:
            channel = "telegram"
            data = json.dumps({"chat_id": tg_chat, "text": text}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                data=data, method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                detail = {"channel": channel, "http": getattr(r, "status", 200)}
            alert.delivery_status = "SENT"
        elif webhook:
            channel = "webhook"
            data = json.dumps({"severity": alert.severity, "title": alert.title, "body": alert.body,
                               "component": alert.component, "created_at": alert.created_at.isoformat()}).encode()
            req = urllib.request.Request(webhook, data=data, method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                detail = {"channel": channel, "http": getattr(r, "status", 200)}
            alert.delivery_status = "SENT"
        else:
            alert.delivery_status = "SKIPPED"
            detail = {"reason": "no_channel_configured"}
    except Exception as e:  # noqa: BLE001 — delivery must never break the tick
        alert.delivery_status = "FAILED"
        detail = {"channel": channel, "error": f"{type(e).__name__}:{str(e)[:160]}"}
    alert.delivery_detail = detail or {}
    alert.save(update_fields=["delivery_status", "delivery_detail"])


def reconcile(orphan_jobs=None):
    """Open/resolve alerts + recommendations from current ComponentHealth.

    Returns counts: {"opened": n, "resolved": n, "recommended": n}.
    """
    orphan_jobs = orphan_jobs or []
    opened = resolved = recommended = escalated = 0
    _RANK = {"INFO": 0, "WARN": 1, "CRITICAL": 2}
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

        # Unhealthy → ensure one OPEN alert (dedup), escalating severity if worsened.
        sev = _severity(row)
        label = dict(Component.CHOICES).get(row.component, row.component)
        existing = open_qs.first()
        if existing is None:
            alert = AlertEvent.objects.create(
                severity=sev, component=row.component,
                terminal_node_id=row.terminal_node_id, mt5_instance_id=row.mt5_instance_id,
                trading_account_id=row.trading_account_id,
                title=f"{label} {row.status}", body=json.dumps(row.detail)[:1000],
                dedup_key=key, status=AlertEvent.Status.OPEN, detail=row.detail,
            )
            opened += 1
            _deliver(alert)
        elif _RANK.get(sev, 0) > _RANK.get(existing.severity, 0):
            # Condition worsened (e.g. WARN sync orphan -> CRITICAL trade-exec orphan):
            # escalate the open alert's severity and re-attempt delivery.
            existing.severity = sev
            existing.title = f"{label} {row.status}"
            existing.detail = row.detail
            existing.save(update_fields=["severity", "title", "detail"])
            _deliver(existing)
            escalated += 1

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

    return {"opened": opened, "resolved": resolved, "recommended": recommended, "escalated": escalated}
