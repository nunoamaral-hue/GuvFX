from django.urls import path
from .views import (
    OnboardingStateView,
    CompleteStepView,
    EmailSendVerificationView,
    EmailVerifyView,
    TwoFactorSetupView,
    TwoFactorVerifyView,
    RiskAcceptView,
    BrokerPartnerListView,
    BrokerReferralView,
    ExecutionReadinessView,
    AccountStatusView,
)

urlpatterns = [
    path("state/", OnboardingStateView.as_view(), name="onboarding-state"),
    path("complete-step/", CompleteStepView.as_view(), name="onboarding-complete-step"),

    # Email verification
    path("email/send-verification/", EmailSendVerificationView.as_view(), name="onboarding-email-send"),
    path("email/verify/", EmailVerifyView.as_view(), name="onboarding-email-verify"),

    # 2FA
    path("2fa/setup/", TwoFactorSetupView.as_view(), name="onboarding-2fa-setup"),
    path("2fa/verify/", TwoFactorVerifyView.as_view(), name="onboarding-2fa-verify"),

    # Risk acceptance
    path("risk/accept/", RiskAcceptView.as_view(), name="onboarding-risk-accept"),

    # Broker partners
    path("brokers/", BrokerPartnerListView.as_view(), name="onboarding-brokers"),
    path("brokers/referral/", BrokerReferralView.as_view(), name="onboarding-broker-referral"),

    # Readiness gate
    path("readiness/", ExecutionReadinessView.as_view(), name="onboarding-readiness"),

    # GFX-BETA-PHASE0 Increment 3 — truthful Account Status panel (account-owner scoped)
    path("account-status/", AccountStatusView.as_view(), name="onboarding-account-status"),
]
