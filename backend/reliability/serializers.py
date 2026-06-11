from rest_framework import serializers

from .models import ComponentHealth, TradingHealthSnapshot, AlertEvent, RecoveryRecommendation


class ComponentHealthSerializer(serializers.ModelSerializer):
    class Meta:
        model = ComponentHealth
        fields = ["id", "component", "terminal_node", "mt5_instance", "trading_account",
                  "status", "since", "last_ok_at", "consecutive_failures", "detail", "updated_at"]


class TradingHealthSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = TradingHealthSnapshot
        fields = ["id", "scope", "terminal_node", "mt5_instance", "trading_account",
                  "state", "can_trade", "reasons", "components", "computed_at"]


class AlertEventSerializer(serializers.ModelSerializer):
    acknowledged_by_username = serializers.CharField(source="acknowledged_by.username", default=None, read_only=True)

    class Meta:
        model = AlertEvent
        fields = ["id", "severity", "component", "terminal_node", "mt5_instance", "trading_account",
                  "title", "body", "dedup_key", "status", "detail",
                  "created_at", "acknowledged_at", "acknowledged_by", "acknowledged_by_username",
                  "resolved_at", "delivery_status"]


class RecoveryRecommendationSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecoveryRecommendation
        fields = ["id", "component", "terminal_node", "mt5_instance", "trading_account",
                  "recommended_action", "target_ref", "rationale", "severity",
                  "linked_alert", "status", "created_at", "updated_at"]
