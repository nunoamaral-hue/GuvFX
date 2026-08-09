"""ADR-0034 Workspace Delivery — the owner-scoped delivery-state API (read-only, DARK).

GET the RemoteApp delivery-state projection for the caller's OWN broker account. Read-only — there is NO
mutation endpoint (minting a delivery is the future onboarding wiring, gated). DARK (404) unless BOTH
``hosted_persistent_mt5_enabled()`` (master) AND ``hosted_mt5_remoteapp_enabled()`` (delivery) are ON — the
endpoint does not exist while the delivery subsystem is OFF. Non-staff read only their own account (404
otherwise, IDOR-safe); staff retain the read-only bypass (parity with the M3c state API). The payload is
entirely non-secret (``delivery_read_model.delivery_state_projection``): no credential, no Windows username,
no runtime path, no signed URL.
"""
from __future__ import annotations

from rest_framework import status as http
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from trading.models import TradingAccount

from hosted_workspace.delivery_read_model import delivery_state_projection
from hosted_workspace.flags import hosted_mt5_remoteapp_enabled, hosted_persistent_mt5_enabled
from hosted_workspace.models import HostedMt5Workspace


class HostedWorkspaceDeliveryStateView(APIView):
    """GET /api/hosted-workspace/delivery-state/?account_id=<id>"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # DARK gate FIRST — before any DB read. Requires BOTH master + delivery flags ON.
        if not (hosted_persistent_mt5_enabled() and hosted_mt5_remoteapp_enabled()):
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

        account_id = request.query_params.get("account_id")
        if not account_id:
            return Response({"detail": "account_id is required."}, status=http.HTTP_400_BAD_REQUEST)
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)
        # Out-of-range PK -> not found (fail closed): never let an oversized id reach the ORM lookup.
        if not (0 < account_id <= (1 << 63) - 1):
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

        is_staff = bool(getattr(request.user, "is_staff", False))
        # IDOR-safe owner scoping: a non-staff user may resolve only their OWN account.
        if is_staff:
            account = TradingAccount.objects.filter(id=account_id).first()
        else:
            account = TradingAccount.objects.filter(id=account_id, user=request.user).first()
        if account is None:
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

        workspace = (HostedMt5Workspace.objects
                     .select_related("workspace_node")
                     .filter(trading_account_id=account.id).first())
        if workspace is None:
            return Response({"detail": "No hosted workspace for this account."},
                            status=http.HTTP_404_NOT_FOUND)

        return Response(delivery_state_projection(workspace, staff=is_staff))
