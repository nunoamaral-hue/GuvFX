from rest_framework import serializers

from .models import BacktestConfig, BacktestRun, WindowsBacktestJob
from strategies.models import Strategy
from trading.models import TradingAccount


# =========================================================================
# Packet B — B5: Canonical backtest API serializers
# =========================================================================


class BacktestRunRequestSerializer(serializers.Serializer):
    """
    Input for POST /api/backtests/jobs/run/.

    Maps only to allowed job creation inputs.
    """

    strategy_id = serializers.IntegerField(required=True)
    symbol = serializers.CharField(max_length=32, required=True)
    timeframe = serializers.CharField(max_length=20, required=True)
    start_date = serializers.DateField(required=True)
    end_date = serializers.DateField(required=True)
    parameter_set = serializers.JSONField(required=False, default=dict)
    data_source = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=""
    )

    def validate_strategy_id(self, value):
        if not Strategy.objects.filter(id=value).exists():
            raise serializers.ValidationError(
                f"Strategy with id {value} does not exist."
            )
        return value

    def validate(self, attrs):
        if attrs["start_date"] >= attrs["end_date"]:
            raise serializers.ValidationError(
                {"start_date": "start_date must be before end_date."}
            )
        return attrs


class BacktestRunResponseSerializer(serializers.Serializer):
    """Output for POST /api/backtests/jobs/run/."""

    backtest_job_id = serializers.IntegerField(read_only=True)
    backtest_execution_id = serializers.IntegerField(read_only=True)
    run_identifier = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    requested_at = serializers.DateTimeField(read_only=True)


class BacktestStatusResponseSerializer(serializers.Serializer):
    """Output for GET /api/backtests/jobs/{id}/status/."""

    job_id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    strategy_id = serializers.IntegerField(read_only=True)
    symbol = serializers.CharField(read_only=True)
    timeframe = serializers.CharField(read_only=True)
    start_date = serializers.DateField(read_only=True)
    end_date = serializers.DateField(read_only=True)
    requested_at = serializers.DateTimeField(read_only=True)
    started_at = serializers.DateTimeField(read_only=True, allow_null=True)
    completed_at = serializers.DateTimeField(read_only=True, allow_null=True)
    worker_id = serializers.CharField(read_only=True, allow_null=True)
    execution_id = serializers.IntegerField(read_only=True, allow_null=True)
    execution_status = serializers.CharField(read_only=True, allow_null=True)
    execution_run_identifier = serializers.CharField(
        read_only=True, allow_null=True
    )
    execution_started_at = serializers.DateTimeField(
        read_only=True, allow_null=True
    )
    execution_completed_at = serializers.DateTimeField(
        read_only=True, allow_null=True
    )
    execution_duration_seconds = serializers.FloatField(
        read_only=True, allow_null=True
    )


class BacktestSummarySerializer(serializers.Serializer):
    """Nested summary output within results response."""

    total_trades = serializers.IntegerField(read_only=True)
    win_rate = serializers.FloatField(read_only=True, allow_null=True)
    profit_factor = serializers.FloatField(read_only=True, allow_null=True)
    max_drawdown = serializers.FloatField(read_only=True, allow_null=True)
    sharpe_ratio = serializers.FloatField(read_only=True, allow_null=True)
    expectancy = serializers.FloatField(read_only=True, allow_null=True)


class PromotionCandidateSerializer(serializers.Serializer):
    """
    Output for promotion candidate data (Packet B — B7).

    Maps review_status → state for the approved API shape.
    """

    id = serializers.IntegerField(read_only=True)
    backtest_execution_id = serializers.IntegerField(read_only=True)
    state = serializers.CharField(source="review_status", read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)


class PromotionCandidateReviewSerializer(serializers.Serializer):
    """
    Input for POST /api/backtests/candidates/{id}/review/ (Packet C1).

    Accepts a review decision and optional notes.
    """

    decision = serializers.ChoiceField(
        choices=["approved", "rejected"],
        required=True,
    )
    notes = serializers.CharField(
        required=False, allow_blank=True, default=""
    )


