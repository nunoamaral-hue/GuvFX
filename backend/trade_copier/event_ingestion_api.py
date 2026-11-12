"""trade_copier.event_ingestion_api — the HTTP surface for the signed fast-path event-ingestion endpoint.

Machine-to-machine (bridge → backend). Follows the app's dedicated-worker idiom: ``authentication_classes=[]``
+ ``AllowAny`` + manual HMAC verification in the body (the cookie/JWT default would reject the bridge). It is
DARK and fail-closed: when ``COPIER_EVENT_INGESTION_ENABLED`` is off it returns 503 and persists nothing.

Response contract (sanitised reason codes only; never a secret/signature/raw payload):
  200 {"status":"accepted", ...}       — batch verified & persisted (idempotent; duplicates counted)
  200 {"status":"rejected", "reason"}  — PERMANENT, deterministic rejection (unknown account / identity) —
                                         acknowledged so an ordered redelivery queue cannot deadlock
  400 {"status":"rejected", "reason"}  — malformed request/payload
  401 {"status":"rejected", "reason"}  — signature / key / timestamp / expiry / digest failure
  409 {"status":"replayed"}            — nonce already burned (idempotent duplicate request)
  413 {"status":"rejected", "reason"}  — body too large
  503 {"status":"disabled"|"unavailable"|"error"} — gate off / key unset / transient fault (safe to retry)
"""
from __future__ import annotations

import json
import logging
import time

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from trade_copier import event_ingestion as ei

logger = logging.getLogger(__name__)

# Verifier reason → HTTP status for the pre-persistence (signature/envelope) stage.
_AUTH_FAIL = frozenset({"unknown_key_id", "bad_signature", "timestamp_skew", "request_expired",
                        "expiry_too_far", "payload_digest_mismatch"})
_MALFORMED = frozenset({"malformed_envelope", "missing_signed_field", "bad_protocol_version",
                        "bad_operation", "bad_timestamp"})
# Post-verification service rejections that are PERMANENT (acknowledge with 200 so redelivery can't deadlock).
_PERMANENT_REJECT = frozenset({"account_not_master"})
_BAD_PAYLOAD = frozenset({"malformed_payload", "empty_batch", "too_many_revisions"})


