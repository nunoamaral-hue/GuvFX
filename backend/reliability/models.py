"""
RX-2 Reliability Core — domain models.

Phase 1 posture: Detection + Visibility + Alerting + Recovery *Recommendations*.
RecoveryAction is an audit record only — Phase 1 never writes one automatically
and never executes recovery.
"""
from django.conf import settings
from django.db import models

from .constants import Component, HealthStatus, TradingState, Scope


class ComponentHealth(models.Model):
    """Latest evaluated health for a (component, scope) pair.

    Scope FKs are null for GLOBAL/infrastructure components and set for
    terminal/account-scoped components (MT5_*, SNAPSHOT_FEED, EXECUTION_PIPELINE).
    """
    component = models.CharField(max_length=32, choices=Component.CHOICES)
    terminal_node = models.ForeignKey(
        "execution.TerminalNode", null=True, blank=True,
        on_delete=models.CASCADE, related_name="component_health",
    )
    mt5_instance = models.ForeignKey(
        "mt5.Mt5Instance", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="component_health",
    )
    trading_account = models.ForeignKey(
        "trading.TradingAccount", null=True, blank=True,
        on_delete=models.CASCADE, related_name="component_health",
    )
    status = models.CharField(max_length=12, choices=HealthStatus.CHOICES, default=HealthStatus.UNKNOWN)
    since = models.DateTimeField(null=True, blank=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    detail = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["component", "terminal_node", "trading_account"],
                name="uniq_component_scope",
            )
        ]
        indexes = [models.Index(fields=["component", "status"])]

    def __str__(self):
        return f"{self.component}[{self.trading_account_id or self.terminal_node_id or 'GLOBAL'}]={self.status}"


class Heartbeat(models.Model):
    """Liveness beat per source (scheduler / worker). RX-2C producer rows."""
    source = models.CharField(max_length=32, unique=True)
    last_beat_at = models.DateTimeField()
    expected_interval_s = models.PositiveIntegerField(default=60)
    detail = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"hb:{self.source}@{self.last_beat_at:%H:%M:%S}"


class TradingHealthSnapshot(models.Model):
    """Aggregate trading-capability state. Produced at GLOBAL / per-terminal /
    per-account scope each reliability_tick."""
    scope = models.CharField(max_length=12, choices=Scope.CHOICES, default=Scope.GLOBAL)
    terminal_node = models.ForeignKey(
        "execution.TerminalNode", null=True, blank=True,
        on_delete=models.CASCADE, related_name="health_snapshots",
    )
    mt5_instance = models.ForeignKey(
        "mt5.Mt5Instance", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="health_snapshots",
    )
    trading_account = models.ForeignKey(
        "trading.TradingAccount", null=True, blank=True,
        on_delete=models.CASCADE, related_name="health_snapshots",
    )
    state = models.CharField(max_length=12, choices=TradingState.CHOICES, default=TradingState.UNKNOWN)
    can_trade = models.BooleanField(default=False)
    reasons = models.JSONField(default=list, blank=True)
    components = models.JSONField(default=dict, blank=True)
    computed_at = models.DateTimeField(db_index=True)

    class Meta:
        indexes = [models.Index(fields=["scope", "computed_at"])]
        ordering = ["-computed_at"]

    def __str__(self):
        return f"{self.scope} {self.state} can_trade={self.can_trade} @{self.computed_at:%H:%M:%S}"


class AlertEvent(models.Model):
    """Durable alert with OPEN → ACKNOWLEDGED → RESOLVED lifecycle."""
    class Severity(models.TextChoices):
        INFO = "INFO", "Info"
        WARN = "WARN", "Warning"
        CRITICAL = "CRITICAL", "Critical"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
        RESOLVED = "RESOLVED", "Resolved"

    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.WARN)
    component = models.CharField(max_length=32, choices=Component.CHOICES)
    terminal_node = models.ForeignKey("execution.TerminalNode", null=True, blank=True, on_delete=models.SET_NULL, related_name="alerts")
    mt5_instance = models.ForeignKey("mt5.Mt5Instance", null=True, blank=True, on_delete=models.SET_NULL, related_name="alerts")
    trading_account = models.ForeignKey("trading.TradingAccount", null=True, blank=True, on_delete=models.SET_NULL, related_name="alerts")
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    dedup_key = models.CharField(max_length=200, db_index=True)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.OPEN)
    detail = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="acknowledged_alerts")
    resolved_at = models.DateTimeField(null=True, blank=True)

    delivery_status = models.CharField(max_length=10, default="PENDING")
    delivery_detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "severity"]), models.Index(fields=["dedup_key", "status"])]

    def __str__(self):
        return f"[{self.severity}] {self.title} ({self.status})"


class RecoveryRecommendation(models.Model):
    """Advisory remediation surfaced to operators. NEVER executed in Phase 1."""
    class Action(models.TextChoices):
        MT5_RELOGIN = "MT5_RELOGIN", "Re-login MT5 to broker"
        RESTART_BRIDGE = "RESTART_BRIDGE", "Restart signal bridge"
        FORCE_FAIL_JOB = "FORCE_FAIL_JOB", "Force-fail orphaned execution job"
        RESTART_WORKER = "RESTART_WORKER", "Restart worker"
        INVESTIGATE_SNAPSHOT = "INVESTIGATE_SNAPSHOT", "Investigate stale snapshot feed"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        DISMISSED = "DISMISSED", "Dismissed"
        SUPERSEDED = "SUPERSEDED", "Superseded"
        COMPLETED = "COMPLETED", "Completed"

    component = models.CharField(max_length=32, choices=Component.CHOICES)
    terminal_node = models.ForeignKey("execution.TerminalNode", null=True, blank=True, on_delete=models.SET_NULL, related_name="recommendations")
    mt5_instance = models.ForeignKey("mt5.Mt5Instance", null=True, blank=True, on_delete=models.SET_NULL, related_name="recommendations")
    trading_account = models.ForeignKey("trading.TradingAccount", null=True, blank=True, on_delete=models.SET_NULL, related_name="recommendations")
    recommended_action = models.CharField(max_length=32, choices=Action.choices)
    target_ref = models.CharField(max_length=120, blank=True)
    rationale = models.TextField(blank=True)
    severity = models.CharField(max_length=10, default="WARN")
    linked_alert = models.ForeignKey(AlertEvent, null=True, blank=True, on_delete=models.SET_NULL, related_name="recommendations")
    dedup_key = models.CharField(max_length=200, db_index=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"REC {self.recommended_action} {self.target_ref} ({self.status})"


class RecoveryAction(models.Model):
    """Audit record of a remediation ACTUALLY performed. Phase 1: created only
    by explicit operator action / runbook logging — never by reliability_tick."""
    class ActorType(models.TextChoices):
        MANUAL = "MANUAL", "Manual (operator)"
        SYSTEM = "SYSTEM", "System (Phase 2+)"

    class Outcome(models.TextChoices):
        OK = "OK", "Ok"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    recommendation = models.ForeignKey(RecoveryRecommendation, null=True, blank=True, on_delete=models.SET_NULL, related_name="actions")
    action = models.CharField(max_length=32)
    target_ref = models.CharField(max_length=120, blank=True)
    actor_type = models.CharField(max_length=10, choices=ActorType.choices, default=ActorType.MANUAL)
    actor_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="recovery_actions")
    outcome = models.CharField(max_length=10, choices=Outcome.choices, default=Outcome.OK)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"ACT {self.action} {self.target_ref} by {self.actor_type} -> {self.outcome}"
