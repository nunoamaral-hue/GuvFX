"""hosted_workspace.diagnostics_views — WHY-not-CONNECTED diagnosis read API (DARK, read-only, STAFF-ONLY).

    GET /api/hosted-workspace/diagnostics/   → per-workspace corroboration diagnosis (+ fleet metrics)

This is **OPERATIONAL DIAGNOSTICS, not a customer feature.** It exists so operators can determine WHY a hosted
workspace has not reached CONNECTED. It is gated by its OWN dedicated flag and is STAFF-ONLY.

Gating (checked in this order, BEFORE any DB read):
  1. ``hosted_connection_diagnostics_enabled()`` — a DEDICATED, independent flag, DEFAULT OFF. The route 404s
     while it is OFF **even when every other Hosted Workspace flag is ON** (production already runs the master /
     observation flags on, so this endpoint must never ride on those). This is the first and operative gate.
  2. ``request.user.is_staff`` — STAFF-ONLY. A non-staff caller (member) gets 404 even with the flag on;
     detailed corroboration diagnostics are not a member surface (product + information-disclosure need separate
     approval). No owner-scoped member branch exists here by design.

STRICTLY read-only: it calls only the pure ``corroboration_diagnosis`` helpers (no host contact, no workspace
mutation). The payload is secret-free (reason codes, booleans, enum codes, coarse ages) — never a credential,
full login, Windows username, or runtime/terminal path. Nothing here arms execution, mutates state, or places an
order.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status as http
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from hosted_workspace.corroboration_diagnosis import diagnose_workspace, observation_health_metrics
from hosted_workspace.flags import hosted_connection_diagnostics_enabled
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
    """GET the corroboration diagnosis — STAFF-ONLY, behind the dedicated diagnostics flag (both 404-invisible)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Gate 1 — the DEDICATED diagnostics flag, checked BEFORE anything else and independently of every other
        # Hosted Workspace flag. OFF (the default) ⇒ 404, even in production where the master/observation flags
        # are on. No DB access happens before this returns.
        if not hosted_connection_diagnostics_enabled():
            return _NOT_FOUND
        # Gate 2 — STAFF-ONLY. A non-staff caller (member) learns nothing even with the flag on; this is an
        # operational surface, not a customer feature. No owner-scoped member path exists here.
        if not bool(getattr(request.user, "is_staff", False)):
            return _NOT_FOUND

        qs = HostedMt5Workspace.objects.select_related("trading_account__broker_server")
        wu, aid = _selector(request)
        if wu or aid is not None:
            try:
                ws = (qs.filter(workspace_uuid=wu).first() if wu
                      else qs.filter(trading_account_id=aid).first())
            except (ValueError, DjangoValidationError):
                return _NOT_FOUND          # a malformed uuid/id matches nothing → 404 (never an unhandled 500)
            if ws is None:
                return _NOT_FOUND
            return Response({"diagnosis": diagnose_workspace(ws)})
        rows = list(qs.order_by("id")[:_FLEET_ROW_LIMIT])
        return Response({
            "rows": [diagnose_workspace(ws) for ws in rows],
            "limit": _FLEET_ROW_LIMIT,
            "metrics": observation_health_metrics(),
        })
