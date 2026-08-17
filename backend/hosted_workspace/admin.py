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

    def get_readonly_fields(self, request, obj=None):
        # Truly read-only (matches this admin's contract): the workspace lifecycle — and ESPECIALLY the arm
        # state (execution_enabled / execution_authorized_at / auto_arm_suppressed) — is driven ONLY by the
        # certified services, never hand-edited in admin. ADR-0047: execution_authorized_at may be set only by
        # the customer's owner-scoped authorize_workspace_execution, so a superuser must not be able to flip
        # execution_enabled (or forge an authorization) via the change form. Every concrete field is readonly.
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
