import os

from django.conf import settings
from django.http import Http404, JsonResponse
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .version import provenance


def health_check(request):
    return JsonResponse(
        {
            "status": "ok",
            "service": "guvfx-backend",
        }
    )


def operational_readiness_api_enabled() -> bool:
    """DARK-by-default gate for the Operational Readiness read API (settings-first-then-env, tolerant).
    OFF => the endpoint 404s (invisible). The CLI commands remain available regardless."""
    val = getattr(settings, "OPERATIONAL_READINESS_API_ENABLED", None)
    if val is None:
        val = os.getenv("OPERATIONAL_READINESS_API_ENABLED", "")
    return str(val).strip().lower() in ("1", "true", "yes", "on")


class VersionView(APIView):
    """IPR Area G — staff-only build provenance + live arming-flag snapshot.

    Least-privilege (IsAdminUser, mirroring reliability/views.py): a build fingerprint is a mild recon
    aid, so it is NOT put on the public health endpoint. Everything returned is non-secret (commit /
    build timestamp / release id + resolved flag booleans — names + booleans only). This is the
    host-verified deploy-parity oracle: compare git_commit against the intended release SHA across the
    shared backend image (guvfx-backend / trade-ingest / shadow)."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        return Response(provenance())


class OperationalReadinessView(APIView):
    """ADR-0035 — the staff-only, DARK-by-default Operational Readiness console data source.

    Read-only aggregation over the existing per-subsystem health sources: the 7-state operational health
    rollup, the Hosted Workspace pre-flight, and the flag-disable rollback plan. Least-privilege
    (IsAdminUser); 404-invisible unless ``OPERATIONAL_READINESS_API_ENABLED`` is on (the CLI commands do
    not need the flag). Everything returned is non-secret (subsystem states, flag booleans, node/counts).
    It MUTATES NOTHING and never authorises anything. ``?section=health|preflight|rollback`` narrows it."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        if not operational_readiness_api_enabled():
            raise Http404()
        from core.operational_health import build_operational_health
        from core.preflight import run_preflight
        from core.rollback_planner import plan_rollback

        section = str(request.query_params.get("section", "") or "").strip().lower()
        builders = {
            "health": lambda: {"operational_health": build_operational_health()},
            "preflight": lambda: {"preflight": run_preflight()},
            "rollback": lambda: {"rollback_plan": plan_rollback()},
        }
        if section in builders:
            return Response(builders[section]())
        return Response({
            "operational_health": build_operational_health(),
            "preflight": run_preflight(),
            "rollback_plan": plan_rollback(),
        })
