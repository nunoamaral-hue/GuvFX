"""trade_copier.serializers — READ-ONLY serializers for the Copy Trading customer UI (PR C).

All fields are read-only: the UI observes; it never mutates through these. Derived display fields
(broker, environment) are computed safely and never leak another tenant's data (the views own-scope
every queryset). LIVE follower execution is surfaced as permanently blocked.
"""
from __future__ import annotations

from rest_framework import serializers

from trade_copier.models import CopierFollower, CopierMaster, CopierReplication, MasterEvent


def _account_environment(account) -> str:
    try:
        from trading.account_policy import Environment, account_environment
        env = account_environment(account)
        return "demo" if env == Environment.DEMO else ("live" if env == Environment.LIVE else "unknown")
    except Exception:  # noqa: BLE001 — never fail a read over a classification edge
        return "unknown"


def _broker_label(account) -> str:
    bs = getattr(account, "broker_server", None)
    return str(getattr(bs, "server_name", "") or getattr(account, "broker_name", "") or "")


class MasterEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = MasterEvent
        fields = ("sequence", "event_type", "position_ticket", "symbol", "side",
                  "volume_before", "volume_after", "volume_delta",
                  "prev_sl", "new_sl", "prev_tp", "new_tp", "reason", "origin", "observed_at")
        read_only_fields = fields


class CopierFollowerSerializer(serializers.ModelSerializer):
    follower_account_id = serializers.IntegerField(source="follower_account.id", read_only=True)
    broker = serializers.SerializerMethodField()
    environment = serializers.SerializerMethodField()
    live_execution_blocked = serializers.SerializerMethodField()

    class Meta:
        model = CopierFollower
        fields = ("id", "follower_account_id", "broker", "environment", "enabled",
                  "sizing_mode", "fixed_lot", "multiplier", "risk_per_trade_pct",
                  "max_volume", "max_aggregate_exposure", "max_concurrent_positions",
                  "max_spread_points", "max_slippage_points",
                  "sltp_translation", "disable_policy",
                  "unprotected_close_enabled", "unprotected_close_authorization_version",
                  "sync_status", "last_outcome", "live_execution_blocked", "updated_at")
        read_only_fields = fields

    def get_broker(self, obj) -> str:
        return _broker_label(obj.follower_account)

    def get_environment(self, obj) -> str:
        return _account_environment(obj.follower_account)

    def get_live_execution_blocked(self, obj) -> bool:
        # LIVE follower execution is not authorized in this subsystem (RED); the UI always shows it
        # blocked for a live follower. A demo follower is executable only via the gated DEMO path.
        return _account_environment(obj.follower_account) == "live"


class CopierReplicationSerializer(serializers.ModelSerializer):
    event_sequence = serializers.IntegerField(source="master_event.sequence", read_only=True)
    event_type = serializers.CharField(source="master_event.event_type", read_only=True)
    follower_account_id = serializers.IntegerField(source="follower.follower_account_id", read_only=True)

    class Meta:
        model = CopierReplication
        fields = ("id", "event_sequence", "event_type", "follower_account_id", "requested_op",
                  "status", "reason", "target_volume", "follower_symbol",
                  "translated_sl", "translated_tp", "constraints_authoritative",
                  "sltp_translation_ok", "follower_ticket", "latency_ms", "created_at")
        read_only_fields = fields


class CopierMasterSerializer(serializers.ModelSerializer):
    account_id = serializers.IntegerField(source="trading_account.id", read_only=True)
    broker = serializers.SerializerMethodField()
    environment = serializers.SerializerMethodField()

    class Meta:
        model = CopierMaster
        fields = ("id", "account_id", "broker", "environment", "observe_enabled", "last_observed_at")
        read_only_fields = fields

    def get_broker(self, obj) -> str:
        return _broker_label(obj.trading_account)

    def get_environment(self, obj) -> str:
        return _account_environment(obj.trading_account)
