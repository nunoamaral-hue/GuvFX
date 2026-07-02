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

    # E3-APPROVAL-RBAC: the approve/reject actions require the dedicated
    # ``review_signals`` permission — plain staff/admin access is NOT enough.
    # Django hides actions whose permission check fails; the service layer
    # re-enforces the same check fail-closed (defence in depth).
    def has_review_permission(self, request) -> bool:
        return services.can_review(getattr(request, "user", None))

    @admin.action(
        description="Approve (SHADOW — records decision only, NO order placed)",
        permissions=["review"],
    )
    def action_approve(self, request, queryset):
        n = 0
        try:
            for a in queryset.filter(status=PendingSignalApproval.Status.PENDING_APPROVAL):
                services.approve(a, reviewer=request.user, notes="Approved via admin (shadow).")
                n += 1
        except services.ReviewPermissionDenied:
            self.message_user(request, "Approval denied: review_signals permission required.",
                              level=messages.ERROR)
            return
        self.message_user(
            request, f"{n} signal(s) approved (shadow — no orders created).",
            level=messages.SUCCESS,
        )

    @admin.action(description="Reject selected pending signals", permissions=["review"])
    def action_reject(self, request, queryset):
        n = 0
        try:
            for a in queryset.filter(status=PendingSignalApproval.Status.PENDING_APPROVAL):
                services.reject(a, reviewer=request.user, notes="Rejected via admin.")
                n += 1
        except services.ReviewPermissionDenied:
            self.message_user(request, "Rejection denied: review_signals permission required.",
                              level=messages.ERROR)
            return
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
