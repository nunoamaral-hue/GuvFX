"""trade_copier.views — READ-ONLY, owner-scoped Copy Trading API for the customer UI (PR C).

Every queryset is scoped to the authenticated owner (staff bypass, mirroring trading.TradeViewSet);
a non-owned account id returns 404 (existence is never leaked). Nothing here mutates or executes.
Gated by ``copier_ui_enabled()``: when OFF the API reports ``enabled: false`` with no rows, so the
feature stays dark in production until armed.
"""
from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from trade_copier.flags import copier_ui_enabled
from trade_copier.models import CopierMaster, CopierReplication
from trade_copier.serializers import (
    CopierFollowerSerializer,
    CopierMasterSerializer,
    CopierReplicationSerializer,
    MasterEventSerializer,
)
from trading.models import TradingAccount

# Permanent, visible execution limitations surfaced to the UI (ADR-0051): LIVE follower execution is
# not authorized; replication is observe/shadow (+ gated DEMO) only.
_LIMITATIONS = {
    "live_follower_execution": "blocked",
    "note": "LIVE follower execution is not enabled. Followers run in observation/shadow mode; "
            "real orders occur only on DEMO followers via a separately authorized path.",
}

_EVENT_LIMIT = 50
_REPLICATION_LIMIT = 100


def _owned_account(request, account_id):
    """The TradingAccount owned by the caller (staff bypass), or None (caller must 404, not leak)."""
    qs = TradingAccount.objects.select_related("broker_server").filter(id=account_id)
    if not request.user.is_staff:
        qs = qs.filter(user=request.user)
    return qs.first()


def _owned_masters(request):
    qs = CopierMaster.objects.select_related("trading_account", "trading_account__broker_server")
    if not request.user.is_staff:
        qs = qs.filter(trading_account__user=request.user)
    return qs


class CopyTradingStatusView(APIView):
    """GET /api/copy-trading/status/ — capability probe for the frontend (is the UI armed?)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"enabled": copier_ui_enabled(), "limitations": _LIMITATIONS})


class CopyTradingMastersView(APIView):
    """GET /api/copy-trading/masters/ — the caller's own designated copier master accounts."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not copier_ui_enabled():
            return Response({"enabled": False, "masters": []})
        masters = _owned_masters(request).order_by("id")
        return Response({"enabled": True, "masters": CopierMasterSerializer(masters, many=True).data})


class CopyTradingOverviewView(APIView):
    """GET /api/copy-trading/accounts/<account_id>/overview/ — the full read-only dashboard for one
    master account: its config, followers, recent observed events and recent replication outcomes."""
    permission_classes = [IsAuthenticated]

    def get(self, request, account_id: int):
        if not copier_ui_enabled():
            return Response({"enabled": False, "master": None, "followers": [],
                             "recent_events": [], "recent_replications": [], "limitations": _LIMITATIONS})
        account = _owned_account(request, account_id)
        if account is None:
            return Response({"detail": "not_found"}, status=404)
        master = (CopierMaster.objects
                  .select_related("trading_account", "trading_account__broker_server")
                  .filter(trading_account=account).first())
        if master is None:
            # Owned account that is not a copier master yet — honest "not configured" state.
            return Response({"enabled": True, "master": None, "followers": [],
                             "recent_events": [], "recent_replications": [], "limitations": _LIMITATIONS})
        followers = (master.followers
                     .select_related("follower_account", "follower_account__broker_server")
                     .order_by("id"))
        events = master.events.order_by("-sequence")[:_EVENT_LIMIT]
        replications = (CopierReplication.objects
                        .filter(follower__master=master)
                        .select_related("master_event", "follower")
                        .order_by("-id")[:_REPLICATION_LIMIT])
        return Response({
            "enabled": True,
            "master": CopierMasterSerializer(master).data,
            "followers": CopierFollowerSerializer(followers, many=True).data,
            "recent_events": MasterEventSerializer(events, many=True).data,
            "recent_replications": CopierReplicationSerializer(replications, many=True).data,
            "limitations": _LIMITATIONS,
        })
