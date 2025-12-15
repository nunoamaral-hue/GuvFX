from rest_framework import serializers

from .models import BacktestConfig, BacktestRun


class BacktestConfigSerializer(serializers.ModelSerializer):
    strategy_name = serializers.CharField(source="strategy.name", read_only=True)
    reference_account_name = serializers.CharField(
        source="reference_account.name",
        read_only=True,
    )

    class Meta:
        model = BacktestConfig
        fields = [
            "id",
            "name",
            "description",
            "strategy",
            "strategy_name",
            "reference_account",
            "reference_account_name",
            "symbol",
            "timeframe",
            "date_from",
            "date_to",
            "initial_balance",
            "risk_per_trade_pct",
            "slippage_points",
            "commission_per_lot",
            "is_active",
            "created_at",
            "updated_at",
        ]
    read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data):
        owner = self.context["request"].user
        return BacktestConfig.objects.create(owner=owner, **validated_data)

    def validate(self, attrs):
        date_from = attrs.get("date_from")
        date_to = attrs.get("date_to")
        if date_from and date_to and date_from >= date_to:
            raise serializers.ValidationError(
                "Start date must be before end date."
            )
        return attrs


class BacktestRunSerializer(serializers.ModelSerializer):
    config_name = serializers.CharField(source="config.name", read_only=True)

    class Meta:
        model = BacktestRun
        fields = [
            "id",
            "config",
            "config_name",
            "symbol",
            "timeframe",
            "date_from",
            "date_to",
            "initial_balance",
            "status",
            "error_message",
            "started_at",
            "finished_at",
            "metrics",
            "equity_curve",
            "created_at",
        ]
        # IMPORTANT: these are now read-only; client only sends "config"
        read_only_fields = [
            "id",
            "symbol",
            "timeframe",
            "date_from",
            "date_to",
            "initial_balance",
            "status",
            "error_message",
            "started_at",
            "finished_at",
            "metrics",
            "equity_curve",
            "created_at",
        ]
