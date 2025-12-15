from rest_framework import serializers

from .models import Strategy, StrategyAssignment, StrategyChangeLog


class StrategySerializer(serializers.ModelSerializer):
    class Meta:
        model = Strategy
        fields = [
            "id",
            "name",
            "description",
            "style",
            "market_type",
            "symbol_universe",
            "timeframe",
            "edge_type",
            "edge_rationale",
            "risk_per_trade_pct",
            "sizing_mode",
            "fixed_lot_size",
            "max_drawdown_pct",
            "magic_number",
            "is_active",
            # NEW fields:
            "ma_fast_period",
            "ma_slow_period",
            "ma_type",
            "auto_optimize_by_ai",
            "indicator_blocks",
            "entry_rules",
            "sl_rules",
            "tp_rules",
            "filters",
            "trade_management",
            "risk_limits",
            "plan_meta",
            # Notes/logic:
            "entry_logic",
            "exit_logic",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data):
        owner = self.context["request"].user
        return Strategy.objects.create(owner=owner, **validated_data)

    def validate(self, attrs):
        errors = {}

        risk_per_trade = attrs.get("risk_per_trade_pct")
        if risk_per_trade is not None and (risk_per_trade <= 0 or risk_per_trade > 10):
            errors["risk_per_trade_pct"] = (
                "Risk per trade must be between 0 and 10%."
            )

        sl_rules = attrs.get("sl_rules") or {}
        sl_method = sl_rules.get("method")
        valid_sl_methods = {"SWING_HIGH_LOW", "FIXED_PIPS", "ATR_MULTIPLE"}
        if sl_method and sl_method not in valid_sl_methods:
            errors["sl_rules"] = "Invalid stop-loss method."

        tp_rules = attrs.get("tp_rules") or {}
        tp_primary = tp_rules.get("primary")
        valid_tp_methods = {"FIXED_RR", "LEVEL_BASED", "TRAILING"}
        if tp_primary and tp_primary not in valid_tp_methods:
            errors["tp_rules"] = "Invalid take-profit method."

        trade_management = attrs.get("trade_management") or {}
        move_to_breakeven = trade_management.get("move_to_breakeven") or {}
        pyramiding = trade_management.get("pyramiding") or {}

        if move_to_breakeven.get("at_r_multiple") is not None and move_to_breakeven[
            "at_r_multiple"
        ] < 0:
            errors["trade_management"] = (
                "Breakeven R multiple cannot be negative."
            )

        if pyramiding.get("max_additions") is not None and pyramiding[
            "max_additions"
        ] < 0:
            errors["trade_management"] = (
                "Pyramiding additions must be 0 or more."
            )

        filters = attrs.get("filters") or {}
        max_trades = filters.get("max_trades_per_day")
        if max_trades is not None and max_trades < 0:
            errors["filters"] = "Max trades per day cannot be negative."

        news_filter = filters.get("news_filter") or {}
        if news_filter.get("pre_event_minutes") is not None and news_filter[
            "pre_event_minutes"
        ] < 0:
            errors["filters"] = "News pre-event buffer must be 0 or more minutes."
        if news_filter.get("post_event_minutes") is not None and news_filter[
            "post_event_minutes"
        ] < 0:
            errors["filters"] = "News post-event buffer must be 0 or more minutes."

        risk_limits = attrs.get("risk_limits") or {}
        if risk_limits.get("daily_max_loss_r") is not None and risk_limits[
            "daily_max_loss_r"
        ] < 0:
            errors["risk_limits"] = "Daily max loss (R) must be >= 0."
        if risk_limits.get("weekly_max_loss_r") is not None and risk_limits[
            "weekly_max_loss_r"
        ] < 0:
            errors["risk_limits"] = "Weekly max loss (R) must be >= 0."
        max_open = risk_limits.get("max_open_risk_pct")
        if max_open is not None and (max_open < 0 or max_open > 100):
            errors["risk_limits"] = "Max open risk percentage must be between 0 and 100."

        plan_meta = attrs.get("plan_meta") or {}
        psychology = plan_meta.get("psychology_rules") or {}
        if psychology.get("after_big_win_r") is not None and psychology[
            "after_big_win_r"
        ] < 0:
            errors["plan_meta"] = "After big win R must be 0 or greater."
        if psychology.get("reduced_risk_per_trade_pct") is not None and psychology[
            "reduced_risk_per_trade_pct"
        ] < 0:
            errors["plan_meta"] = "Reduced risk per trade percentage must be >= 0."

        if errors:
            raise serializers.ValidationError(errors)

        return attrs


class StrategyAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = StrategyAssignment
        fields = [
            "id",
            "strategy",
            "account",
            "is_active",
            "risk_per_trade_override_pct",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

class StrategyChangeLogSerializer(serializers.ModelSerializer):
    changed_by_email = serializers.EmailField(
        source="changed_by.email", read_only=True, allow_null=True
    )

    class Meta:
        model = StrategyChangeLog
        fields = [
            "id",
            "source",
            "changed_by_email",
            "created_at",
            "before_settings",
            "after_settings",
        ]
