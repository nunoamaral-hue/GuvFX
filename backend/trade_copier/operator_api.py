"""trade_copier.operator_api — the READ-ONLY operator lifecycle/failure API (ADR-0083 Phase-3).

ONE GET endpoint, STAFF-ONLY, read-only, redacted, paginated, DARK-gated. It exposes NO mutation surface
(only ``get`` is defined → POST/PUT/PATCH/DELETE return 405), takes NO retry/recover/modify/close/arm/
authorization action, and leaks NO secret/credential/raw-login. Non-staff requests are denied (403) — the
strongest tenant isolation: a customer can never reach the operator view. When the gate is OFF the endpoint
returns ``enabled:false`` with no rows.
"""
from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

_TRUTHY = ("1", "true", "yes", "on")
_MAX_LIMIT = 200


class OperatorLifecycleView(APIView):
    """GET /api/copy-trading/operator/lifecycles/ — staff-only, read-only operator lifecycle/failure view."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Staff/operator only. A non-staff (customer) user is denied outright — no cross-tenant access path.
        if not getattr(request.user, "is_staff", False):
            return Response({"detail": "operator access required"}, status=403)

        p = request.query_params
        account_id = p.get("account_id")
        state = p.get("state")
        attention_only = str(p.get("attention", "")).strip().lower() in _TRUTHY
        try:
            limit = int(p.get("limit", 50))
            offset = int(p.get("offset", 0))
            account_id = int(account_id) if account_id not in (None, "") else None
        except (TypeError, ValueError):
            return Response({"detail": "invalid query parameter"}, status=400)
        if limit < 1 or limit > _MAX_LIMIT or offset < 0:
            return Response({"detail": "limit must be 1..200 and offset >= 0"}, status=400)

        from trade_copier.operator_view import list_operator_lifecycles
        data = list_operator_lifecycles(
            account_id=account_id, state=state, attention_only=attention_only, limit=limit, offset=offset)
        return Response(data)
