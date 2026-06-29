"""Admin review surface for pending signal approvals (status-only; no orders)."""

from django.contrib import admin, messages

from . import services
from .models import PendingSignalApproval, SignalAuditEvent


@admin.register(PendingSignalApproval)
class PendingSignalApprovalAdmin(admin.ModelAdmin):
    list_display = (
        "id", "source", "message_id", "symbol", "direction", "entry",
        "stop_loss", "take_profit", "status", "reviewer", "reviewed_at", "created_at",
    )
    list_filter = ("status", "source", "direction")
    search_fields = ("message_id", "symbol")
    readonly_fields = ("created_at", "reviewed_at")
    actions = ("action_approve", "action_reject")

    @admin.action(description="Approve (SHADOW — records decision only, NO order placed)")
    def action_approve(self, request, queryset):
        n = 0
        for a in queryset.filter(status=PendingSignalApproval.Status.PENDING_APPROVAL):
            services.approve(a, reviewer=request.user, notes="Approved via admin (shadow).")
            n += 1
        self.message_user(
            request, f"{n} signal(s) approved (shadow — no orders created).",
            level=messages.SUCCESS,
        )

    @admin.action(description="Reject selected pending signals")
    def action_reject(self, request, queryset):
        n = 0
        for a in queryset.filter(status=PendingSignalApproval.Status.PENDING_APPROVAL):
            services.reject(a, reviewer=request.user, notes="Rejected via admin.")
            n += 1
        self.message_user(request, f"{n} signal(s) rejected.", level=messages.SUCCESS)


@admin.register(SignalAuditEvent)
class SignalAuditEventAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "event", "approval", "actor")
    list_filter = ("event",)
    readonly_fields = ("timestamp", "actor", "event", "approval", "detail")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