class CopierIngestThrottle(SimpleRateThrottle):
    """Per-identity rate limit for the AllowAny machine endpoint (the user-scoped default can't bind here)."""
    scope = "copier_event_ingest"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class SignedEventIngestView(APIView):
    authentication_classes = []          # HMAC envelope verification is handled explicitly below
    permission_classes = [AllowAny]
    throttle_classes = [CopierIngestThrottle]

    def post(self, request):
        from trade_copier.flags import copier_event_ingestion_enabled
        if not copier_event_ingestion_enabled():
            return Response({"status": "disabled"}, status=503)

        raw = request.body or b""
        if len(raw) > ei.MAX_BODY_BYTES:
            return Response({"status": "rejected", "reason": "body_too_large"}, status=413)
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return Response({"status": "rejected", "reason": "malformed_json"}, status=400)

        try:
            keyring = ei.build_keyring()
        except Exception:  # noqa: BLE001 — CredentialError or any resolution fault → fail closed, no detail leak
            logger.error("copier ingest key unavailable (fail closed)")
            return Response({"status": "unavailable", "reason": "ingest_key_unset"}, status=503)

        try:
            verified = ei.verify_ingest_request(
                envelope, keyring=keyring, now=time.time(), nonce_burn=ei.make_nonce_burn())
        except ei.IngestError as e:
            if e.reason == "nonce_replayed":
                return Response({"status": "replayed"}, status=409)
            status = 400 if e.reason in _MALFORMED else (401 if e.reason in _AUTH_FAIL else 400)
            return Response({"status": "rejected", "reason": e.reason}, status=status)

        signed = verified["signed"]
        try:
            account_id = int(signed.get("account_id"))
        except (TypeError, ValueError):
            return Response({"status": "rejected", "reason": "bad_account_id"}, status=400)
        correlation_id = str(signed.get("correlation_id") or "") or _new_cid()
        body_digest = str(signed.get("payload_digest") or "")

        try:
            outcome = ei.ingest_revisions(
                account_id=account_id, payload=verified["payload"],
                body_digest=body_digest, correlation_id=correlation_id)
        except ei.IngestError as e:
            if e.reason in _PERMANENT_REJECT:
                _raise_ingest_alert(e.reason, account_id=account_id, correlation_id=correlation_id)
                return Response({"status": "rejected", "reason": e.reason}, status=200)
            if e.reason in _BAD_PAYLOAD:
                return Response({"status": "rejected", "reason": e.reason}, status=400)
            logger.error("copier ingest service fault (fail closed)")
            return Response({"status": "error"}, status=503)
        except Exception:  # noqa: BLE001 — never leak internals; fail closed and allow a safe retry
            logger.exception("copier ingest unexpected fault")
            return Response({"status": "error"}, status=503)

        if not outcome.identity_ok:
            _raise_ingest_alert("identity_mismatch", account_id=account_id, correlation_id=correlation_id)

        # ADR-0094 — REAL-TIME: when the immediate-dispatch gate is ARMED, drive the governed pipeline for this
        # master in-line so the follower order is submitted now (target ≤1s) instead of waiting for the ~2-min
        # scheduled worker. DARK/default-OFF: when the gate is off we add zero work and this response is
        # byte-identical to ADR-0074. Fail-safe: a dispatch fault never breaks this response or the immutable
        # evidence already committed (the slow reconciler stays the safety net); every downstream gate + the
        # full per-pair live admission are re-read inside the pass.
        realtime = None
        if outcome.identity_ok and outcome.accepted and _immediate_dispatch_armed():
            realtime = _trigger_immediate_dispatch(account_id=account_id, correlation_id=correlation_id)

        return Response({
            "status": "accepted",
            "accepted": outcome.accepted,
            "duplicates": outcome.duplicates,
            "rejected": [{"ticket": t, "revision": r, "reason": why} for (t, r, why) in outcome.rejected],
            "changes": list(outcome.changes),
            "realtime": realtime,
        }, status=200)


def _new_cid() -> str:
    try:
        from core.observability import new_correlation_id
        return new_correlation_id()
    except Exception:  # noqa: BLE001
        return ""


def _raise_ingest_alert(reason: str, *, account_id: int, correlation_id: str) -> None:
    """Route a permanent ingestion rejection to the governed operator alert channel (gated, dedup, fail-open)."""
    try:
        from trade_copier import failure_alerts
        failure_alerts.raise_ingest_rejection(reason, account_id=account_id, correlation_id=correlation_id)
    except Exception:  # noqa: BLE001 — alerting must never break ingestion
        logger.exception("copier ingest alert routing failed")


def _immediate_dispatch_armed() -> bool:
    """Cheap pre-check so the ADR-0074 OFF path stays byte-identical (we only reach the orchestrator when armed)."""
    try:
        from trade_copier.flags import copier_immediate_dispatch_enabled
        return bool(copier_immediate_dispatch_enabled())
    except Exception:  # noqa: BLE001 — fail closed to the inert persist-only path
        return False


def _trigger_immediate_dispatch(*, account_id: int, correlation_id: str):
    """Resolve the master for this signed account scope and run the governed real-time pipeline. Fully fail-safe:
    the orchestrator is itself contained, and any resolution error here is swallowed so ingestion never breaks."""
    try:
        from trade_copier.immediate_dispatch import dispatch_master_realtime
        from trade_copier.models import CopierMaster
        master = CopierMaster.objects.filter(trading_account_id=account_id).only("id").first()
        if master is None:
            return {"enabled": True, "reason": "no_master_for_account", "submitted": 0}
        return dispatch_master_realtime(master.pk, source="EVENT_PUSH", correlation_id=correlation_id)
    except Exception:  # noqa: BLE001 — a real-time trigger fault must never break ingestion
        logger.exception("copier immediate-dispatch trigger contained")
        return {"enabled": True, "reason": "trigger_contained", "submitted": 0}
