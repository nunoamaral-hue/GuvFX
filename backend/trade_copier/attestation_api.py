"""trade_copier.attestation_api — the NON-LIFECYCLE host-emitter attestation verification endpoint (ADR-0077).

``POST /api/copy-trading/attest-verify/``. Machine-to-machine (emitter → backend), ``AllowAny`` + manual HMAC
verification, DARK + fail-closed: when ``COPIER_ATTESTATION_VERIFY_ENABLED`` is off it returns 503 and verifies
nothing. It verifies an emitter identity attestation and cross-checks it against the DB; it creates NO lifecycle
state, MasterEvent, ExecutionJob, alert or broker contact.

Response (sanitised reason codes only; never a secret/signature):
  200 {"status":"verified", ...}       — signature + DB identity cross-check pass
  200 {"status":"rejected","reason"}   — unknown account (acknowledged, deterministic)
  400 {"status":"rejected","reason"}   — malformed request/envelope
  401 {"status":"rejected","reason"}   — signature / identity-mismatch / timestamp / build failure
  409 {"status":"replayed"}            — nonce already burned
  503 {"status":"disabled"|"unavailable"} — gate off / key unset
"""
from __future__ import annotations

import json
import logging

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from trade_copier.emitter import attestation as att

logger = logging.getLogger(__name__)

_AUTH_FAIL = frozenset({
    "unknown_key_id", "bad_signature", "attestation_stale", "attestation_expired", "timestamp_in_future",
    "expiry_too_far", "account_mismatch", "broker_login_mismatch", "broker_server_mismatch", "host_mismatch",
    "terminal_mismatch", "bridge_mismatch", "build_not_allowed", "observed_login_mismatch", "nonce_too_long",
})
_MALFORMED = frozenset({"malformed_envelope", "missing_signed_field", "bad_protocol_version", "bad_operation",
                        "bad_timestamp", "bad_account_id"})


class CopierAttestThrottle(SimpleRateThrottle):
    scope = "copier_attest_verify"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class AttestationVerifyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [CopierAttestThrottle]

    def post(self, request):
        from trade_copier.flags import copier_attestation_verify_enabled
        if not copier_attestation_verify_enabled():
            return Response({"status": "disabled"}, status=503)

        raw = request.body or b""
        if len(raw) > 65536:
            return Response({"status": "rejected", "reason": "body_too_large"}, status=413)
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return Response({"status": "rejected", "reason": "malformed_json"}, status=400)

        from trade_copier.attestation_verify import (AttestationVerifyError, verify_attestation_request)
        try:
            verdict = verify_attestation_request(envelope)
        except AttestationVerifyError as e:
            if e.reason == "disabled":
                return Response({"status": "disabled"}, status=503)
            if e.reason == "account_not_found":
                return Response({"status": "rejected", "reason": e.reason}, status=200)
            logger.error("copier attestation verify fault (fail closed)")
            return Response({"status": "unavailable"}, status=503)
        except att.AttestError as e:
            if e.reason == "nonce_replayed":
                return Response({"status": "replayed"}, status=409)
            status = 400 if e.reason in _MALFORMED else (401 if e.reason in _AUTH_FAIL else 400)
            return Response({"status": "rejected", "reason": e.reason}, status=status)
        except Exception:  # noqa: BLE001 — never leak internals; fail closed
            logger.exception("copier attestation verify unexpected fault")
            return Response({"status": "unavailable"}, status=503)

        return Response({"status": "verified", **verdict}, status=200)
