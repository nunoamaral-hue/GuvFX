from django.http import JsonResponse
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
