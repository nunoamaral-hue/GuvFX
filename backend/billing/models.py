import hashlib
import json

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# UserSubscriptionState
# ---------------------------------------------------------------------------


class UserSubscriptionState(models.Model):
    """
    Singleton-per-user record tracking the user's current product subscription,
    billing status, trial dates, and billing-cycle metadata.
    """

    class Plan(models.TextChoices):
        STARTER_TRIAL = "starter_trial", "Starter Trial"
        STANDARD = "standard", "Standard"
        PRO = "pro", "Pro"
        ADVANCED = "advanced", "Advanced"

    class PlanStatus(models.TextChoices):
        TRIAL_ACTIVE = "trial_active", "Trial Active"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past Due"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        VIEWER_ONLY = "viewer_only", "Viewer Only"

    class BillingCycle(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        ANNUAL = "annual", "Annual"

    # ---- Relationships ----
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscription_state",
    )

    # ---- Plan & Status ----
    current_plan = models.CharField(
        max_length=32,
        choices=Plan.choices,
        null=True,
        blank=True,
    )
    plan_status = models.CharField(
        max_length=32,
        choices=PlanStatus.choices,
        default=PlanStatus.VIEWER_ONLY,
    )
    viewer_mode = models.BooleanField(default=True)
    has_ever_paid = models.BooleanField(default=False)
    currency = models.CharField(max_length=3, default="USD")

    # ---- Trial tracking ----
    trial_started_at = models.DateTimeField(null=True, blank=True)
    trial_expires_at = models.DateTimeField(null=True, blank=True)

    # ---- Billing cycle ----
    billing_cycle = models.CharField(
        max_length=16,
        choices=BillingCycle.choices,
        null=True,
        blank=True,
    )
    current_period_started_at = models.DateTimeField(null=True, blank=True)
    current_period_ends_at = models.DateTimeField(null=True, blank=True)

    # ---- Invoice / payment dates ----
    next_invoice_date = models.DateField(null=True, blank=True)
    next_payment_due_date = models.DateField(null=True, blank=True)
    last_invoice_date = models.DateField(null=True, blank=True)
    last_payment_at = models.DateTimeField(null=True, blank=True)
    last_plan_change_at = models.DateTimeField(null=True, blank=True)

    # ---- Timestamps ----
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User subscription state"
        verbose_name_plural = "User subscription states"

    def __str__(self) -> str:
        plan = self.current_plan or "no-plan"
        return f"{self.user} – {plan} ({self.plan_status})"

    def clean(self) -> None:
        super().clean()
        self._validate_viewer_mode_consistency()

    def save(self, *args, **kwargs) -> None:
        self._validate_viewer_mode_consistency()
        super().save(*args, **kwargs)

    def _validate_viewer_mode_consistency(self) -> None:
        """
        Enforce invariant: viewer_mode must be consistent with plan_status.

        - viewer_mode=True  is invalid when plan_status in {trial_active, active}
        - viewer_mode=False is invalid when plan_status in {expired, viewer_only, cancelled}
        - past_due is intentionally permissive (grace-period ambiguity)
        """
        VIEWER_STATUSES = {
            self.PlanStatus.EXPIRED,
            self.PlanStatus.VIEWER_ONLY,
            self.PlanStatus.CANCELLED,
        }
        ACTIVE_STATUSES = {
            self.PlanStatus.TRIAL_ACTIVE,
            self.PlanStatus.ACTIVE,
        }

        if self.viewer_mode and self.plan_status in ACTIVE_STATUSES:
            raise ValidationError(
                {
                    "viewer_mode": (
                        f"viewer_mode cannot be True when plan_status "
                        f"is '{self.plan_status}'."
                    )
                }
            )
        if not self.viewer_mode and self.plan_status in VIEWER_STATUSES:
            raise ValidationError(
                {
                    "viewer_mode": (
                        f"viewer_mode cannot be False when plan_status "
                        f"is '{self.plan_status}'."
                    )
                }
            )


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------


