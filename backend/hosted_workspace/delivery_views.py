"""ADR-0034 Workspace Delivery — the owner-scoped delivery API (state read + connect mint, DARK).

Two owner-scoped endpoints, both 404-invisible unless BOTH ``hosted_persistent_mt5_enabled()`` (master) AND
``hosted_mt5_remoteapp_enabled()`` (delivery) are ON:

- ``HostedWorkspaceDeliveryStateView`` (GET) — the non-secret delivery-state projection. Non-staff read only
  their own account (404 otherwise, IDOR-safe); staff retain the read-only bypass (parity with the M3c state
  API). The payload carries no credential, Windows username, runtime path, or signed URL.

- ``HostedWorkspaceDeliveryConnectView`` (POST) — mints the owner's RemoteApp connection descriptor. The
  client supplies ONLY its own ``account_id`` (intent); the server derives the workspace, host, Windows
  username, RemoteApp program/args and credential from durable records. STRICTLY owner-scoped — there is NO
  staff bypass on minting (unlike the read view): minting opens a live credentialed session, so it requires
  actual ownership (least privilege, parity with ``authorize_workspace_delivery``). Delivery ONLY: it never
  arms or influences execution. Fail-closed: any not-owner/missing case is a 404 (IDOR-safe); an owned-but-
  not-deliverable workspace is a 409 with a stable, non-secret reason code; the returned descriptor is the
  4 safe fields only (the Windows password rides solely inside the AES token in ``embed_url``, never returned
  or logged).
"""
from __future__ import annotations

import uuid

from rest_framework import status as http
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from trading.models import TradingAccount

from hosted_workspace.delivery import (
    DeliveryReason,
    authorize_workspace_delivery,
)
from hosted_workspace.delivery_persistence import record_delivery_attempt
from hosted_workspace.delivery_read_model import delivery_state_projection
from hosted_workspace.flags import hosted_mt5_remoteapp_enabled, hosted_persistent_mt5_enabled
from hosted_workspace.models import HostedMt5Workspace


class HostedWorkspaceDeliveryStateView(APIView):
    """GET /api/hosted-workspace/delivery-state/?account_id=<id>"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # DARK gate FIRST — before any DB read. Requires BOTH master + delivery flags ON.
        if not (hosted_persistent_mt5_enabled() and hosted_mt5_remoteapp_enabled()):
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

        account_id = request.query_params.get("account_id")
        if not account_id:
            return Response({"detail": "account_id is required."}, status=http.HTTP_400_BAD_REQUEST)
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)
        # Out-of-range PK -> not found (fail closed): never let an oversized id reach the ORM lookup.
        if not (0 < account_id <= (1 << 63) - 1):
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

        is_staff = bool(getattr(request.user, "is_staff", False))
        # IDOR-safe owner scoping: a non-staff user may resolve only their OWN account.
        if is_staff:
            account = TradingAccount.objects.filter(id=account_id).first()
        else:
            account = TradingAccount.objects.filter(id=account_id, user=request.user).first()
        if account is None:
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

        workspace = (HostedMt5Workspace.objects
                     .select_related("workspace_node")
                     .filter(trading_account_id=account.id).first())
        if workspace is None:
            return Response({"detail": "No hosted workspace for this account."},
                            status=http.HTTP_404_NOT_FOUND)

        return Response(delivery_state_projection(workspace, staff=is_staff))


# Reasons that mean "you own this workspace but it is not deliverable yet" — safe to surface the stable code
# to the owner (non-secret), returned as 409. Everything else that denies is mapped to 404 (IDOR-safe) or a
# generic 503 (unexpected), so a caller can never distinguish "not yours" from "does not exist".
_NOT_READY_REASONS = frozenset({
    DeliveryReason.NODE_UNASSIGNED,
    DeliveryReason.NODE_TRANSPORT_UNCONFIGURED,
    DeliveryReason.IDENTITY_MISSING,
    DeliveryReason.IDENTITY_ADMIN,
    DeliveryReason.IDENTITY_NOT_PROVISIONED,
    DeliveryReason.IDENTITY_NO_CREDENTIAL,
    DeliveryReason.RUNTIME_MISSING,
    DeliveryReason.GUAC_UNCONFIGURED,
})


class HostedWorkspaceDeliveryConnectView(APIView):
    """POST /api/hosted-workspace/delivery-connect/  body: {"account_id": <int>}

    Mint the caller's OWN RemoteApp connection descriptor. Owner-scoped, DARK-gated, fail-closed, delivery-only
    (never touches execution). The client's only input is its own ``account_id``; host / Windows username /
    RemoteApp program / args / credential are ALL server-derived by ``authorize_workspace_delivery``. The
    Windows password is never returned or logged — it rides only inside the AES token in ``embed_url``."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # DARK gate FIRST — before any DB read. Requires BOTH master + delivery flags ON (404 while OFF).
        if not (hosted_persistent_mt5_enabled() and hosted_mt5_remoteapp_enabled()):
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

        account_id = request.data.get("account_id") if hasattr(request, "data") else None
        if account_id is None:
            return Response({"detail": "account_id is required."}, status=http.HTTP_400_BAD_REQUEST)
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)
        if not (0 < account_id <= (1 << 63) - 1):
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

        # STRICTLY owner-scoped — NO staff bypass on minting. A user may connect ONLY their own account; any
        # other account (incl. for staff) is indistinguishable from non-existent (404, IDOR-safe).
        account = TradingAccount.objects.filter(id=account_id, user=request.user).first()
        if account is None:
            return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

        workspace = (HostedMt5Workspace.objects
                     .select_related("trading_account", "workspace_node")
                     .filter(trading_account_id=account.id).first())
        if workspace is None:
            return Response({"detail": "No hosted workspace for this account."},
                            status=http.HTTP_404_NOT_FOUND)

        # The ONLY server-derived input to the delivery authority is the owner-resolved workspace uuid. The
        # authority re-checks ownership itself (defence-in-depth) and derives every connection value.
        correlation_id = uuid.uuid4().hex
        auth = authorize_workspace_delivery(request.user, workspace.workspace_uuid)
        # Single-writer bookkeeping: record the attempt (AUTHORIZED/FAILED) on the OWNER's workspace. No
        # credential is persisted; this emits no telemetry and never touches execution.
        record_delivery_attempt(workspace, auth, correlation_id=correlation_id)

        if auth.authorized and auth.descriptor is not None:
            # Return ONLY the 4 safe fields. The Windows password is NOT here (it is inside the AES token).
            return Response({
                "transport_type": auth.descriptor.get("transport_type"),
                "embed_url": auth.descriptor.get("embed_url"),
                "session_token": auth.descriptor.get("session_token", ""),
                "expiry": auth.descriptor.get("expiry"),
            })

        reason = str(auth.reason or "")
        if reason in _NOT_READY_REASONS:
            # Owned but not deliverable yet — safe to surface the stable, non-secret reason code.
            return Response({"detail": "Workspace is not ready for delivery.", "reason": reason},
                            status=http.HTTP_409_CONFLICT)
        # NOT_OWNER / WORKSPACE_MISSING / INVALID_REQUEST / SUBSYSTEM_DISABLED / ERROR — never distinguish the
        # cause to the caller; fail closed as 404 (nothing to connect to).
        return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)
