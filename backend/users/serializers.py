from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "username",
            "first_name",
            "last_name",
            "is_staff",
            "is_superuser",
        ]


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ["email", "username", "password", "first_name", "last_name"]

    def validate_password(self, value):
        """Run Django's password validators (common passwords, similarity, etc.)."""
        validate_password(value)
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        # GFX-BETA-PHASE0 Increment 4 — new users automatically become beta (payment-bypassed) in the
        # data model. This grants config capabilities only; it does NOT open onboarding
        # (beta_onboarding_open() stays False) and cannot make trading reachable.
        try:
            from billing.beta import grant_beta_entitlement
            grant_beta_entitlement(user)
        except Exception:  # pragma: no cover - entitlement is best-effort, never blocks registration
            import logging
            logging.getLogger(__name__).exception(
                "register: beta entitlement grant failed for user=%s", user.id)
        return user


class EmailTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Custom token serializer that explicitly uses 'email' as the login field.
    """
    username_field = "email"


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)

    def validate_old_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Old password is incorrect.")
        return value

    def validate_new_password(self, value):
        # Use Django's password validators
        validate_password(value)
        return value

    def save(self, **kwargs):
        user = self.context["request"].user
        new_password = self.validated_data["new_password"]
        user.set_password(new_password)
        user.save()
        return user