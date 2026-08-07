"""Minimal read-only staff admin for HostedMt5Workspace. Inert while the subsystem is DARK (no rows
exist). Read-only: the workspace lifecycle is driven by services, never hand-edited in admin."""
from django.contrib import admin

from .models import HostedMt5Workspace


@admin.register(HostedMt5Workspace)
class HostedMt5WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("trading_account_id", "state", "active_account_match",
                    "supervision_state", "last_observed_at", "updated_at")
    list_filter = ("state", "supervision_state", "active_account_match")
    search_fields = ("trading_account__account_number", "workspace_uuid")
    readonly_fields = ("workspace_uuid", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
