"""trade_copier.emitter.attestation — one-shot identity-attestation envelope (ADR-0077). Pure, Django-free.

The emitter proves — before observing anything — that it is the right build, on the right host, bound to the
right GuvFX account/broker-identity/terminal/bridge. It mirrors the audited ``event_ingestion`` HMAC idiom
(canonical-JSON signed field set, HMAC-SHA256, ``hmac.compare_digest``, bounded skew + expiry, single-use nonce)
and signs with the emitter's OWN dedicated key (``COPIER_INGEST_HMAC_KEY``) — never a broker/agent/DB/Tailscale
secret. The envelope carries only identity FINGERPRINTS (the broker login is hashed, never raw). Verifying an
attestation creates NO lifecycle state, MasterEvent, ExecutionJob, alert or broker activity — it is a pure
identity proof. Negative controls (wrong host/account/server/terminal/bridge, stale/future timestamp, replay,
tamper, bad signature, unsupported build/protocol) all fail closed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Callable, Optional

ATTEST_PROTOCOL_VERSION = "copier-attest-1"
ATTEST_OPERATION = "copier_attestation"
DEFAULT_MAX_SKEW_SECONDS = 30
MAX_EXPIRY_WINDOW_SECONDS = 600
MAX_NONCE_LEN = 80

_SIGNED_FIELDS = (
    "protocol_version", "operation", "key_id", "build_sha", "host_id", "account_id",
    "broker_login_fp", "broker_server", "terminal_fp", "bridge_id", "config_digest",
    "observed_login_fp", "nonce", "timestamp", "expiry",
)


class AttestError(Exception):
    """A sanitised attestation rejection. ``reason`` is a stable code (never a secret)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def fingerprint(value) -> str:
    """Stable, non-reversible fingerprint of an identity value (e.g. a broker login). Never the raw value."""
    return hashlib.sha256(str(value or "").strip().encode("utf-8")).hexdigest()[:32]


def canonical_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _signature(secret: str, signed: dict) -> str:
    body = canonical_bytes({k: signed[k] for k in _SIGNED_FIELDS})
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def build_attestation(*, secret: str, account_id: int, broker_login: str, broker_server: str, host_id: str,
                      terminal_fp: str, bridge_id: str, build_sha: str, config_digest: str, nonce: str, now: float,
                      key_id: str = "copier-ingest", ttl_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
                      observed_login: Optional[str] = None) -> dict:
    """Build a signed attestation envelope. ``broker_login``/``observed_login`` are fingerprinted, never carried
    raw. ``observed_login`` (optional) is the live identity observed via ``/mt5/snapshots/account`` (NO positions
    read) — binding the terminal the emitter actually reached to the expected account."""
    signed = {
        "protocol_version": ATTEST_PROTOCOL_VERSION,
        "operation": ATTEST_OPERATION,
        "key_id": str(key_id),
        "build_sha": str(build_sha or ""),
        "host_id": str(host_id or ""),
        "account_id": int(account_id),
        "broker_login_fp": fingerprint(broker_login),
        "broker_server": str(broker_server or ""),
        "terminal_fp": str(terminal_fp or ""),
        "bridge_id": str(bridge_id or ""),
        "config_digest": str(config_digest or ""),
        "observed_login_fp": (fingerprint(observed_login) if observed_login else ""),
        "nonce": str(nonce),
        "timestamp": float(now),
        "expiry": float(now) + float(ttl_seconds),
    }
    return {"signed": signed, "signature": _signature(secret, signed)}


def verify_attestation(envelope: dict, *, keyring: dict, now: float,
                       nonce_burn: Callable[[str, float], bool],
                       max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
                       expected: Optional[dict] = None) -> dict:
    """Verify a signed attestation envelope and (optionally) cross-check it against the expected identity.

    PURE except the injected ``now`` + ``nonce_burn``. Raises ``AttestError(reason)`` on any failure; returns the
    validated ``signed`` dict on success. The nonce is burned only AFTER the signature verifies. ``expected`` (a
    dict of authoritative identity fingerprints the BACKEND builds from the DB) drives the wrong-host/account/
    server/terminal/bridge/build negative controls — a None/omitted expected field is not checked."""
    if not isinstance(envelope, dict):
        raise AttestError("malformed_envelope")
    signed = envelope.get("signed")
    signature = envelope.get("signature")
    if not isinstance(signed, dict) or not isinstance(signature, str):
        raise AttestError("malformed_envelope")
    for field in _SIGNED_FIELDS:
        if field not in signed:
            raise AttestError("missing_signed_field")
    if signed.get("protocol_version") != ATTEST_PROTOCOL_VERSION:
        raise AttestError("bad_protocol_version")
    if signed.get("operation") != ATTEST_OPERATION:
        raise AttestError("bad_operation")

    key_id = str(signed.get("key_id") or "")
    secret = keyring.get(key_id)
    if not secret:
        raise AttestError("unknown_key_id")
    if not hmac.compare_digest(_signature(secret, signed), signature):
        raise AttestError("bad_signature")

    try:
        ts = float(signed["timestamp"])
        expiry = float(signed["expiry"])
    except (TypeError, ValueError):
        raise AttestError("bad_timestamp")
    if float(now) - ts > float(max_skew_seconds) and ts <= float(now):
        raise AttestError("attestation_stale")
    if ts - float(now) > float(max_skew_seconds):
        raise AttestError("timestamp_in_future")
    if float(now) > expiry:
        raise AttestError("attestation_expired")
    if expiry - ts > float(MAX_EXPIRY_WINDOW_SECONDS):
        raise AttestError("expiry_too_far")

    # Expected-identity cross-check (the backend supplies authoritative values from the DB).
    if expected:
        if "account_id" in expected and int(signed["account_id"]) != int(expected["account_id"]):
            raise AttestError("account_mismatch")
        if expected.get("broker_login_fp") and signed["broker_login_fp"] != expected["broker_login_fp"]:
            raise AttestError("broker_login_mismatch")
        if expected.get("broker_server") and \
                str(signed["broker_server"]).strip().casefold() != str(expected["broker_server"]).strip().casefold():
            raise AttestError("broker_server_mismatch")
        if expected.get("host_id") and str(signed["host_id"]) != str(expected["host_id"]):
            raise AttestError("host_mismatch")
        if expected.get("terminal_fp") and str(signed["terminal_fp"]) != str(expected["terminal_fp"]):
            raise AttestError("terminal_mismatch")
        if expected.get("bridge_id") and str(signed["bridge_id"]) != str(expected["bridge_id"]):
            raise AttestError("bridge_mismatch")
        allow = expected.get("build_sha_allow")
        if allow and str(signed["build_sha"]) not in set(allow):
            raise AttestError("build_not_allowed")
        obs = expected.get("observed_login_fp")
        if obs and signed.get("observed_login_fp") and signed["observed_login_fp"] != obs:
            raise AttestError("observed_login_mismatch")

    nonce = str(signed.get("nonce") or "")
    if not nonce:
        raise AttestError("missing_signed_field")
    if len(nonce) > MAX_NONCE_LEN:
        raise AttestError("nonce_too_long")
    if not nonce_burn(nonce, expiry):
        raise AttestError("nonce_replayed")

    return {"signed": signed}
