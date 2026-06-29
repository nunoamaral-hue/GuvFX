from rest_framework import serializers


from .models import Strategy, StrategyAssignment, StrategyChangeLog
from trading.models import Trade


def validate_trendline_break_pocket_filters(filters: dict) -> dict:
    """
    Validate Trendline Break Pocket (Ali) strategy-specific filter parameters.
    Returns dict of validation errors (empty if valid).

    NOTE: This function lives in serializers.py to avoid circular import with views.py.
    """
    errors = {}

    if not isinstance(filters, dict):
        return errors

    template_slug = filters.get("template_slug", "")

    # Only validate if this is the Trendline Break Pocket template
    if template_slug != "trendline-break-pocket-ali":
        return errors

    # direction_mode validation
    direction_mode = filters.get("direction_mode")
    valid_direction_modes = {"both", "long", "short"}
    if direction_mode and direction_mode not in valid_direction_modes:
        errors["direction_mode"] = f"direction_mode must be one of: {', '.join(valid_direction_modes)}"

    # trendline_lookback_bars validation (must be >= 50)
    lookback = filters.get("trendline_lookback_bars")
    if lookback is not None:
        try:
            lookback_int = int(lookback)
            if lookback_int < 50:
                errors["trendline_lookback_bars"] = "trendline_lookback_bars must be >= 50"
        except (TypeError, ValueError):
            errors["trendline_lookback_bars"] = "trendline_lookback_bars must be an integer"

    # rr_target validation (must be > 0)
    rr_target = filters.get("rr_target")
    if rr_target is not None:
        try:
            rr_float = float(rr_target)
            if rr_float <= 0:
                errors["rr_target"] = "rr_target must be > 0"
        except (TypeError, ValueError):
            errors["rr_target"] = "rr_target must be a number"

    # Zone validation (low < high for each zone, zone_type valid)
    zones = filters.get("zones") or {}
    valid_zone_types = {"supply", "demand", "pivot"}
    for symbol, zone_list in zones.items():
        if not isinstance(zone_list, list):
            continue
        for i, zone in enumerate(zone_list):
            if not isinstance(zone, dict):
                continue
            low = zone.get("low")
            high = zone.get("high")
            zone_type = zone.get("zone_type")

            if low is not None and high is not None:
                try:
                    low_f = float(low)
                    high_f = float(high)
                    if low_f >= high_f:
                        errors[f"zones.{symbol}[{i}]"] = f"Zone low ({low_f}) must be < high ({high_f})"
                except (TypeError, ValueError):
                    errors[f"zones.{symbol}[{i}]"] = "Zone low/high must be numbers"

            if zone_type and zone_type not in valid_zone_types:
                errors[f"zones.{symbol}[{i}].zone_type"] = f"zone_type must be one of: {', '.join(valid_zone_types)}"

    return errors


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

    def update(self, instance, validated_data):
        """
        Override update to implement MERGE semantics for JSON fields like 'filters'.

        This prevents accidental data loss when PATCH includes only partial filters.
        For example, PATCH with {"filters": {"max_trades_per_day": 10}} should NOT
        wipe out existing zones - it should merge.

        Merge rules for 'filters':
        - Shallow merge: existing keys are kept unless overwritten by incoming data
        - If incoming data has a key, it replaces the existing key entirely
        - This allows updating individual fields while preserving zones
        """
        # Handle filters merge for PATCH requests
        if "filters" in validated_data and self.instance is not None:
            existing_filters = self.instance.filters or {}
            incoming_filters = validated_data.get("filters") or {}

            # Shallow merge: existing + incoming (incoming wins on conflict)
            merged_filters = {**existing_filters, **incoming_filters}
            validated_data["filters"] = merged_filters

        # Standard update for all other fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance

    def validate(self, attrs):
        errors = {}

        risk_per_trade = attrs.get("risk_per_trade_pct")
        if risk_per_trade is not None and (risk_per_trade <= 0 or risk_per_trade > 10):
            errors["risk_per_trade_pct"] = (
                "Risk per trade must be between 0 and 10%."
            )

        # Magic number: optional, must be non-negative integer if provided,
        # and must be unique per owner (to enable deterministic MT5 attribution).
        if "magic_number" in attrs:
            magic = attrs.get("magic_number")
            # Server-side lock (clear): once trades exist for this strategy, magic_number cannot be cleared.
            if magic is None and self.instance is not None:
                current_magic = self.instance.magic_number
                legacy_ids = [int(self.instance.pk)]
                if current_magic is not None:
                    try:
                        legacy_ids.append(int(current_magic))
                    except Exception:
                        pass

                has_trades = Trade.objects.filter(magic_number__in=legacy_ids).exists()
                if has_trades:
                    errors["magic_number"] = (
                        "Magic number is locked because trades already exist for this strategy."
                    )
            if magic is not None:
                try:
                    magic_int = int(magic)
                except Exception:
                    errors["magic_number"] = "Magic number must be an integer."
                else:
                    # Server-side lock: once trades exist for this strategy, magic_number cannot change.
                    if self.instance is not None:
                        current_magic = self.instance.magic_number
                        current_magic_int = int(current_magic) if current_magic is not None else None
                        incoming_magic_int = magic_int

                        # Only enforce if value is changing
                        if current_magic_int != incoming_magic_int:
                            legacy_ids = [int(self.instance.pk)]
                            if current_magic_int is not None:
                                legacy_ids.append(current_magic_int)

                            has_trades = Trade.objects.filter(magic_number__in=legacy_ids).exists()
                            if has_trades:
                                errors["magic_number"] = (
                                    "Magic number is locked because trades already exist for this strategy."
                                )
                    if magic_int < 0:
                        errors["magic_number"] = "Magic number must be a non-negative integer."
                    else:
                        owner = self.context["request"].user
                        qs = Strategy.objects.filter(owner=owner, magic_number=magic_int)
                        if self.instance is not None:
                            qs = qs.exclude(pk=self.instance.pk)
                        if qs.exists():
                            errors["magic_number"] = "This magic number is already used by another strategy. Choose a different value."

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

        # Template-specific validation: Trendline Break Pocket (Ali)
        tbp_errors = validate_trendline_break_pocket_filters(filters)
        if tbp_errors:
            # Merge errors; if filters already has an error, append
            if "filters" in errors:
                errors["filters"] = f"{errors['filters']} | {tbp_errors}"
            else:
                errors["filters"] = tbp_errors

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

    def validate(self, attrs):
        is_active = attrs.get("is_active", getattr(self.instance, "is_active", True))
        account = attrs.get("account", getattr(self.instance, "account", None))

        if is_active and account and not account.is_active:
            raise serializers.ValidationError("Cannot activate assignment on an inactive TradingAccount.")

        return attrs

    class Meta:
        model = StrategyAssignment
        fields = [
            "id",
            "strategy",
            "account",
            "is_active",
            "stage",
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
