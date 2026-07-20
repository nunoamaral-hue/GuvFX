"""GFX-BETA-PHASE0 Increment 1 — per-(broker account + strategy assignment) lot-size override API.

Account-owner-scoped (NOT strategy-owner-scoped, unlike StrategyAssignmentViewSet) so a beta user who
owns the broker account can configure sizing for their assignment. READ + WRITE of the per-leg lot only;
it is NOT wired to live execution (inert until Phase-3 routing) and never touches the global operator
sizing or any open position.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (StrategyAssignment, AssignmentLegSizing, AssignmentLegSizingHistory,
                     effective_lot_per_leg, set_assignment_lot_per_leg)


class AssignmentLegSizingView(APIView):
    """GET/PUT /api/assignments/<pk>/leg-sizing/ — per-leg lot override for one strategy assignment.

    Ownership: the assignment's broker account must belong to request.user (staff bypass). This is the
    User → Broker Account → Strategy Assignment → per-leg lot chain.
    """
    permission_classes = [IsAuthenticated]

    def _get_owned_assignment(self, request, pk):
        # Not scoped by strategy owner — scoped by ACCOUNT owner (the beta-user model).
        asn = (StrategyAssignment.objects.select_related("account", "leg_sizing")
               .filter(pk=pk).first())
        if asn is None:
            return None, Response({"detail": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if not request.user.is_staff and asn.account.user_id != request.user.id:
            # Do not leak existence of another tenant's assignment.
            return None, Response({"detail": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return asn, None

    def _payload(self, asn):
        sizing = getattr(asn, "leg_sizing", None)
        return {
            "assignment_id": asn.id,
            "account_id": asn.account_id,
            "signal_source": getattr(asn, "signal_source", "") or "",
            "lot_per_leg": str(effective_lot_per_leg(asn)),
            "is_override": sizing is not None,
            "version": sizing.version if sizing is not None else None,
            "default_lot_per_leg": str(AssignmentLegSizing.DEFAULT_LOT),
            "min": str(AssignmentLegSizing.LOT_MIN),
            "step": str(AssignmentLegSizing.LOT_STEP),
            "max": str(AssignmentLegSizing.LOT_MAX),
            # Phase-0 truthfulness: this config does NOT affect live trading yet.
            "applies_to_live_execution": False,
            "note": "Applies to FUTURE signals only once multi-tenant routing is enabled (Phase 3); "
                    "never modifies open positions or the global operator sizing.",
        }

    def get(self, request, pk):
        asn, err = self._get_owned_assignment(request, pk)
        if err:
            return err
        return Response({"ok": True, **self._payload(asn)})

    def put(self, request, pk):
        asn, err = self._get_owned_assignment(request, pk)
        if err:
            return err
        try:
            set_assignment_lot_per_leg(asn, request.data.get("lot_per_leg"), user=request.user)
        except DjangoValidationError as e:
            return Response({"ok": False, "errors": e.message_dict}, status=status.HTTP_400_BAD_REQUEST)
        asn.refresh_from_db()
        asn = (StrategyAssignment.objects.select_related("account", "leg_sizing").get(pk=asn.pk))
        return Response({"ok": True, **self._payload(asn)})


class AssignmentLegSizingHistoryView(APIView):
    """GET /api/assignments/<pk>/leg-sizing/history/ — immutable audit trail (newest first)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        asn = StrategyAssignment.objects.select_related("account").filter(pk=pk).first()
        if asn is None or (not request.user.is_staff and asn.account.user_id != request.user.id):
            return Response({"detail": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        rows = (AssignmentLegSizingHistory.objects.filter(assignment=asn)
                .select_related("changed_by").order_by("-version"))

        def _who(r):
            # Do not disclose a staff member's identity to a non-staff tenant; show the tenant their
            # own changes, mask operator/staff edits as "operator".
            if r.changed_by_id is None:
                return None
            if request.user.is_staff or r.changed_by_id == request.user.id:
                return r.changed_by.email
            return "operator"

        return Response({"ok": True, "history": [{
            "version": r.version, "lot_per_leg": str(r.lot_per_leg),
            "changed_by": _who(r),
            "changed_at": r.changed_at.isoformat(),
        } for r in rows]})
