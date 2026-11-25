"""trade_copier.owner_demo_api — owner-facing HTTP surface for the governed #46→#48 demonstration control
(GFX-170, ADR-0127). Four stages: readiness (GET, read-only), prepare (POST), confirm (POST, fresh re-auth),
stop (POST). Mounted under ``/api/copy-trading/owner-demo/``.

Authorization is OWNERSHIP, not an email string: every stage resolves the governed route fail-closed and permits
ONLY the verified owner of BOTH governed accounts (``owner_demo_control.resolve_governed_demo_route``). The feature
flag ``copier_owner_demo_control_enabled`` gates availability (503 when DARK). State-changing stages are POST-only
(405 otherwise) and carry CSRF via ``CookieJWTAuthentication`` (the cookie auth enforces CSRF on unsafe methods).

Deliberate confirmation requires a FRESH RE-AUTHENTICATION PROOF: the owner re-submits their current password,
verified server-side with ``request.user.check_password`` (the same primitive as the change-password flow). The
password is never stored, logged, echoed, or placed in the audit — only the boolean verdict crosses into the
orchestrator. The browser never receives or submits a signing secret; production runs with NO host actuators, so
confirm fails closed (DARK) with ``EXECUTION_ACTUATORS_UNPROVISIONED`` until a separate Sponsor real-money GO.
"""
from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from trade_copier import owner_demo_control as odc


def _http_status(res: dict) -> int:
    """Map a stage result to an HTTP status. Fail-closed and unambiguous."""
    if res.get("enabled") is False or res.get("reason") == odc.R_FEATURE_DARK:
        return 503
    if res.get("authorized") is False:
        return 403
    reason = res.get("reason")
    if reason == odc.R_RECENT_AUTH_REQUIRED:
        return 403
    if reason in (odc.R_LEASE_CONFLICT, odc.R_AUTHZ_EXISTS, odc.R_ARM_EXISTS, odc.R_ALREADY_PREPARED,
                  odc.R_NO_PREPARED_DEMO, odc.R_CONFIRM_TOKEN_INVALID, odc.R_CONFIRM_TOKEN_EXPIRED,
                  odc.R_READINESS_NOT_READY):
        return 409
    return 200


class _OwnerDemoBase(APIView):
    permission_classes = [IsAuthenticated]


class OwnerDemoReadinessView(_OwnerDemoBase):
    """GET — read-only readiness + DARK gates + concurrency. Never mutates."""

    def get(self, request):
        res = odc.readiness(request.user)
        return Response(res, status=_http_status(res))


class OwnerDemoStatusView(_OwnerDemoBase):
    """GET — compact value-blind state read for the owner UI."""

    def get(self, request):
        res = odc.status(request.user)
        return Response(res, status=_http_status(res))


class OwnerDemoPrepareView(_OwnerDemoBase):
    """POST — create the bounded owner-self-approved authorization + hold the acceptance lease. No arm, no host."""

    def post(self, request):
        res = odc.prepare(request.user)
        return Response(res, status=_http_status(res))


class OwnerDemoConfirmView(_OwnerDemoBase):
    """POST — deliberate arm. Requires a fresh re-auth proof (re-submitted password) + the prepare confirm token."""

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        confirm_token = body.get("confirm_token") or ""
        password = body.get("password") or ""
        # Fresh re-authentication proof: verify the re-submitted password server-side. Never stored/logged/echoed.
        recent_auth_ok = False
        try:
            recent_auth_ok = bool(password) and bool(request.user.check_password(password))
        except Exception:  # noqa: BLE001 — any auth error is fail-closed (no step-up proof)
            recent_auth_ok = False
        password = None  # scrub the reference immediately
        res = odc.confirm(request.user, confirm_token=confirm_token, recent_auth_ok=recent_auth_ok,
                          actuators=None)   # production: NO host actuators ⇒ fails closed (DARK)
        return Response(res, status=_http_status(res))


class OwnerDemoStopView(_OwnerDemoBase):
    """POST — return the route to DARK (disarm + revoke + host OFF via unprovisioned actuators + lease release)."""

    def post(self, request):
        res = odc.stop(request.user, actuators=None)
        return Response(res, status=_http_status(res))
