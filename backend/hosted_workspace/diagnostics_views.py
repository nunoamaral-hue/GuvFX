"""hosted_workspace.diagnostics_views — WHY-not-CONNECTED diagnosis read API (DARK, read-only).

    GET /api/hosted-workspace/diagnostics/   → per-workspace corroboration diagnosis (+ staff fleet metrics)

404-invisible while the hosted subsystem is DARK (``hosted_persistent_mt5_enabled()`` OFF), decided BEFORE any
DB read. STRICTLY read-only: it calls only the pure ``corroboration_diagnosis`` helpers (no host contact, no
workspace mutation). SCOPING:

  * a STAFF caller may see the whole fleet (bounded) + the fail-open health metrics, or one account via an
    optional ``?account_id=`` / ``?workspace_uuid=`` selector;
  * a NON-STAFF caller sees ONLY their own workspace's diagnosis (owner-scoped via the immutable
    ``trading_account.user`` binding). A selector that is not theirs, or no workspace, yields 404 — a non-owner
    learns nothing (IDOR-safe).

The payload is secret-free (reason codes, booleans, enum codes, coarse ages) — never a credential, full login,
username or path. Nothing here arms execution, mutates delivery/observer state, or places an order.
"""
from __future__ import annotations

from rest_framework import status as http
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from hosted_workspace.corroboration_diagnosis import diagnose_workspace, observation_health_metrics
from hosted_workspace.flags import hosted_persistent_mt5_enabled
from hosted_workspace.models import HostedMt5Workspace

_NOT_FOUND = Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

# Bound on the staff fleet listing so the response stays finite (DARK read-only diagnostics).
_FLEET_ROW_LIMIT = 200


def _selector(request):
    """Optional explicit account selector (query params): ``workspace_uuid`` or ``account_id``."""
    wu = str(request.query_params.get("workspace_uuid", "") or "").strip() or None
    aid = request.query_params.get("account_id")
    try:
        aid = int(aid) if aid not in (None, "") else None
    except (TypeError, ValueError):
        aid = None
    return wu, aid


class HostedWorkspaceDiagnosticsView(APIView):
    """GET the corroboration diagnosis. Staff → fleet (+ metrics) or a selected account; non-staff → own only."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # 404 BEFORE any DB read while the subsystem is dark.
        if not hosted_persistent_mt5_enabled():
            return _NOT_FOUND

        is_staff = bool(getattr(request.user, "is_staff", False))
        wu, aid = _selector(request)

        if is_staff:
            qs = HostedMt5Workspace.objects.select_related("trading_account__broker_server")
            if wu or aid is not None:
                ws = (qs.filter(workspace_uuid=wu).first() if wu
                      else qs.filter(trading_account_id=aid).first())
                if ws is None:
                    return _NOT_FOUND
                return Response({"diagnosis": diagnose_workspace(ws)})
            rows = list(qs.order_by("id")[:_FLEET_ROW_LIMIT])
            return Response({
                "rows": [diagnose_workspace(ws) for ws in rows],
                "limit": _FLEET_ROW_LIMIT,
                "metrics": observation_health_metrics(),
            })

        # Non-staff: owner-scoped to the caller's OWN workspace(s). A selector that is not theirs → 404.
        owned = HostedMt5Workspace.objects.filter(
            trading_account__user=request.user).select_related("trading_account__broker_server")
        if wu:
            ws = owned.filter(workspace_uuid=wu).first()
        elif aid is not None:
            ws = owned.filter(trading_account_id=aid).first()
        else:
            # No selector: only unambiguous when the caller owns exactly one workspace (no arbitrary pick).
            ws = owned.first() if owned.count() == 1 else None
        if ws is None:
            return _NOT_FOUND
        return Response({"diagnosis": diagnose_workspace(ws)})
