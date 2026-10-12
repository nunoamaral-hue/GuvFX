"""Staff admin for broker configuration (Django admin is auth-gated staff-only; never publicly exposed).

BrokerProfile is editable by staff (the broker administration interface); BrokerProfileAudit is strictly read-only
(the append-only configuration history). No credentials are shown here — profiles hold only config.
"""
from __future__ import annotations

from django.contrib import admin

from .models import BrokerProfile, BrokerProfileAudit


@admin.register(BrokerProfile)
class BrokerProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "approved_sender_domains", "trusted_authserv_ids", "auth_policy",
                    "parser_name", "parser_version", "is_active", "updated_at")
    list_filter = ("is_active", "auth_policy")
    search_fields = ("name", "slug", "official_domains", "approved_sender_domains", "server_names")
    readonly_fields = ("created_at", "updated_at")


@admin.register(BrokerProfileAudit)
class BrokerProfileAuditAdmin(admin.ModelAdmin):
    list_display = ("broker_profile", "action", "changed_by", "created_at")
    list_filter = ("action",)
    search_fields = ("broker_profile__name", "changed_by")
    readonly_fields = ("broker_profile", "action", "snapshot", "changed_by", "created_at")

    def has_add_permission(self, request):          # append-only history — written by the app, never by hand
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