class BacktestResultsResponseSerializer(serializers.Serializer):
    """Output for GET /api/backtests/jobs/{id}/results/."""

    job_id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    summary_available = serializers.BooleanField(read_only=True)
    summary = BacktestSummarySerializer(read_only=True, allow_null=True)
    execution_id = serializers.IntegerField(read_only=True, allow_null=True)
    execution_status = serializers.CharField(read_only=True, allow_null=True)
    artifact_count = serializers.IntegerField(read_only=True)
    promotion_candidate = PromotionCandidateSerializer(
        read_only=True, allow_null=True
    )


class BacktestArtifactMetadataSerializer(serializers.Serializer):
    """Output for GET /api/backtests/jobs/{id}/artifacts/."""

    artifact_id = serializers.IntegerField(read_only=True)
    artifact_type = serializers.CharField(read_only=True)
    file_path = serializers.CharField(read_only=True)
    file_size = serializers.IntegerField(read_only=True, allow_null=True)
    checksum = serializers.CharField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)


# =========================================================================
# Legacy serializers (existing — unchanged)
# =========================================================================


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


# =============================================================================
# Windows Agent Backtest Serializers (MVP)
# =============================================================================


class WindowsBacktestRunRequestSerializer(serializers.Serializer):
    """
    Validates the request body for creating a Windows backtest job.
    """

    strategy_id = serializers.IntegerField(required=True)
    account_id = serializers.IntegerField(required=True)
    username = serializers.CharField(max_length=128, required=True)
    datadir = serializers.CharField(max_length=512, required=False, allow_blank=True, default="")
    symbol = serializers.CharField(max_length=32, required=True)
    timeframe = serializers.CharField(max_length=20, required=True)
    date_from = serializers.DateField(required=True)
    date_to = serializers.DateField(required=True)
    deposit = serializers.DecimalField(max_digits=20, decimal_places=2, required=True)
    leverage = serializers.IntegerField(required=True)
    mode = serializers.CharField(max_length=32, required=False, default="real_ticks")

    def validate_strategy_id(self, value):
        """Ensure strategy exists."""
        if not Strategy.objects.filter(id=value).exists():
            raise serializers.ValidationError(f"Strategy with id {value} does not exist.")
        return value

    def validate_account_id(self, value):
        """Ensure account exists."""
        if not TradingAccount.objects.filter(id=value).exists():
            raise serializers.ValidationError(f"TradingAccount with id {value} does not exist.")
        return value

    def validate(self, attrs):
        """Cross-field validation."""
        if attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError({
                "date_from": "date_from must be before or equal to date_to."
            })
        if attrs["deposit"] <= 0:
            raise serializers.ValidationError({
                "deposit": "deposit must be a positive number."
            })
        if attrs["leverage"] <= 0:
            raise serializers.ValidationError({
                "leverage": "leverage must be a positive integer."
            })
        return attrs


class WindowsBacktestJobSerializer(serializers.ModelSerializer):
    """
    Serializes WindowsBacktestJob for API responses.
    """

    strategy_id = serializers.PrimaryKeyRelatedField(
        source="strategy",
        read_only=True,
    )
    account_id = serializers.PrimaryKeyRelatedField(
        source="account",
        read_only=True,
    )

    class Meta:
        model = WindowsBacktestJob
        fields = [
            "id",
            "job_id",
            "owner",
            "strategy_id",
            "account_id",
            "username",
            "datadir",
            "symbol",
            "timeframe",
            "date_from",
            "date_to",
            "deposit",
            "leverage",
            "mode",
            "state",
            "status_json",
            "result_json",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "job_id",
            "owner",
            "strategy_id",
            "account_id",
            "username",
            "datadir",
            "symbol",
            "timeframe",
            "date_from",
            "date_to",
            "deposit",
            "leverage",
            "mode",
            "state",
            "status_json",
            "result_json",
            "created_at",
            "updated_at",
        ]


class AIBacktestRecommendationRequestSerializer(serializers.Serializer):
    """
    Validates the request body for AI backtest recommendations.
    """

    job_id = serializers.CharField(max_length=64, required=True)

    def validate_job_id(self, value):
        """Ensure job exists."""
        if not WindowsBacktestJob.objects.filter(job_id=value).exists():
            raise serializers.ValidationError(f"WindowsBacktestJob with job_id '{value}' does not exist.")
        return value