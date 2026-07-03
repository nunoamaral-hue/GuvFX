"""Admin review surface for pending signal approvals (status-only; no orders)."""

from django.contrib import admin, messages

from . import services
from .models import (
    AcquiredMessage,
    ParserProfile,
    PendingSignalApproval,
    SignalAuditEvent,
    SignalProvider,
    SignalUpdate,
)


@admin.register(PendingSignalApproval)
class PendingSignalApprovalAdmin(admin.ModelAdmin):
    list_display = (
        "id", "source", "message_id", "symbol", "direction", "entry",
        "stop_loss", "take_profit", "status", "source_edited", "reviewer",
        "reviewed_at", "created_at",
    )
    # WAYOND-EDIT-MEDIA: surface the edited flag so reviewers can see/filter entries
    # whose source message was edited (verify entry/SL/TP before approving).
    list_filter = ("status", "source", "direction", "source_edited")
    search_fields = ("message_id", "symbol")
    # E3-APPROVAL-RBAC: the review decision fields are read-only in the change
    # form — status may ONLY change via the gated approve/reject actions (which
    # enforce review_signals + write the audit). Editing status directly in the
    # form would otherwise bypass the RBAC gate and the audit trail. source_edited
    # is a system-set provenance flag → read-only too.
    readonly_fields = ("status", "source_edited", "reviewer", "reviewed_at",
                       "review_notes", "created_at")
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


# --- SIGNAL-ACQUISITION-MVP admin (providers editable; ledger read-only) ------


@admin.register(ParserProfile)
class ParserProfileAdmin(admin.ModelAdmin):
    list_display = ("slug", "version", "active", "created_at")
    list_filter = ("active",)


@admin.register(SignalProvider)
class SignalProviderAdmin(admin.ModelAdmin):
    list_display = (
        "slug", "name", "status", "telegram_chat_id", "parser_profile",
        "acquisition_window_seconds", "last_signal_at", "updated_at",
    )
    list_filter = ("status", "parser_profile")
    search_fields = ("slug", "name", "telegram_chat_id")
    readonly_fields = ("last_signal_at", "watermark_last_message_id", "created_at", "updated_at")
    actions = ("action_arm", "action_pause")

    @admin.action(description="Arm selected providers (begin acquisition)")
    def action_arm(self, request, queryset):
        armed = 0
        for p in queryset:
            if p.telegram_chat_id:
                p.status = SignalProvider.Status.ARMED
                p.disabled_reason = ""
                p.save(update_fields=["status", "disabled_reason", "updated_at"])
                armed += 1
        self.message_user(request, f"{armed} provider(s) armed.", level=messages.SUCCESS)

    @admin.action(description="Pause selected providers (stop acquisition)")
    def action_pause(self, request, queryset):
        queryset.update(status=SignalProvider.Status.PAUSED)
        self.message_user(request, "Provider(s) paused.", level=messages.SUCCESS)


@admin.register(AcquiredMessage)
class AcquiredMessageAdmin(admin.ModelAdmin):
    """Read-only acquisition ledger."""

    list_display = ("acquired_at", "provider", "message_id", "outcome", "reason", "approval")
    list_filter = ("outcome", "provider")
    search_fields = ("message_id", "chat_id")
    readonly_fields = tuple(f.name for f in AcquiredMessage._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SignalUpdate)
class SignalUpdateAdmin(admin.ModelAdmin):
    """Read-only update ledger (recorded, not acted on in MVP)."""

    list_display = ("created_at", "provider", "message_id", "kind", "reply_to_message_id", "processed")
    list_filter = ("kind", "processed", "provider")
    readonly_fields = tuple(f.name for f in SignalUpdate._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
