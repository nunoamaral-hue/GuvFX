from rest_framework import serializers
from .models import TradingAccount, BrokerServer, Trade
from .crypto import encrypt_password


class TradingAccountSerializer(serializers.ModelSerializer):
    # Preferred: normalized FK
    broker_server = serializers.PrimaryKeyRelatedField(
        queryset=BrokerServer.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )

    # Read-only convenience fields for UI
    broker_display_name = serializers.CharField(source="broker_server.broker_display_name", read_only=True)
    server_name = serializers.CharField(source="broker_server.server_name", read_only=True)

    # Incoming plaintext password (write-only). We store it into password_enc.
    password = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)

    class Meta:
        model = TradingAccount
        fields = [
            "id",
            "name",
            "broker_server",
            "broker_display_name",
            "server_name",
            "broker_name",          # legacy fallback (optional)
            "account_number",
            "is_demo",
            "is_active",
            "created_at",
            "updated_at",
            "password",             # write-only
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        broker_server = attrs.get("broker_server") or getattr(self.instance, "broker_server", None)
        broker_name = (attrs.get("broker_name") or getattr(self.instance, "broker_name", "") or "").strip()

        if not broker_server and not broker_name:
            raise serializers.ValidationError("Provide either broker_server or broker_name.")

        return attrs

    def create(self, validated_data):
        raw_password = validated_data.pop("password", "") or ""
        # Backwards compatibility: if broker_password is still sent by older UI
        legacy_pw = validated_data.pop("broker_password", "") if "broker_password" in validated_data else ""
        raw_password = raw_password or legacy_pw

        if raw_password:
            validated_data["password_enc"] = encrypt_password(raw_password)
            # keep legacy field empty going forward
            validated_data["broker_password"] = ""

        return super().create(validated_data)

    def update(self, instance, validated_data):
        raw_password = validated_data.pop("password", "") or ""
        legacy_pw = validated_data.pop("broker_password", "") if "broker_password" in validated_data else ""
        raw_password = raw_password or legacy_pw

        if raw_password:
            instance.password_enc = encrypt_password(raw_password)
            instance.broker_password = ""

        return super().update(instance, validated_data)


class TradeSerializer(serializers.ModelSerializer):
    """Read-only serializer for executed trades."""

    class Meta:
        model = Trade
        fields = "__all__"
        read_only_fields = "__all__"