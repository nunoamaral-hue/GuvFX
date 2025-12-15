from rest_framework import serializers


class AccountPerformanceSerializer(serializers.Serializer):
    account_id = serializers.IntegerField()
    account_name = serializers.CharField()
    broker_name = serializers.CharField()

    num_trades = serializers.IntegerField()
    total_profit = serializers.DecimalField(max_digits=20, decimal_places=2)
    total_commission = serializers.DecimalField(max_digits=20, decimal_places=2)
    total_swap = serializers.DecimalField(max_digits=20, decimal_places=2)
    net_pnl = serializers.DecimalField(max_digits=20, decimal_places=2)


class StrategyBacktestSummarySerializer(serializers.Serializer):
    config_id = serializers.IntegerField()
    config_name = serializers.CharField()
    strategy_id = serializers.IntegerField()
    strategy_name = serializers.CharField()

    num_runs = serializers.IntegerField()
    last_status = serializers.CharField()
    last_run_created_at = serializers.DateTimeField(allow_null=True)
    last_metrics = serializers.JSONField(allow_null=True)