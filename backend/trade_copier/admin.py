"""trade_copier.admin — read-mostly admin for the copier observer (PR A).

``CopierMaster`` is editable only for its ``observe_enabled`` toggle (staff worklist control). The
raw-evidence models (polls, snapshots, events) are registered READ-ONLY — they are append-only
observation and must never be edited through the admin (``.claude/rules/data.md``)."""
from django.contrib import admin

from trade_copier.models import (
    CopierMaster,
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