class Invoice(models.Model):
    """
    An invoice record tied to a user, optionally linked to their
    UserSubscriptionState at time of issue.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ISSUED = "issued", "Issued"
        PAID = "paid", "Paid"
        PAST_DUE = "past_due", "Past Due"
        VOID = "void", "Void"
        CANCELLED = "cancelled", "Cancelled"

    # ---- Relationships ----
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="invoices",
    )
    subscription_state = models.ForeignKey(
        UserSubscriptionState,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices",
    )

    # ---- Invoice identity ----
    invoice_number = models.CharField(max_length=64, unique=True)

    # ---- Snapshot of plan at time of issue ----
    plan_at_issue = models.CharField(
        max_length=32,
        choices=UserSubscriptionState.Plan.choices,
        null=True,
        blank=True,
    )
    billing_cycle_at_issue = models.CharField(
        max_length=16,
        choices=UserSubscriptionState.BillingCycle.choices,
        null=True,
        blank=True,
    )

    # ---- Period ----
    period_start = models.DateField()
    period_end = models.DateField()

    # ---- Dates ----
    issue_date = models.DateField()
    due_date = models.DateField()

    # ---- Status ----
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )

    # ---- Financials ----
    currency = models.CharField(max_length=3, default="USD")
    subtotal_amount = models.DecimalField(max_digits=10, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)

    # ---- Payment tracking ----
    paid_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)

    # ---- Metadata ----
    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    # ---- Timestamps ----
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Invoice"
        verbose_name_plural = "Invoices"
        ordering = ["-issue_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "period_start", "period_end", "plan_at_issue"],
                name="unique_user_period_plan_invoice",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "-issue_date"],
                name="idx_invoice_user_issue_date",
            ),
            models.Index(
                fields=["status"],
                name="idx_invoice_status",
            ),
        ]

    def __str__(self) -> str:
        return f"Invoice {self.invoice_number} ({self.user} – {self.status})"

    def clean(self) -> None:
        super().clean()
        self._validate_total_amount_consistency()

    def save(self, *args, **kwargs) -> None:
        self._validate_total_amount_consistency()
        super().save(*args, **kwargs)

    def _validate_total_amount_consistency(self) -> None:
        """
        Minimal deterministic check: total_amount must not be less than
        subtotal_amount, and should equal subtotal_amount + tax_amount.
        """
        if (
            self.subtotal_amount is not None
            and self.tax_amount is not None
            and self.total_amount is not None
        ):
            expected = self.subtotal_amount + self.tax_amount
            if self.total_amount != expected:
                raise ValidationError(
                    {
                        "total_amount": (
                            f"total_amount ({self.total_amount}) must equal "
                            f"subtotal_amount ({self.subtotal_amount}) + "
                            f"tax_amount ({self.tax_amount}) = {expected}."
                        )
                    }
                )


# ---------------------------------------------------------------------------
# PaymentEvent (webhook ingress log)
# ---------------------------------------------------------------------------

# Maximum raw payload size in bytes (64 KiB).  Payloads exceeding this
# limit are truncated before persistence.
_MAX_RAW_PAYLOAD_BYTES = 65_536

# Fields that must be redacted from raw payloads before storage.
_SENSITIVE_PAYLOAD_KEYS = frozenset({
    "password", "secret", "token", "api_key", "apikey",
    "credential", "private", "credit_card", "cvv", "ssn",
    "card_number", "cvc", "expiry",
})


def _sanitize_payload(payload: dict) -> dict:
    """
    Recursively redact sensitive keys from a webhook payload dict.

    Enforces the bounded-size rule by serialising → truncating → re-parsing
    if the result exceeds ``_MAX_RAW_PAYLOAD_BYTES``.
    """
    def _redact(obj):
        if isinstance(obj, dict):
            return {
                k: ("[REDACTED]" if k.lower() in _SENSITIVE_PAYLOAD_KEYS else _redact(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [_redact(item) for item in obj]
        return obj

    sanitized = _redact(payload)
    raw = json.dumps(sanitized, separators=(",", ":"), default=str)
    if len(raw.encode("utf-8")) > _MAX_RAW_PAYLOAD_BYTES:
        sanitized = {"_truncated": True, "_size": len(raw)}
    return sanitized


class PaymentEvent(models.Model):
    """
    Durable ingress log and processing record for payment-provider
    webhook events.

    Append / new-record oriented.  ``raw_payload`` is treated as immutable
    after creation — no update path is exposed.
    """

    class ProcessingStatus(models.TextChoices):
        RECEIVED = "received", "Received"
        VERIFIED = "verified", "Verified"
        DUPLICATE = "duplicate", "Duplicate"
        REJECTED = "rejected", "Rejected"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"

    # ---- Identity ----
    provider_name = models.CharField(
        max_length=64,
        db_index=True,
        help_text="Payment provider identifier (e.g. 'stripe').",
    )
    provider_event_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Provider-assigned event ID (used for primary idempotency).",
    )
    event_type = models.CharField(
        max_length=128,
        help_text="Provider event type (e.g. 'invoice.payment_succeeded').",
    )

    # ---- Idempotency ----
    idempotency_key = models.CharField(
        max_length=255,
        unique=True,
        help_text="Deterministic idempotency key (unique per webhook event).",
    )

    # ---- Subscription linkage ----
    subscription_reference = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_index=True,
        help_text="Provider subscription ID or internal reference.",
    )

    # ---- Timestamps ----
    provider_timestamp = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp provided by the payment provider for this event.",
    )
    signature_verified_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    processed_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # ---- Payload ----
    raw_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Sanitized, bounded webhook payload.  Immutable after creation.  "
            "Must not contain secrets or unbounded raw dumps."
        ),
    )

    # ---- Processing ----
    processing_status = models.CharField(
        max_length=16,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.RECEIVED,
        db_index=True,
    )

    # ---- Optional ----
    correlation_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Optional correlation identifier for tracing.",
    )

    class Meta:
        verbose_name = "Payment Event"
        verbose_name_plural = "Payment Events"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["provider_name", "provider_event_id"],
                name="idx_payment_provider_event",
            ),
            models.Index(
                fields=["processing_status", "created_at"],
                name="idx_payment_status_created",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"PaymentEvent {self.provider_name}:{self.provider_event_id} "
            f"[{self.processing_status}]"
        )

    def save(self, *args, **kwargs) -> None:
        # Immutability guard: raw_payload must not change after initial save.
        if self.pk is not None:
            try:
                existing = PaymentEvent.objects.only("raw_payload").get(pk=self.pk)
                if existing.raw_payload != self.raw_payload:
                    raise ValidationError(
                        {"raw_payload": "raw_payload is immutable after creation."}
                    )
            except PaymentEvent.DoesNotExist:
                pass  # First save — proceed normally.
        super().save(*args, **kwargs)

    @staticmethod
    def build_idempotency_key(
        provider_name: str,
        provider_event_id: str = "",
        event_type: str = "",
        subscription_reference: str = "",
        provider_timestamp: str = "",
    ) -> str:
        """
        Build a deterministic idempotency key.

        Primary source: ``provider_name`` + ``provider_event_id``.
        Fallback (when ``provider_event_id`` is absent):
            ``provider_name`` + ``event_type`` + ``subscription_reference``
            + ``provider_timestamp``.
        """
        if provider_event_id:
            raw = f"{provider_name}:{provider_event_id}"
        else:
            raw = (
                f"{provider_name}:{event_type}:"
                f"{subscription_reference}:{provider_timestamp}"
            )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
