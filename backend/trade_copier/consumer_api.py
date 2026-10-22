"""trade_copier.consumer_api — dedicated DEMO partial-close claim/complete HTTP endpoints (ADR-0056).

A SEPARATE surface from the generic ``next_job``. MODERN worker auth ONLY (X-Worker-Id / X-Worker-Secret);
a legacy shared X-Worker-Token is categorically refused. Only a pure ``partial_close_worker`` identity is
accepted (a co-granted generic-mutation permission is refused — mutually exclusive classes). The worker
supplies NO routing/identity authority: its authorized account + node(s) come from its own
``worker_permissions`` and node authorization, and every job fact is re-derived server-side.
"""
from __future__ import annotations

from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from execution.auth import authenticate_worker
from execution.node_execution import worker_authorized_nodes
from trade_copier import consumer
from trade_copier.consumer import ClaimRefused


def _modern_worker(request):
    """Resolve a MODERN worker identity. A legacy X-Worker-Token is refused (no shared-token access); absent
    modern credentials are refused; bad credentials raise PermissionDenied (→ 403)."""
    if request.META.get("HTTP_X_WORKER_TOKEN"):
        raise ClaimRefused("legacy_worker_token_refused")
    wid = request.META.get("HTTP_X_WORKER_ID")
    secret = request.META.get("HTTP_X_WORKER_SECRET")
    if not wid or not secret:
        raise ClaimRefused("modern_worker_auth_required")
    return authenticate_worker(wid, secret)


class _DedicatedWorkerView(APIView):
    authentication_classes = []            # worker header auth is handled explicitly below
    permission_classes = [AllowAny]

    def _auth(self, request):
        worker = _modern_worker(request)
        consumer.assert_dedicated_worker(worker)   # pure partial_close_worker only (no generic co-grant)
        return worker


class PartialCloseClaimView(_DedicatedWorkerView):
    def post(self, request):
        try:
            worker = self._auth(request)
        except ClaimRefused as e:
            return Response({"detail": e.reason}, status=403)
        except PermissionDenied:
            return Response({"detail": "invalid_worker_credentials"}, status=403)
        perms = worker.worker_permissions or {}
        account_id = perms.get("partial_close_account_id")   # the worker's ONE authorized account route
        if account_id is None:
            return Response({"detail": "no_account_route"}, status=403)
        out = consumer.claim_partial_close(
            worker, account_id=int(account_id), authorized_node_hostnames=worker_authorized_nodes(worker))
        if out.get("ok"):
            return Response(out, status=200)
        return Response(out, status=(204 if out.get("reason") == "no_jobs" else 409))


class PartialCloseAuthorizeSendView(_DedicatedWorkerView):
    """Single-use PRE-SEND authorization (ADR-0056 F1). The host worker calls this immediately before the
    bridge mutation; the mutation instruction (ticket/volume/symbol/identity) is released ONLY on success,
    after a fresh in-lock re-read of the three feature flags + full route/identity revalidation."""
    def post(self, request):
        try:
            worker = self._auth(request)
        except ClaimRefused as e:
            return Response({"detail": e.reason}, status=403)
        except PermissionDenied:
            return Response({"detail": "invalid_worker_credentials"}, status=403)
        perms = worker.worker_permissions or {}
        account_id = perms.get("partial_close_account_id")
        if account_id is None:
            return Response({"detail": "no_account_route"}, status=403)
        data = request.data or {}
        try:
            out = consumer.authorize_send(
                worker, job_id=int(data.get("job_id")), dispatch_token=str(data.get("dispatch_token") or ""),
                account_id=int(account_id), authorized_node_hostnames=worker_authorized_nodes(worker))
        except ClaimRefused as e:
            return Response({"detail": e.reason}, status=403)
        except (TypeError, ValueError):
            return Response({"detail": "bad_request"}, status=400)
        return Response(out, status=200 if out.get("ok") else 409)


class PartialCloseCompleteView(_DedicatedWorkerView):
    def post(self, request):
        try:
            worker = self._auth(request)
        except ClaimRefused as e:
            return Response({"detail": e.reason}, status=403)
        except PermissionDenied:
            return Response({"detail": "invalid_worker_credentials"}, status=403)
        data = request.data or {}
        try:
            out = consumer.complete_partial_close(
                worker, job_id=int(data.get("job_id")), dispatch_token=str(data.get("dispatch_token") or ""),
                bridge_result=data.get("bridge_result") or {},
                send_nonce=(str(data.get("send_nonce")) if data.get("send_nonce") is not None else None))
        except ClaimRefused as e:
            return Response({"detail": e.reason}, status=403)
        except (TypeError, ValueError):
            return Response({"detail": "bad_request"}, status=400)
        return Response(out, status=200 if out.get("ok") else 409)
