from django.contrib import admin

from strategies.models import (
    Strategy,
    StrategyAssignment,
    StrategyChangeLog,
    StrategyRuntimeEvent,
    StrategyRuntimeState,
)


@admin.register(Strategy)
class StrategyAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "style", "market_type", "timeframe", "is_active", "created_at")
    list_filter = ("is_active", "style", "market_type")
    search_fields = ("name", "owner__username")


@admin.register(StrategyAssignment)
class StrategyAssignmentAdmin(admin.ModelAdmin):
    list_display = ("strategy", "account", "stage", "is_active", "created_at")
    list_filter = ("is_active", "stage")
    search_fields = ("strategy__name", "account__mt5_account_id")


@admin.register(StrategyChangeLog)
class StrategyChangeLogAdmin(admin.ModelAdmin):
    list_display = ("strategy", "source", "changed_by", "created_at")
    list_filter = ("source",)
    search_fields = ("strategy__name",)


@admin.register(StrategyRuntimeState)
class StrategyRuntimeStateAdmin(admin.ModelAdmin):
    list_display = (
        "assignment", "strategy_key", "symbol",
        "daily_r_pnl", "daily_trade_count", "consecutive_losses",
        "paused_until", "last_eval_at",
    )
    list_filter = ("strategy_key", "symbol")
    search_fields = ("strategy_key", "symbol")
    readonly_fields = ("created_at", "updated_at")


@admin.register(StrategyRuntimeEvent)
class StrategyRuntimeEventAdmin(admin.ModelAdmin):
    list_display = (
        "assignment", "strategy_key", "symbol",
        "event_type", "reason_code", "bar_close_time", "created_at",
    )
    list_filter = ("event_type", "strategy_key", "reason_code")
    search_fields = ("strategy_key", "symbol", "reason_code")
    readonly_fields = ("created_at",)
    date_hierarchy = "created_at"
