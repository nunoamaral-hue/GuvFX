"""broker_intelligence read-only member API.

WP6: the member-facing Withdrawals metrics endpoint. Owner-scoped (the authenticated member's OWN, non-disconnected
accounts only — never staff cross-user aggregation, never another member's data) and DARK-gated
(``broker_withdrawal_ux_enabled`` — 404 while OFF so the member feature stays dark until the Sponsor arms it).
Observation-only: it reads the withdrawal projection; no mutation, no broker contact, no execution/credential/
financial authority.
"""
from __future__ import annotations

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .flags import broker_withdrawal_ux_enabled
from .metrics import withdrawal_metrics


class WithdrawalMetricsView(APIView):
    """GET /api/broker-intelligence/withdrawals/metrics/ — the member's own withdrawal statistics."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not broker_withdrawal_ux_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from trading.models import TradingAccount
        # ALL of the member's OWN accounts — INCLUDING disconnected/tombstoned ones. A withdrawal is a durable
        # historical fact; a withdrawal a member made on an account they later disconnected is still THEIR history
        # and must not silently vanish from their totals (truthfulness/completeness). Unlike the live-portfolio
        # dashboard (which excludes disconnected accounts because their LIVE balance is gone), this is a historical
        # record. Still strictly owner-scoped (user=request.user) with no staff bypass — never another member's data.
        accounts = TradingAccount.objects.filter(user=request.user)
        return Response(withdrawal_metrics(accounts))
