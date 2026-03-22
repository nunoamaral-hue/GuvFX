"""
Onboarding delta layer — orchestration state models.

These models track onboarding progression only.
They do NOT duplicate billing, execution, or credential state.
"""
from __future__ import annotations

import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


# ─────────────────────────────────────────────────────────────────────
# A1 — UserOnboardingState
# ─────────────────────────────────────────────────────────────────────

class UserOnboardingState(models.Model):
    """
    Orchestration-only state for onboarding progression.
    Does NOT store billing state, credentials, or execution readiness.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="onboarding_state",
    )

    # Step flags
    email_verified = models.BooleanField(default=False)
    two_factor_enabled = models.BooleanField(default=False)
    risk_accepted = models.BooleanField(default=False)
    plan_selected = models.BooleanField(default=False)
    account_connected = models.BooleanField(default=False)
    strategy_assigned = models.BooleanField(default=False)
    onboarding_completed = models.BooleanField(default=False)

    # Metadata
    risk_accepted_at = models.DateTimeField(null=True, blank=True)
    onboarding_completed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User Onboarding State"
        verbose_name_plural = "User Onboarding States"

    def __str__(self) -> str:
        return f"OnboardingState(user={self.user_id}, completed={self.onboarding_completed})"


# ─────────────────────────────────────────────────────────────────────
# Email Verification Token
# ─────────────────────────────────────────────────────────────────────

class EmailVerificationToken(models.Model):
    """
    Secure email verification token with hashed storage.
    Plaintext token is sent to the user; only the SHA-256 hash is persisted.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="email_verification_tokens",
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    TOKEN_EXPIRY_HOURS = 24

    class Meta:
        verbose_name = "Email Verification Token"

    def __str__(self) -> str:
        return f"EmailVerificationToken(user={self.user_id}, used={self.used})"

    @classmethod
    def hash_token(cls, plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    @classmethod
    def create_for_user(cls, user) -> tuple["EmailVerificationToken", str]:
        """
        Create a new verification token for the user.
        Returns (token_instance, plaintext_token).
        The plaintext token is NOT persisted.
        """
        plaintext = secrets.token_urlsafe(48)
        token = cls.objects.create(
            user=user,
            token_hash=cls.hash_token(plaintext),
            expires_at=timezone.now() + timezone.timedelta(hours=cls.TOKEN_EXPIRY_HOURS),
        )
        return token, plaintext

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_valid(self) -> bool:
        return not self.used and not self.is_expired


# ─────────────────────────────────────────────────────────────────────
# Two-Factor Secret
# ─────────────────────────────────────────────────────────────────────

class TwoFactorSecret(models.Model):
    """
    TOTP secret encrypted via the platform's Fernet key (GUVFX_FERNET_KEY).
    Uses the same encryption subsystem as TradingAccount.password_enc.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="two_factor_secret",
    )
    secret_enc = models.TextField(
        help_text="Fernet-encrypted TOTP base32 secret. Never log or expose."
    )
    is_verified = models.BooleanField(
        default=False,
        help_text="True only after first successful TOTP verification.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Two-Factor Secret"

    def __str__(self) -> str:
        return f"TwoFactorSecret(user={self.user_id}, verified={self.is_verified})"


# ─────────────────────────────────────────────────────────────────────
# Broker Partner System
# ─────────────────────────────────────────────────────────────────────

class BrokerPartner(models.Model):
    """Reference data for broker partners. Tracking only — no credentials."""
    name = models.CharField(max_length=160)
    broker_code = models.CharField(max_length=64, unique=True)
    referral_url = models.URLField(max_length=512, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Broker Partner"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.broker_code})"


class UserBrokerReferral(models.Model):
    """Tracks user clicks on broker referral links. No credentials, no execution linkage."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="broker_referrals",
    )
    broker_partner = models.ForeignKey(
        BrokerPartner,
        on_delete=models.CASCADE,
        related_name="referrals",
    )
    referral_code = models.CharField(max_length=128, blank=True, default="")
    clicked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "User Broker Referral"

    def __str__(self) -> str:
        return f"Referral(user={self.user_id}, broker={self.broker_partner.broker_code})"
