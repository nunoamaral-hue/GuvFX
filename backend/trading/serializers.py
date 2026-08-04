from rest_framework import serializers
from .models import TradingAccount, BrokerServer, Trade, BrokerAccountValidationAttempt
from .crypto import encrypt_password
from .classification import classification_error
from core.audit import log_customer_credential_event


class TradingAccountSerializer(serializers.ModelSerializer):
    broker_server = serializers.PrimaryKeyRelatedField(
        queryset=BrokerServer.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )

    broker_display_name = serializers.CharField(source="broker_server.broker_display_name", read_only=True)
    server_name = serializers.CharField(source="broker_server.server_name", read_only=True)

    # Accept plaintext password in request, store encrypted in password_enc
    password = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)
    mt5_instance = serializers.PrimaryKeyRelatedField(read_only=True)
    class Meta:
        model = TradingAccount
        fields = [
            "id",
            "name",
            "mt5_instance",
            "broker_server",
            "broker_display_name",
            "server_name",
            "broker_name",
            "account_number",
            "is_demo",
            "is_active",
            "created_at",
            "updated_at",
            "password",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        broker_server = attrs.get("broker_server") or getattr(self.instance, "broker_server", None)
        broker_name = (attrs.get("broker_name") or getattr(self.instance, "broker_name", "") or "").strip()
        if not broker_server and not broker_name:
            raise serializers.ValidationError("Provide either broker_server or broker_name.")

        # T7 (Phase 3 / P3-E): demo/live classification consistency, via the shared config-level check
        # (also enforced at the add-with-mt5-login create endpoint). Scoped to requests that set the
        # classification fields, so it never retroactively blocks an unrelated edit on a legacy
        # inconsistent row. Broker-TRUTH verification (account_info().trade_mode) stays at execution.
        if broker_server is not None and ("is_demo" in attrs or "broker_server" in attrs):
            is_demo = attrs.get("is_demo", getattr(self.instance, "is_demo", False))
            err = classification_error(is_demo, broker_server)
            if err:
                raise serializers.ValidationError({"is_demo": err})
        return attrs

    def create(self, validated_data):
        raw_password = validated_data.pop("password", "") or ""
        legacy_pw = validated_data.pop("broker_password", "") if "broker_password" in validated_data else ""
        raw_password = raw_password or legacy_pw

        if raw_password:
            validated_data["password_enc"] = encrypt_password(raw_password)
            validated_data["broker_password"] = ""

        instance = super().create(validated_data)
        if raw_password:
            # Customer-credential audit (Phase 3): intake of a broker password. Redacted, no secret.
            log_customer_credential_event(
                "CREATED", account=instance, request=self.context.get("request"), purpose="intake")
        return instance

    def update(self, instance, validated_data):
        raw_password = validated_data.pop("password", "") or ""
        legacy_pw = validated_data.pop("broker_password", "") if "broker_password" in validated_data else ""
        raw_password = raw_password or legacy_pw

        if raw_password:
            instance.password_enc = encrypt_password(raw_password)
            instance.broker_password = ""

        instance = super().update(instance, validated_data)
        if raw_password:
            # A new password supplied for an existing account is a credential ROTATION.
            log_customer_credential_event(
                "ROTATED", account=instance, request=self.context.get("request"), purpose="intake-update")
        return instance


class BrokerValidationAttemptSerializer(serializers.ModelSerializer):
    """WP1A (ADR-0028) — secret-safe projection of a broker-login validation attempt. Read-only; the
    field set is the ADR-0027 allow-list only (no password / ciphertext / envelope / host path)."""

    class Meta:
        model = BrokerAccountValidationAttempt
        fields = [
            "id", "trigger", "status", "reason_code", "retryable", "is_demo",
            "server", "login_masked", "correlation_id", "created_at",
        ]
        read_only_fields = fields


class TradeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Trade
        fields = "__all__"
        read_only_fields = "__all__"
