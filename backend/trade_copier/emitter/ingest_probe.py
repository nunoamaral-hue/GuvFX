"""trade_copier.emitter.ingest_probe — one-shot NON-TRADING ingest-signing probe envelope (GFX-160, ADR-0119).
Pure, Django-free. ASCII-only.

Purpose: prove, end to end, that the standing account-46 ingest key can sign a request the backend accepts via the
bounded INGEST verifier window — i.e. host-key signing + transport + backend HMAC authentication + timestamp/skew +
single-use nonce/replay protection — WITHOUT any trading semantics. It mirrors the audited ``event_ingestion`` /
``attestation`` HMAC idiom (canonical-JSON signed field set, HMAC-SHA256, ``hmac.compare_digest``, bounded skew +
expiry, single-use nonce) and signs with the emitter's OWN dedicated key (``COPIER_INGEST_HMAC_KEY``) — never a
broker/agent/DB/Tailscale secret.

STRUCTURALLY NON-TRADING (the core safety property): the signed envelope carries ONLY a fixed protocol/operation,
the key id, the fixed account id, a nonce and timestamps. It has NO symbol, side, volume, price, ticket, order,
lifecycle or position field, so it is structurally INCAPABLE of being interpreted as (or mutated into) an
OPEN/MODIFY/CLOSE execution event. Verifying a probe creates NO ``MasterEvent``, ``CopierPositionLifecycle``,
``CopierLifecycleDispatch``, ``ExecutionJob``, follower ticket, authorization, arm, ``LiveExecutionAuthorization``
or alert, and contacts no broker. It is a pure crypto/transport/replay proof and NOTHING else.

Negative controls (wrong key, stale/future timestamp, expired, replay, tamper, bad signature, wrong account,
wrong/unsupported operation or protocol, oversize nonce) all fail closed with a stable sanitised ``reason``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Callable, Optional

INGEST_PROBE_PROTOCOL_VERSION = "copier-ingest-probe-1"
INGEST_PROBE_OPERATION = "copier_ingest_probe"
DEFAULT_MAX_SKEW_SECONDS = 30
MAX_EXPIRY_WINDOW_SECONDS = 600
MAX_NONCE_LEN = 80

# The COMPLETE signed field set. Deliberately minimal and trade-free: no symbol/side/volume/price/ticket/order.
_SIGNED_FIELDS = (
    "protocol_version", "operation", "key_id", "account_id", "nonce", "timestamp", "expiry",
)


class IngestProbeError(Exception):
    """A sanitised probe rejection. ``reason`` is a stable code (never a secret)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def canonical_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _signature(secret: str, signed: dict) -> str:
    body = canonical_bytes({k: signed[k] for k in _SIGNED_FIELDS})
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def build_ingest_probe(*, secret: str, account_id: int, nonce: str, now: float,
                       key_id: str = "copier-ingest",
                       ttl_seconds: int = DEFAULT_MAX_SKEW_SECONDS) -> dict:
    """Build ONE signed, non-trading probe envelope. No identity fingerprints, no trade fields: only the fixed
    protocol/operation, the key id, the account id, the nonce and the timestamps are signed."""
    signed = {
        "protocol_version": INGEST_PROBE_PROTOCOL_VERSION,
        "operation": INGEST_PROBE_OPERATION,
        "key_id": str(key_id),
        "account_id": int(account_id),
        "nonce": str(nonce),
        "timestamp": float(now),
        "expiry": float(now) + float(ttl_seconds),
    }
    return {"signed": signed, "signature": _signature(secret, signed)}


def verify_ingest_probe(envelope: dict, *, keyring: dict, now: float,
                        nonce_burn: Callable[[str, float], bool],
                        max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
                        expected_account_id: Optional[int] = None) -> dict:
    """Verify a signed probe envelope. PURE except the injected ``now`` + ``nonce_burn``. Raises
    ``IngestProbeError(reason)`` on any failure; returns ``{"signed": <validated signed dict>}`` on success.

    The nonce is burned only AFTER the signature verifies (so a bad signature can never consume a nonce). The
    operation is pinned to ``INGEST_PROBE_OPERATION`` — any other operation is refused, so this verifier can
    never process a trade/lifecycle payload. ``expected_account_id`` (when provided) pins the account."""
    if not isinstance(envelope, dict):
        raise IngestProbeError("malformed_envelope")
    signed = envelope.get("signed")
    signature = envelope.get("signature")
    if not isinstance(signed, dict) or not isinstance(signature, str):
        raise IngestProbeError("malformed_envelope")
    for field in _SIGNED_FIELDS:
        if field not in signed:
            raise IngestProbeError("missing_signed_field")
    if signed.get("protocol_version") != INGEST_PROBE_PROTOCOL_VERSION:
        raise IngestProbeError("bad_protocol_version")
    if signed.get("operation") != INGEST_PROBE_OPERATION:
        raise IngestProbeError("bad_operation")

    try:
        account_id = int(signed["account_id"])
    except (TypeError, ValueError):
        raise IngestProbeError("bad_account_id")

    key_id = str(signed.get("key_id") or "")
    secret = keyring.get(key_id)
    if not secret:
        raise IngestProbeError("unknown_key_id")
    if not hmac.compare_digest(_signature(secret, signed), signature):
        raise IngestProbeError("bad_signature")

    try:
        ts = float(signed["timestamp"])
        expiry = float(signed["expiry"])
    except (TypeError, ValueError):
        raise IngestProbeError("bad_timestamp")
    if float(now) - ts > float(max_skew_seconds) and ts <= float(now):
        raise IngestProbeError("probe_stale")
    if ts - float(now) > float(max_skew_seconds):
        raise IngestProbeError("timestamp_in_future")
    if float(now) > expiry:
        raise IngestProbeError("probe_expired")
    if expiry - ts > float(MAX_EXPIRY_WINDOW_SECONDS):
        raise IngestProbeError("expiry_too_far")

    # Account pin (the backend supplies the authoritative fixed account id). Checked AFTER the signature so a
    # wrong-account probe still has to be correctly signed first (never an unauthenticated oracle).
    if expected_account_id is not None and account_id != int(expected_account_id):
        raise IngestProbeError("account_mismatch")

    nonce = str(signed.get("nonce") or "")
    if not nonce:
        raise IngestProbeError("missing_signed_field")
    if len(nonce) > MAX_NONCE_LEN:
        raise IngestProbeError("nonce_too_long")
    if not nonce_burn(nonce, expiry):
        raise IngestProbeError("nonce_replayed")

    return {"signed": signed}
