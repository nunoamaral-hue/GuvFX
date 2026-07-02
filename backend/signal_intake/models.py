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

    # SIGNAL-ACQUISITION-MVP: the provider that acquired this signal. Nullable for
    # pre-existing rows and the legacy file-intake path (forward string ref).
    provider = models.ForeignKey(
        "signal_intake.SignalProvider", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="approvals",
    )

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
        # E3-APPROVAL-RBAC: approving/rejecting a signal requires this dedicated
        # permission — plain Django-admin/staff access is NOT sufficient.
        permissions = [
            ("review_signals", "Can approve or reject pending signals"),
        ]
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
        # E3-APPROVAL-RBAC: refused approve/reject attempts are audited too.
        APPROVAL_DENIED = "APPROVAL_DENIED", "Approve/reject attempt denied (no reviewer permission)"

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


# =============================================================================
# SIGNAL-ACQUISITION-MVP — provider platform (Phase 1, repo-only, no order)
# =============================================================================


class ParserProfile(models.Model):
    """Names a parser strategy a provider's messages are dispatched through.

    The ``slug`` is resolved to a callable in ``signal_intake.parsers``. MVP ships
    ``wayond_v1`` (wraps the existing Wayond parser)."""

    slug = models.CharField(max_length=64, unique=True)
    description = models.CharField(max_length=255, blank=True)
    version = models.CharField(max_length=32, default="v1")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.slug} ({self.version})"


class SignalProvider(models.Model):
    """A Telegram signal provider. Providers are DATA — onboarding is creating a
    row + arming it; only an ``ARMED`` provider is acquired. Never places an order."""

    class Status(models.TextChoices):
        ONBOARDING = "ONBOARDING", "Onboarding (not yet armed)"
        ARMED = "ARMED", "Armed (acquiring)"
        PAUSED = "PAUSED", "Paused (operator-disabled)"
        INACTIVE = "INACTIVE", "Inactive (no signal ~1 month)"
        RETIRED = "RETIRED", "Retired"

    slug = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=128, blank=True)
    # The chat identity = the trust boundary (allowlist). Blank until verified.
    telegram_chat_id = models.CharField(max_length=64, blank=True, db_index=True)
    chat_title = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ONBOARDING)
    parser_profile = models.ForeignKey(
        ParserProfile, on_delete=models.PROTECT, related_name="providers",
    )
    disabled_reason = models.CharField(max_length=255, blank=True)
    # ~5-10 min acquisition window (Nuno); per-provider override, default 600s.
    acquisition_window_seconds = models.PositiveIntegerField(default=600)
    last_signal_at = models.DateTimeField(null=True, blank=True)
    watermark_last_message_id = models.CharField(max_length=64, blank=True)
    subscription_note = models.CharField(max_length=255, blank=True)  # renewal date; NEVER creds
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="signal_providers",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("slug",)

    def is_armed(self) -> bool:
        return self.status == self.Status.ARMED

    def __str__(self) -> str:
        return f"{self.slug} ({self.status})"


class AcquiredMessage(models.Model):
    """Append-only acquisition ledger + dedup key. One row per (provider,
    message_id): every acquired message is recorded with its outcome so catch-up
    is replay-safe and stale/quarantined messages are audited (never silently
    dropped)."""

    class Outcome(models.TextChoices):
        INTAKEN = "INTAKEN", "Tradeable signal → PendingSignalApproval"
        UPDATE = "UPDATE", "Update (TP hit / move SL) recorded"
        STALE = "STALE", "Outside acquisition window — dismissed"
        QUARANTINED = "QUARANTINED", "Quarantined (edit/media/unknown/malformed)"
        DROPPED_NOT_ARMED = "DROPPED_NOT_ARMED", "Provider not armed — dropped"

    provider = models.ForeignKey(
        SignalProvider, on_delete=models.PROTECT, related_name="acquired_messages",
    )
    chat_id = models.CharField(max_length=64, blank=True)
    message_id = models.CharField(max_length=64)
    outcome = models.CharField(max_length=24, choices=Outcome.choices)
    reason = models.CharField(max_length=255, blank=True)
    telegram_date = models.DateTimeField(null=True, blank=True)
    acquired_at = models.DateTimeField(auto_now_add=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    approval = models.ForeignKey(
        PendingSignalApproval, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="acquired_messages",
    )

    class Meta:
        ordering = ("-acquired_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "message_id"], name="uniq_provider_message",
            ),
        ]
        indexes = [models.Index(fields=["outcome"])]

    def __str__(self) -> str:
        return f"[{self.outcome}] {self.provider_id}:{self.message_id}"


class SignalUpdate(models.Model):
    """A recorded reply/update message (TP hit / move SL / edit). MVP RECORDS these
    for operator visibility; acting on them (position modification) is a separate,
    later, gated packet."""

    class Kind(models.TextChoices):
        TP_HIT = "TP_HIT", "Take-profit hit"
        MOVE_SL = "MOVE_SL", "Move stop-loss"
        EDIT = "EDIT", "Edited message"
        OTHER = "OTHER", "Other update"

    provider = models.ForeignKey(
        SignalProvider, on_delete=models.PROTECT, related_name="updates",
    )
    chat_id = models.CharField(max_length=64, blank=True)
    message_id = models.CharField(max_length=64)
    reply_to_message_id = models.CharField(max_length=64, blank=True)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.OTHER)
    approval = models.ForeignKey(
        PendingSignalApproval, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="updates",
    )
    raw_payload = models.JSONField(default=dict, blank=True)
    processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "id")

    def __str__(self) -> str:
        return f"{self.kind} {self.provider_id}:{self.message_id}"
