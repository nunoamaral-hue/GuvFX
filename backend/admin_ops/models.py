"""
Admin Operations Console models.

Only one new model is introduced: ``EntitlementOverride`` — a temporary,
capability-scoped overlay that does NOT mutate the underlying
UserSubscriptionState.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class EntitlementOverride(models.Model):
    """
    Temporary, capability-scoped entitlement overlay.

    Rules enforced by the service layer / API (not model-level):
    - mandatory ``reason``
    - mandatory ``expires_at`` (in the future at creation)
    - ``created_by`` must be a super_admin
    - no silent stacking (unique on user + capability + is_active)
    - renewals and cancellations are audited separately
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="entitlement_overrides",
    )

    capability = models.CharField(
        max_length=64,
        help_text=(
            "Entitlement capability being overridden, e.g. "
            "'can_deploy_automation', 'max_trading_accounts'."
        ),
    )

    override_value = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Override payload.  For boolean capabilities: {'granted': true}. "
            "For numeric limits: {'value': 10}."
        ),
    )

    reason = models.TextField(
        help_text="Mandatory justification for the override.",
    )

    is_active = models.BooleanField(default=True, db_index=True)

    expires_at = models.DateTimeField(
        db_index=True,
        help_text="Mandatory expiry timestamp (UTC).",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_overrides",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Entitlement Override"
        verbose_name_plural = "Entitlement Overrides"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "capability"],
                condition=models.Q(is_active=True),
                name="uniq_active_override_per_capability",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "is_active"],
                name="idx_override_user_active",
            ),
            models.Index(
                fields=["expires_at"],
                name="idx_override_expires",
            ),
        ]

    def __str__(self) -> str:
        status = "active" if self.is_active else "inactive"
        return f"Override({self.capability}) for user {self.user_id} [{status}]"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at
