from decimal import Decimal
from rest_framework import serializers
from .models import ExecutionJob, TerminalNode


class TerminalNodeSerializer(serializers.ModelSerializer):
    computed_active_accounts = serializers.IntegerField(read_only=True)
    has_capacity = serializers.BooleanField(read_only=True)

    class Meta:
        model = TerminalNode
        fields = [
            "id",
            "hostname",
            "display_name",
            "status",
            "max_accounts",
            "active_accounts",
            "computed_active_accounts",
            "has_capacity",
            "last_heartbeat",
            "created_at",
            "updated_at",
        ]
        read_only_fields = (
            "id",
            "active_accounts",
            "computed_active_accounts",
            "has_capacity",
            "last_heartbeat",
            "created_at",
            "updated_at",
        )


class ExecutionJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionJob
        fields = "__all__"
        # EXEC-HARDEN-JOBS: order-defining fields (job_type, account, strategy,
        # assignment, terminal_node, payload) are read-only so they can never be
        # set or changed through the serializer by client input. ExecutionJobs are
        # created only through sanctioned, gated server-side paths; this serializer
        # shapes read/response output, not client-driven writes.
        read_only_fields = (
            "job_type",
            "account",
            "strategy",
            "assignment",
            "terminal_node",
            "payload",
            "status",
            "worker_id",
            "result",
            "error_message",
            "lease_expires_at",
            "recovered",
            "recovery_reason",
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


class DemoTradeJobRequestSerializer(serializers.Serializer):
    """
    Request serializer for creating a demo trade job.

    Safety: Symbol and side are validated, but actual execution uses
    hard-coded safety rails (EURUSD only, 0.01 lot, BUY only for demo).
    """
    account_id = serializers.IntegerField(
        help_text="TradingAccount ID (must be demo account owned by user)"
    )
    strategy_id = serializers.IntegerField(
        help_text="Strategy ID (must be owned by user)"
    )
    # Symbol is informational - backend enforces EURUSD only
    symbol = serializers.ChoiceField(
        choices=["EURUSD"],
        default="EURUSD",
        help_text="Symbol to trade (demo only supports EURUSD)"
    )
    # Side is informational - backend enforces BUY only for demo
    side = serializers.ChoiceField(
        choices=["BUY"],
        default="BUY",
        help_text="Trade direction (demo only supports BUY)"
    )
