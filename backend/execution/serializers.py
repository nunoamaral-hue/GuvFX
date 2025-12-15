from decimal import Decimal
from rest_framework import serializers
from .models import ExecutionJob


class ExecutionJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionJob
        fields = "__all__"
        read_only_fields = (
            "status",
            "worker_id",
            "result",
            "error_message",
            "created_at",
            "started_at",
            "finished_at",
            "created_by",
        )


class OpenTradeJobRequestSerializer(serializers.Serializer):
    account = serializers.IntegerField()
    strategy = serializers.IntegerField()

    symbol = serializers.CharField(max_length=32)
    direction = serializers.ChoiceField(choices=["BUY", "SELL"])
    timeframe = serializers.CharField(max_length=16)

    entry_type = serializers.ChoiceField(choices=["MARKET", "LIMIT", "STOP"])
    entry_price = serializers.DecimalField(
        max_digits=20, decimal_places=10, required=False, allow_null=True
    )

    sl_price = serializers.DecimalField(max_digits=20, decimal_places=10)
    tp_price = serializers.DecimalField(
        max_digits=20, decimal_places=10, required=False, allow_null=True
    )

    risk_per_trade_pct = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, allow_null=True
    )

    comment = serializers.CharField(max_length=128, required=False, allow_blank=True)
