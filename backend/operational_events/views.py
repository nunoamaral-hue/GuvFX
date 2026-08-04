"""WP5.1 — the owner-scoped operational-events API (ADR-0032).

Read-only. DARK (404) while ``OPERATIONS_EVENTS_ENABLED`` is off — the endpoint does not exist yet.
Returns ``{summary, timeline}`` for the caller's OWN account. Non-staff users may read only their own
account (404 otherwise, IDOR-safe) and receive customer-visible events; staff may read any account and
receive operator-visible (all) events. No secrets, no diagnostics — the payload is entirely non-secret
projections. No privilege expansion beyond the existing staff bypass convention.
"""
from __future__ import annotations

from rest_framework import status as http
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from trading.models import TradingAccount

from .constants import DEFAULT_TIMELINE_LIMIT, operations_events_enabled
from .query import OperationalQueryService


class OperationalAccountEventsView(APIView):
    """GET /api/operations/account-events/?account_id=<id>[&limit=&offset=&category=]"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # DARK gate FIRST — before any DB read — so the endpoint is invisible while the flag is OFF.
        if not operations_events_enabled():
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

        account_id = request.query_params.get("account_id")
        if not account_id:
            return Response({"detail": "account_id is required."}, status=http.HTTP_400_BAD_REQUEST)
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

        is_staff = bool(getattr(request.user, "is_staff", False))
        # IDOR-safe owner scoping: a non-staff user may resolve only their OWN account.
        if is_staff:
            account = TradingAccount.objects.filter(id=account_id).first()
        else:
            account = TradingAccount.objects.filter(id=account_id, user=request.user).first()
        if account is None:
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

        category = request.query_params.get("category") or None
        limit = request.query_params.get("limit", DEFAULT_TIMELINE_LIMIT)
        offset = request.query_params.get("offset", 0)
        # Non-staff see only customer-visible events; staff (operators) see everything for the account.
        customer_only = not is_staff

        timeline = OperationalQueryService.timeline(
            account, limit=limit, offset=offset, category=category, customer_only=customer_only)
        # Same visibility boundary as the timeline: a non-staff owner's summary aggregates must not
        # disclose operator-only (customer_visible=False) event content.
        summary = OperationalQueryService.summary(account, customer_only=customer_only)
        return Response({
            "summary": summary.as_dict(),
            "timeline": [e.as_dict() for e in timeline],
        })
