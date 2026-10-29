"""trade_copier.views — READ-ONLY, owner-scoped Copy Trading API for the customer UI (PR C).

Every queryset is scoped to the authenticated owner (staff bypass, mirroring trading.TradeViewSet);
a non-owned account id returns 404 (existence is never leaked). Nothing here mutates or executes.
Gated by the founder/operator COHORT gate ``copier_ui_enabled_for(request.user)`` (default-DENY,
allow-list only): a non-cohort caller gets ``enabled: false`` with no rows (fail-closed, approved
unavailable response), so the feature stays dark for everyone outside the allow-list.
"""
from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from trade_copier.config_api import follower_config_readiness
from trade_copier.flags import copier_ui_enabled_for, execution_banner, execution_status
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

# The safety banner the founder UI must always display is DERIVED LIVE from the execute flags via
# trade_copier.flags.execution_banner() — NOT a constant — so it reflects the real posture and would stop
# reading "EXECUTION OFF" the instant DEMO execution were armed. Surfaced on every response so the client
# cannot render the monitoring UI without it. (Today, with the producer off, it is "SHADOW — EXECUTION OFF".)
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
    """GET /api/copy-trading/status/ — capability probe for the frontend (is the UI visible to me?).

    ``enabled`` reflects the founder/operator COHORT gate for the CALLING user (default-DENY): a
    non-cohort caller gets ``enabled: false`` so the frontend hides the Copy Trading entry. The
    permanent ``SHADOW — EXECUTION OFF`` banner is returned to every caller (cohort or not)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        status = execution_status()
        return Response({"enabled": copier_ui_enabled_for(request.user),
                         "banner": execution_banner(), "execution": status["execution"],
                         "execution_status": status, "limitations": _LIMITATIONS})


class CopyTradingMastersView(APIView):
    """GET /api/copy-trading/masters/ — the caller's own designated copier master accounts."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not copier_ui_enabled_for(request.user):
            return Response({"enabled": False, "banner": execution_banner(), "masters": []})
        masters = _owned_masters(request).order_by("id")
        return Response({"enabled": True, "banner": execution_banner(),
                         "masters": CopierMasterSerializer(masters, many=True).data})


class CopyTradingOverviewView(APIView):
    """GET /api/copy-trading/accounts/<account_id>/overview/ — the full read-only dashboard for one
    master account: its config, followers, recent observed events and recent replication outcomes."""
    permission_classes = [IsAuthenticated]

    def get(self, request, account_id: int):
        if not copier_ui_enabled_for(request.user):
            return Response({"enabled": False, "banner": execution_banner(), "master": None, "followers": [],
                             "recent_events": [], "recent_replications": [], "limitations": _LIMITATIONS})
        account = _owned_account(request, account_id)
        if account is None:
            return Response({"detail": "not_found"}, status=404)
        master = (CopierMaster.objects
                  .select_related("trading_account", "trading_account__broker_server")
                  .filter(trading_account=account).first())
        if master is None:
            # Owned account that is not a copier master yet — honest "not configured" state.
            return Response({"enabled": True, "banner": execution_banner(), "master": None, "followers": [],
                             "recent_events": [], "recent_replications": [], "limitations": _LIMITATIONS})
        followers = list(master.followers
                         .select_related("follower_account", "follower_account__broker_server")
                         .order_by("id"))
        followers_data = CopierFollowerSerializer(followers, many=True).data
        # Carry the SAME fail-closed per-follower readiness the founder config page computes onto the
        # monitoring overview, so a follower whose configured lot its broker cannot execute (e.g. a
        # Pepperstone follower where 0.01 is below the broker volume_min) is shown "not execution-ready
        # — <reason>" here too, instead of a bare sizing line that could read as set-up-and-ready. This
        # asserts only the NEGATIVE (blocked + reason); it never renders a positive "execution-ready" claim.
        for follower, data in zip(followers, followers_data):
            readiness = follower_config_readiness(master, follower)
            data["config_status"] = readiness["config_status"]
            data["config_blocked_reason"] = readiness["config_blocked_reason"]
            data["follower_current"] = readiness["follower_current"]
        events = master.events.order_by("-sequence")[:_EVENT_LIMIT]
        replications = (CopierReplication.objects
                        .filter(follower__master=master)
                        .select_related("master_event", "follower")
                        .order_by("-id")[:_REPLICATION_LIMIT])
        return Response({
            "enabled": True,
            "banner": execution_banner(),
            "execution_status": execution_status(),
            "master": CopierMasterSerializer(master).data,
            "followers": followers_data,
            "recent_events": MasterEventSerializer(events, many=True).data,
            "recent_replications": CopierReplicationSerializer(replications, many=True).data,
            "limitations": _LIMITATIONS,
        })
