"""ADR-0034 / M3c — the owner-scoped Hosted Workspace state API (read-only, DARK).

GET the canonical M3c workspace-state projection for the caller's OWN broker account. Read-only. DARK (404)
while ``hosted_persistent_mt5_enabled()`` is OFF — the endpoint does not exist yet. Non-staff users may read
only their own account (404 otherwise, IDOR-safe) and receive the customer-safe projection; staff receive the
same projection plus operator-only, still-secret-free provenance. The payload is entirely non-secret
(``read_model.workspace_state_projection``): no credential, no full login, no attach path, no diagnostics. No
privilege expansion beyond the existing staff-bypass convention.
"""
from __future__ import annotations

from rest_framework import status as http
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from trading.models import TradingAccount

from hosted_workspace.flags import hosted_persistent_mt5_enabled
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.read_model import workspace_state_projection


class HostedWorkspaceStateView(APIView):
    """GET /api/hosted-workspace/workspace-state/?account_id=<id>"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # DARK gate FIRST — before any DB read — so the endpoint is invisible while the subsystem is OFF.
        if not hosted_persistent_mt5_enabled():
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
                     .filter(trading_account_id=account.id)
                     .prefetch_related("transitions").first())
        if workspace is None:
            # The account exists and is owned by the caller, but has no hosted workspace provisioned.
            return Response({"detail": "No hosted workspace for this account."},
                            status=http.HTTP_404_NOT_FOUND)

        return Response(workspace_state_projection(workspace, staff=is_staff))
