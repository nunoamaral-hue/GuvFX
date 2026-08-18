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


def _source_cap_per_leg(asn):
    """The operator per-leg lot ceiling for this assignment's signal source (a HARD limit a customer
    can only reduce below). Fail-closed to the conservative global default for an unknown source."""
    from execution.models import SignalSourceConfig
    cap, _total = SignalSourceConfig.sizing_caps(getattr(asn, "signal_source", "") or "")
    return cap


try:
    from execution.models import MAX_PLAN_LEGS
except Exception:  # pragma: no cover - defensive import
    MAX_PLAN_LEGS = 3


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
        # The effective per-leg ceiling is the SMALLER of the model max and the operator/source cap —
        # a customer setting may only ever REDUCE risk within the operator ceiling (P0-A Phase 6).
        source_cap = _source_cap_per_leg(asn)
        effective_max = min(AssignmentLegSizing.LOT_MAX, source_cap)
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
            "max": str(effective_max),
            "source_cap": str(source_cap),
            "max_legs": MAX_PLAN_LEGS,   # beta Wayond opens up to this many TP positions per signal
            # P0-A: this per-leg lot NOW drives live planning (a customer with a row sizes at their lot;
            # no row keeps the source-global sizing). Future signals only — never open positions.
            "applies_to_live_execution": True,
            "note": "Sets the lot size for EACH position Wayond opens. A signal can open up to "
                    f"{MAX_PLAN_LEGS} positions, so the maximum total per signal is {MAX_PLAN_LEGS}× this "
                    "value. Applies to future signals only; never changes an open position.",
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
        # Enforce the operator/source per-leg cap on write: a customer may only REDUCE risk within the
        # operator ceiling, never raise it (broker min/step/max are validated inside validate_lot).
        try:
            requested = AssignmentLegSizing.validate_lot(request.data.get("lot_per_leg"))
        except DjangoValidationError as e:
            return Response({"ok": False, "errors": e.message_dict}, status=status.HTTP_400_BAD_REQUEST)
        cap = _source_cap_per_leg(asn)
        if requested > cap:
            return Response(
                {"ok": False, "errors": {"lot_per_leg": [f"must be at most {cap} for this strategy"]}},
                status=status.HTTP_400_BAD_REQUEST)
        try:
            set_assignment_lot_per_leg(asn, requested, user=request.user)
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
