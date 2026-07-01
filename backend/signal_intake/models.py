"""
Signal Intake (GFX-PKT-WAYOND-EXEC-E0) — SHADOW.

Captures a parsed Wayond Telegram signal as a human-reviewable
``PendingSignalApproval``. This is the execution-side entry point for the
Telegram signal, kept entirely separate from the WIMS content path
(ADR-009: a WIMS ConsumptionContract must never trigger an order).

E0 is SHADOW ONLY:
  * No ExecutionJob is ever created here (this app does not import ``execution``).
  * Approving/rejecting changes status and writes an audit row — nothing more.
  * The signal→order bridge is a LATER, separately-gated packet (E1+).
"""

from django.conf import settings
from django.db import models


class PendingSignalApproval(models.Model):
    """A Wayond signal awaiting a human decision. Creates NO order."""

    class Source(models.TextChoices):
        WAYOND_TELEGRAM = "WAYOND_TELEGRAM", "Wayond Telegram"

    class Direction(models.TextChoices):
        BUY = "BUY", "Buy"
        SELL = "SELL", "Sell"

    class Status(models.TextChoices):
        PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
        APPROVED = "APPROVED", "Approved (no order placed — shadow)"
        REJECTED = "REJECTED", "Rejected"
        EXPIRED = "EXPIRED", "Expired"
        QUARANTINED = "QUARANTINED", "Quarantined (unparseable)"

    source = models.CharField(
        max_length=32, choices=Source.choices, default=Source.WAYOND_TELEGRAM
    )
    # External Telegram message identifier — the dedup key (with source).
    message_id = models.CharField(max_length=128)

    # Descriptive signal metadata (strings, as parsed; NOT an executable order).
    symbol = models.CharField(max_length=32, blank=True)
    direction = models.CharField(max_length=8, choices=Direction.choices, blank=True)
    entry = models.CharField(max_length=32, blank=True)
    stop_loss = models.CharField(max_length=32, blank=True)
    take_profit = models.CharField(max_length=32, blank=True)
    take_profits = models.JSONField(default=list, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)

    # OPS-OBSERVABILITY: correlation id tying every lifecycle stage of one
    # execution attempt together (signal → plan → shadow job → order_check).
    # Nullable/blank for backwards compatibility with pre-existing rows.
    correlation_id = models.CharField(max_length=64, blank=True, default="")

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING_APPROVAL
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="signal_approvals",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # Deduplication: one record per (source, message_id).
            models.UniqueConstraint(
                fields=["source", "message_id"], name="uniq_source_message",
            ),
        ]
        indexes = [models.Index(fields=["status"])]
        verbose_name = "Pending signal approval"

    def __str__(self) -> str:
        return f"[{self.status}] {self.source} {self.message_id} {self.direction} {self.symbol}"


class SignalAuditEvent(models.Model):
    """Append-only audit of signal-intake decisions (no execution events)."""

    class Event(models.TextChoices):
        SIGNAL_RECEIVED = "SIGNAL_RECEIVED", "Signal received (pending approval)"
        SIGNAL_QUARANTINED = "SIGNAL_QUARANTINED", "Signal quarantined"
        SIGNAL_APPROVED = "SIGNAL_APPROVED", "Signal approved (shadow — no order)"
        SIGNAL_REJECTED = "SIGNAL_REJECTED", "Signal rejected"

    timestamp = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="signal_audit_events",
    )
    event = models.CharField(max_length=32, choices=Event.choices)
    approval = models.ForeignKey(
        PendingSignalApproval, on_delete=models.CASCADE, related_name="audit_events",
    )
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("timestamp", "id")

    def __str__(self) -> str:
        return f"{self.timestamp:%Y-%m-%d %H:%M:%S} {self.event} approval#{self.approval_id}"
