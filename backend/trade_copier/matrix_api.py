"""trade_copier.matrix_api — read-only multi-broker instrument-mapping matrix API (founder-only).

``GET /api/copy-trading/instrument-matrix/`` returns, for every ORDERED pair of the caller's OWN current
live accounts, the catalogue-coverage counts (eligible source instruments, verified, unverified,
ambiguous, stale, ineligible, absent) + a coverage percentage, and a drill-down candidate list per pair
(native master→follower symbols, classification, verification time/source, material differences).

It is:
  * cohort-gated (``copier_ui_enabled_for``, default-DENY) AND ``IsAuthenticated``;
  * owner-scoped — only the caller's own accounts are considered (``current_live_accounts``), so it can
    never read another tenant's catalogue;
  * strictly READ-ONLY — it computes from the stored catalogue + maps; it writes nothing, enables no flag,
    and places no order. Catalogue coverage is explicitly NOT execution readiness.
"""
from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from trade_copier.config_api import current_live_accounts
from trade_copier.flags import copier_ui_enabled_for
from trade_copier.matrix import ABSENT, compute_matrix

# The quiet observation-mode status reused across the Copy Trading surfaces (no red banner). Catalogue
# coverage is NOT execution readiness — stated explicitly so a full matrix is never mistaken for "ready".
_EXECUTION_STATUS = {
    "mode": "observation",
    "execution": "off",
    "label": "Observation mode",
    "detail": "Copy execution off",
    "note": "Catalogue coverage is not execution readiness. Mapping or saving configuration does not activate trading.",
}


def _serialize_candidate(c) -> dict:
    return {
        "master_symbol": c.master_symbol,
        "follower_symbol": c.follower_symbol,
        "canonical": {"slug": c.canonical_slug, "display": c.canonical_display},
        "classification": c.classification,
        "reason": c.reason,
        "confidence": c.confidence,
        "material_differences": c.material_differences,
        "verified_at": c.verified_at,
        "verified_source": c.verified_source,
    }


def _serialize_direction(d, *, include_absent: bool) -> dict:
    cands = d.candidates if include_absent else [c for c in d.candidates if c.classification != ABSENT]
    return {
        "master_account_id": d.master_account_id,
        "master_broker": d.master_broker,
        "follower_account_id": d.follower_account_id,
        "follower_broker": d.follower_broker,
        "eligible_source": d.eligible_source,
        "counts": d.counts,
        "coverage_pct": d.coverage_pct,
        "candidates": [_serialize_candidate(c) for c in cands],
    }


class InstrumentMatrixView(APIView):
    """GET the founder-only, owner-scoped, read-only instrument-mapping matrix."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not copier_ui_enabled_for(request.user):
            return Response({
                "enabled": False, "execution_status": _EXECUTION_STATUS,
                "accounts": [], "directions": [],
                "note": "Catalogue coverage is not execution readiness.",
            })
        accounts = list(current_live_accounts(request.user))
        view = compute_matrix(accounts)

        # Optional single-pair drill-down (inspection by pair); otherwise all ordered pairs.
        want_master = request.query_params.get("master")
        want_follower = request.query_params.get("follower")
        include_absent = str(request.query_params.get("include_absent", "")).lower() in ("1", "true", "yes")
        directions = view.directions
        if want_master and want_follower:
            try:
                wm, wf = int(want_master), int(want_follower)
                directions = [d for d in directions
                              if d.master_account_id == wm and d.follower_account_id == wf]
                include_absent = True  # a focused single-pair view shows everything, absent included
            except (TypeError, ValueError):
                directions = []

        return Response({
            "enabled": True,
            "execution_status": _EXECUTION_STATUS,
            "accounts": view.accounts,
            "directions": [_serialize_direction(d, include_absent=include_absent) for d in directions],
            "note": "Catalogue coverage is not execution readiness.",
        })
