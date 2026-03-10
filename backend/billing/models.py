from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


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
