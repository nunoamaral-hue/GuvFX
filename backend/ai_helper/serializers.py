from rest_framework import serializers


class StrategyInsightsRequestSerializer(serializers.Serializer):
    strategy_id = serializers.IntegerField()
    max_runs = serializers.IntegerField(required=False, default=5)


class StrategyInsightsResponseSerializer(serializers.Serializer):
    strategy_id = serializers.IntegerField()
    summary = serializers.CharField()
    recommendations = serializers.ListField(
        child=serializers.CharField(), allow_empty=True
    )
    risk_assessment = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
