"""trade_copier.admin — read-mostly admin for the copier observer (PR A).

``CopierMaster`` is editable only for its ``observe_enabled`` toggle (staff worklist control). The
raw-evidence models (polls, snapshots, events) are registered READ-ONLY — they are append-only
observation and must never be edited through the admin (``.claude/rules/data.md``)."""
from django.contrib import admin

from trade_copier.models import (
    CopierFollower,
    CopierFollowerAudit,
    CopierLiveAuthorization,
    CopierLiveKillSwitch,
    CopierMaster,
    CopierReplication,
    CopierSymbolMap,
    MasterEvent,
    MasterObservationPoll,
    MasterPositionSnapshot,
)


@admin.register(CopierMaster)
class CopierMasterAdmin(admin.ModelAdmin):
    list_display = ("id", "trading_account", "observe_enabled", "last_observed_at", "last_poll")
    list_filter = ("observe_enabled",)
    raw_id_fields = ("trading_account", "last_poll")
    readonly_fields = ("last_observed_at", "last_poll", "created_at", "updated_at")


class _ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):  # noqa: D401
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MasterObservationPoll)
class MasterObservationPollAdmin(_ReadOnlyAdmin):
    list_display = ("id", "master", "observed_at", "position_count", "observed_login",
                    "observed_trade_mode", "transport_reason")
    list_filter = ("observed_trade_mode",)
    search_fields = ("observed_login", "observed_server")


@admin.register(MasterPositionSnapshot)
class MasterPositionSnapshotAdmin(_ReadOnlyAdmin):
    list_display = ("id", "poll", "position_ticket", "symbol", "side", "volume", "sl", "tp")
    search_fields = ("position_ticket", "symbol")


@admin.register(MasterEvent)
class MasterEventAdmin(_ReadOnlyAdmin):
    list_display = ("id", "master", "sequence", "event_type", "position_ticket", "symbol",
                    "volume_delta", "observed_at")
    list_filter = ("event_type",)
    search_fields = ("position_ticket", "symbol")


# ── PR B — follower side ──
@admin.register(CopierFollower)
class CopierFollowerAdmin(admin.ModelAdmin):
    list_display = ("id", "master", "follower_account", "enabled", "sizing_mode",
                    "disable_policy", "sync_status")
    list_filter = ("enabled", "sizing_mode", "disable_policy", "sync_status")
    raw_id_fields = ("master", "follower_account", "strategy_assignment", "unprotected_close_authorized_by")
    readonly_fields = ("created_at", "updated_at")


@admin.register(CopierSymbolMap)
class CopierSymbolMapAdmin(admin.ModelAdmin):
    list_display = ("id", "master_broker", "master_symbol", "follower_broker", "follower_symbol",
                    "verified", "verified_at")
    list_filter = ("verified",)
    search_fields = ("master_broker", "master_symbol", "follower_broker", "follower_symbol")


@admin.register(CopierReplication)
class CopierReplicationAdmin(_ReadOnlyAdmin):
    list_display = ("id", "master_event", "follower", "requested_op", "status", "reason",
                    "target_volume", "follower_symbol")
    list_filter = ("status", "requested_op")
    search_fields = ("follower_symbol", "reason")


@admin.register(CopierFollowerAudit)
class CopierFollowerAuditAdmin(_ReadOnlyAdmin):
    list_display = ("id", "follower", "action", "actor", "at")
    list_filter = ("action",)


@admin.register(CopierLiveAuthorization)
class CopierLiveAuthorizationAdmin(_ReadOnlyAdmin):
    """READ-ONLY (ADR-0057): a LIVE authorization is created ONLY by the staff-gated
    ``grant_copier_live_authorization`` command — never through the admin. Logins are shown as stored
    identifiers (not secrets), but no credential is ever displayed."""
    list_display = ("id", "user", "master_account", "follower_account", "source_symbol", "follower_symbol",
                    "max_volume", "max_concurrent_positions", "max_orders", "orders_consumed", "status",
                    "valid_from", "expires_at")
    list_filter = ("status",)
    search_fields = ("source_symbol", "follower_symbol", "reason")


@admin.register(CopierLiveKillSwitch)
class CopierLiveKillSwitchAdmin(_ReadOnlyAdmin):
    """READ-ONLY: engaged only by the ``copier_live_disarm`` command (audit preserved)."""
    list_display = ("id", "engaged", "engaged_at", "engaged_by", "reason")
    list_filter = ("engaged",)
