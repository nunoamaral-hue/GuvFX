"""trade_copier.ingest_probe_api — the NON-TRADING one-shot ingest-signing probe endpoint (GFX-160, ADR-0119).

``POST /api/copy-trading/ingest-probe/``. Machine-to-machine (standing emitter host -> backend), ``AllowAny`` +
manual HMAC verification, DARK + fail-closed: when ``copier_ingest_probe_enabled`` is off it returns 503 and
verifies nothing. It proves host-key signing + transport + authentication + replay protection ONLY; it creates NO
lifecycle state, ``MasterEvent``, ``ExecutionJob``, authorization, arm or broker contact, and the probe operation
is structurally incapable of becoming an OPEN/MODIFY/CLOSE.

Response (sanitised reason codes only; never a secret/signature/envelope):
  200 {"status":"verified", ...}           — signature + account pin + nonce all pass
  400 {"status":"rejected","reason"}        — malformed request/envelope
  401 {"status":"rejected","reason"}        — signature / account / operation / timestamp failure
  409 {"status":"replayed"}                 — nonce already burned
  503 {"status":"disabled"|"unavailable"}   — gate off / no single OPEN INGEST window / internal fault
"""
from __future__ import annotations

import json
import logging

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from trade_copier.emitter import ingest_probe as ip

logger = logging.getLogger(__name__)

_AUTH_FAIL = frozenset({
    "unknown_key_id", "bad_signature", "probe_stale", "probe_expired", "timestamp_in_future", "expiry_too_far",
    "account_mismatch", "account_not_probe_pinned", "nonce_too_long",
})
_MALFORMED = frozenset({"malformed_envelope", "missing_signed_field", "bad_protocol_version", "bad_operation",
                        "bad_timestamp", "bad_account_id"})


class CopierIngestProbeThrottle(SimpleRateThrottle):
    scope = "copier_ingest_probe"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class IngestProbeVerifyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [CopierIngestProbeThrottle]

    def post(self, request):
        from trade_copier.flags import copier_ingest_probe_enabled
        if not copier_ingest_probe_enabled():
            return Response({"status": "disabled"}, status=503)

        raw = request.body or b""
        if len(raw) > 65536:
            return Response({"status": "rejected", "reason": "body_too_large"}, status=413)
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return Response({"status": "rejected", "reason": "malformed_json"}, status=400)

        from trade_copier.ingest_probe_verify import (IngestProbeVerifyError, verify_ingest_probe_request)
        from trade_copier.verifier_window import VerifierWindowError
        try:
            verdict = verify_ingest_probe_request(envelope)
        except IngestProbeVerifyError as e:
            # Only "disabled" is raised by this layer; treat anything else as fail-closed unavailable.
            return Response({"status": "disabled" if e.reason == "disabled" else "unavailable"}, status=503)
        except VerifierWindowError:
            # No single OPEN, unexpired INGEST window (0, >=2, expired, or key file absent) -> fail closed.
            return Response({"status": "unavailable", "reason": "verifier_window_unavailable"}, status=503)
        except ip.IngestProbeError as e:
            if e.reason == "nonce_replayed":
                return Response({"status": "replayed"}, status=409)
            status = 400 if e.reason in _MALFORMED else (401 if e.reason in _AUTH_FAIL else 400)
            return Response({"status": "rejected", "reason": e.reason}, status=status)
        except Exception:  # noqa: BLE001 — never leak internals; fail closed
            logger.exception("copier ingest probe verify unexpected fault")
            return Response({"status": "unavailable"}, status=503)

        return Response({"status": "verified", **verdict}, status=200)
