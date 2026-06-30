from django.contrib import admin, messages

from .models import (
    TerminalNode,
    ExecutionJob,
    WorkerIdentity,
    ExecutionControl,
    ProposedSignalOrder,
    ProposalAuditEvent,
    SignalSourceConfig,
    SignalExecutionPlan,
    ProposedOrderLeg,
    PlanAuditEvent,
)


@admin.register(TerminalNode)
class TerminalNodeAdmin(admin.ModelAdmin):
    list_display = (
        "hostname",
        "display_name",
        "status",
        "max_accounts",
        "active_accounts",
        "last_heartbeat",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("hostname", "display_name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ExecutionJob)
class ExecutionJobAdmin(admin.ModelAdmin):
    list_display = ("id", "job_type", "account", "status", "worker_id", "terminal_node", "created_at")
    list_filter = ("job_type", "status")
    search_fields = ("worker_id",)
    readonly_fields = ("created_at", "started_at", "finished_at")
    raw_id_fields = ("account", "strategy", "assignment", "terminal_node", "created_by")


@admin.register(WorkerIdentity)
class WorkerIdentityAdmin(admin.ModelAdmin):
    list_display = ("worker_id", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("worker_id",)
    readonly_fields = ("created_at",)


# =============================================================================
# EXEC-E1a — execution control + proposals (no order surface)
# =============================================================================


@admin.register(ExecutionControl)
class ExecutionControlAdmin(admin.ModelAdmin):
    """Functional kill switch + signal-specific disable. Toggled via actions."""

    list_display = (
        "pk",
        "kill_switch_engaged",
        "signal_proposals_enabled",
        "reason",
        "updated_by",
        "updated_at",
    )
    readonly_fields = ("updated_at",)
    actions = (
        "engage_kill_switch_action",
        "release_kill_switch_action",
        "disable_proposals_action",
        "enable_proposals_action",
    )

    def has_add_permission(self, request):
        # Singleton — created on demand via get_solo().
        return ExecutionControl.objects.count() == 0

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Engage kill switch (block all signal proposals)")
    def engage_kill_switch_action(self, request, queryset):
        from .signal_proposals import engage_kill_switch

        engage_kill_switch(actor=request.user, reason="engaged via admin")
        self.message_user(request, "Kill switch engaged.", level=messages.WARNING)

    @admin.action(description="Release kill switch")
    def release_kill_switch_action(self, request, queryset):
        from .signal_proposals import release_kill_switch

        release_kill_switch(actor=request.user, reason="released via admin")
        self.message_user(request, "Kill switch released.", level=messages.SUCCESS)

    @admin.action(description="Disable signal proposals")
    def disable_proposals_action(self, request, queryset):
        from .signal_proposals import set_signal_proposals_enabled

        set_signal_proposals_enabled(False, actor=request.user, reason="disabled via admin")
        self.message_user(request, "Signal proposals disabled.", level=messages.WARNING)

    @admin.action(description="Enable signal proposals")
    def enable_proposals_action(self, request, queryset):
        from .signal_proposals import set_signal_proposals_enabled

        set_signal_proposals_enabled(True, actor=request.user, reason="enabled via admin")
        self.message_user(request, "Signal proposals enabled.", level=messages.SUCCESS)


@admin.register(ProposedSignalOrder)
class ProposedSignalOrderAdmin(admin.ModelAdmin):
    """Read-mostly. Proposals are created ONLY via the safety-gated bridge."""

    list_display = (
        "id",
        "status",
        "direction",
        "symbol",
        "lot_size",
        "is_demo",
        "account",
        "created_at",
    )
    list_filter = ("status", "direction", "is_demo", "symbol")
    search_fields = ("symbol", "account__account_number")
    raw_id_fields = ("approval", "account", "proposed_by")
    readonly_fields = (
        "approval",
        "account",
        "symbol",
        "direction",
        "entry",
        "stop_loss",
        "take_profit",
        "lot_size",
        "risk_per_trade_pct",
        "is_demo",
        "account_environment",
        "proposed_by",
        "created_at",
    )

    def has_add_permission(self, request):
        # No direct creation — must go through propose_order_from_approval so the
        # demo/kill-switch/allowlist/cap checks cannot be bypassed.
        return False


@admin.register(ProposalAuditEvent)
class ProposalAuditEventAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "proposal", "approval", "actor", "created_at")
    list_filter = ("event",)
    raw_id_fields = ("proposal", "approval", "actor")
    readonly_fields = ("event", "proposal", "approval", "actor", "detail", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# =============================================================================
# EXEC-E1b — demo execution plans (no order surface)
# =============================================================================


@admin.register(SignalSourceConfig)
class SignalSourceConfigAdmin(admin.ModelAdmin):
    """Per-source auto-demo arming. The ONLY editable execution-planning surface."""

    list_display = (
        "source",
        "auto_demo_execution_enabled",
        "total_lot_target",
        "updated_by",
        "updated_at",
    )
    list_filter = ("auto_demo_execution_enabled",)
    readonly_fields = ("updated_at",)


class ProposedOrderLegInline(admin.TabularInline):
    model = ProposedOrderLeg
    extra = 0
    can_delete = False
    readonly_fields = (
        "leg_index", "take_profit", "stop_loss", "lot_size", "order_type",
        "status", "hold_reason", "created_at",
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SignalExecutionPlan)
class SignalExecutionPlanAdmin(admin.ModelAdmin):
    """Read-only. Plans are created ONLY via the safety-gated planner."""

    list_display = (
        "id", "status", "direction", "symbol", "total_lot", "is_demo",
        "account", "hold_reason", "created_at",
    )
    list_filter = ("status", "direction", "is_demo", "symbol", "source")
    search_fields = ("symbol", "message_id", "account__account_number")
    raw_id_fields = ("approval", "account", "proposed_by")
    inlines = (ProposedOrderLegInline,)
    readonly_fields = (
        "approval", "account", "source", "chat_id", "message_id", "symbol",
        "direction", "entry", "stop_loss", "order_type", "total_lot", "is_demo",
        "account_environment", "signal_timestamp", "status", "hold_reason",
        "proposed_by", "created_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(ProposedOrderLeg)
class ProposedOrderLegAdmin(admin.ModelAdmin):
    list_display = (
        "id", "plan", "leg_index", "take_profit", "lot_size", "order_type",
        "status", "created_at",
    )
    list_filter = ("status", "order_type")
    raw_id_fields = ("plan",)
    readonly_fields = (
        "plan", "leg_index", "take_profit", "stop_loss", "lot_size",
        "order_type", "status", "hold_reason", "created_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(PlanAuditEvent)
class PlanAuditEventAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "plan", "leg", "approval", "actor", "created_at")
    list_filter = ("event",)
    raw_id_fields = ("plan", "leg", "approval", "actor")
    readonly_fields = ("event", "plan", "leg", "approval", "actor", "detail", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
