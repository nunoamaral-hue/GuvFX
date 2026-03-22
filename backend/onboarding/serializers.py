from rest_framework import serializers
from .models import BrokerPartner


class OnboardingStateSerializer(serializers.Serializer):
    email_verified = serializers.BooleanField(read_only=True)
    two_factor_enabled = serializers.BooleanField(read_only=True)
    risk_accepted = serializers.BooleanField(read_only=True)
    plan_selected = serializers.BooleanField(read_only=True)
    account_connected = serializers.BooleanField(read_only=True)
    strategy_assigned = serializers.BooleanField(read_only=True)
    onboarding_completed = serializers.BooleanField(read_only=True)
    risk_accepted_at = serializers.DateTimeField(read_only=True)
    onboarding_completed_at = serializers.DateTimeField(read_only=True)


class CompleteStepSerializer(serializers.Serializer):
    step = serializers.ChoiceField(
        choices=[
            "risk_accepted",
            "plan_selected",
            "account_connected",
            "strategy_assigned",
        ],
    )


class EmailVerifySerializer(serializers.Serializer):
    token = serializers.CharField(max_length=128)


class TwoFactorVerifySerializer(serializers.Serializer):
    otp_code = serializers.CharField(min_length=6, max_length=6)


class BrokerPartnerSerializer(serializers.ModelSerializer):
    class Meta:
        model = BrokerPartner
        fields = ["id", "name", "broker_code", "referral_url", "is_active"]
        read_only_fields = fields


class BrokerReferralSerializer(serializers.Serializer):
    broker_code = serializers.CharField(max_length=64)
    referral_code = serializers.CharField(max_length=128, required=False, default="")
