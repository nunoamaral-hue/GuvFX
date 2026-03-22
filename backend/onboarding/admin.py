from django.contrib import admin
from .models import (
    UserOnboardingState,
    EmailVerificationToken,
    TwoFactorSecret,
    BrokerPartner,
    UserBrokerReferral,
)


@admin.register(UserOnboardingState)
class UserOnboardingStateAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "email_verified",
        "two_factor_enabled",
        "risk_accepted",
        "plan_selected",
        "account_connected",
        "strategy_assigned",
        "onboarding_completed",
    )
    list_filter = ("onboarding_completed", "email_verified", "plan_selected")
    search_fields = ("user__email",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(BrokerPartner)
class BrokerPartnerAdmin(admin.ModelAdmin):
    list_display = ("name", "broker_code", "is_active")
    list_filter = ("is_active",)


@admin.register(UserBrokerReferral)
class UserBrokerReferralAdmin(admin.ModelAdmin):
    list_display = ("user", "broker_partner", "clicked_at")
    list_filter = ("broker_partner",)
    search_fields = ("user__email",)
