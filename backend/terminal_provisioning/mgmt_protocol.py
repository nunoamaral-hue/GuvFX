"""CVM-Inc-3 B — beta provisioning management-channel PROTOCOL (shared backend↔agent contract).

The backend NEVER transmits PowerShell, command lines, executable paths, filesystem paths, terminal
arguments, task/service definitions or arbitrary environment variables (Nuno requirement 1). A request
carries ONLY this fixed schema; the Windows agent maps the allowlisted ``operation`` to locally
installed, version-controlled implementation code.

Security (requirement 4): every request is signed (HMAC-SHA256 over the canonical body, constant-time
compare), timestamped with bounded skew, short-lived (expiry), and single-use (nonce). Signing keys are
addressed by ``key_id`` so keys can be rotated (verify accepts any known key; sign uses the active key).
"""
import hashlib
import hmac
import json
import secrets

PROTOCOL_VERSION = 1

# The provisioning operations the protocol may carry. TOMBSTONE (quarantine) replaces destructive
# TEARDOWN for the first production walk (requirement 2). No arbitrary delete is expressible.
PROVISIONING_OPERATIONS = ("MATERIALISE", "START", "VERIFY", "STOP", "TOMBSTONE")
# NEGOTIATE is a read-only, authenticated handshake (no runtime side-effect) the backend MUST perform to
# agree protocol/agent/manifest versions + supported operations before sending any provisioning request
# (versioned-contract requirement). It is signed like any request but touches no runtime.
HANDSHAKE_OPERATIONS = ("NEGOTIATE",)
ALLOWED_OPERATIONS = PROVISIONING_OPERATIONS + HANDSHAKE_OPERATIONS

# runtime_uuid placeholder for the (runtime-less) NEGOTIATE handshake.
NIL_UUID = "00000000-0000-0000-0000-000000000000"

# Fields covered by the signature (the canonical body). Deliberately excludes ``signature`` itself and
# any free-form payload — there is nowhere to smuggle a command, path or argument.
_SIGNED_FIELDS = ("protocol_version", "provisioning_job_id", "runtime_uuid", "operation",
                  "timestamp", "expiry", "nonce", "correlation_id", "key_id")

DEFAULT_TTL_SECONDS = 30          # short expiry
DEFAULT_MAX_SKEW_SECONDS = 30     # bounded clock skew


class ProtocolError(Exception):
    """Request failed protocol validation. ``reason_code`` is user-safe/sanitised."""
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _canonical_body(fields: dict) -> bytes:
    """Deterministic canonical serialisation of the signed fields (sorted keys, compact separators)."""
    return json.dumps({k: fields[k] for k in _SIGNED_FIELDS}, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sign_request(*, provisioning_job_id, runtime_uuid, operation, correlation_id, keyring: dict,
                 key_id: str, now: int, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 nonce: str | None = None) -> dict:
    """Build a fully-signed request dict. ``now`` is an integer epoch (caller supplies ``timezone``-based
    time). ``keyring`` maps key_id → secret; ``key_id`` selects the active signing key."""
    if operation not in ALLOWED_OPERATIONS:
        raise ProtocolError("operation_not_allowed")
    if key_id not in keyring:
        raise ProtocolError("unknown_key_id")
    fields = {
        "protocol_version": PROTOCOL_VERSION,
        "provisioning_job_id": int(provisioning_job_id),
        "runtime_uuid": str(runtime_uuid),
        "operation": operation,
        "timestamp": int(now),
        "expiry": int(now) + int(ttl_seconds),
        "nonce": nonce or secrets.token_hex(16),
        "correlation_id": str(correlation_id),
        "key_id": str(key_id),
    }
    fields["signature"] = hmac.new(
        keyring[key_id].encode("utf-8"), _canonical_body(fields), hashlib.sha256).hexdigest()
    return fields


def verify_request(request: dict, *, keyring: dict, now: int, nonce_seen, nonce_remember,
                   max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS) -> dict:
    """Independently validate a request (used by the Windows agent). Raises ``ProtocolError`` on any
    failure; returns the validated signed fields on success.

    ``nonce_seen(nonce) -> bool`` and ``nonce_remember(nonce, expiry)`` are the durable single-use nonce
    store (persisted across agent restarts). Nonce is only remembered AFTER the signature verifies, so an
    attacker cannot burn a victim's nonce with an unsigned request."""
    if not isinstance(request, dict):
        raise ProtocolError("malformed_request")
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_protocol_version")
    op = request.get("operation")
    if op not in ALLOWED_OPERATIONS:
        raise ProtocolError("operation_not_allowed")
    # every signed field must be present (and nothing else is trusted)
    for f in _SIGNED_FIELDS:
        if f not in request:
            raise ProtocolError("missing_field")
    sig = request.get("signature")
    if not isinstance(sig, str) or not sig:
        raise ProtocolError("missing_signature")

    key_id = request["key_id"]
    if key_id not in keyring:
        raise ProtocolError("unknown_key_id")

    ts, exp = request["timestamp"], request["expiry"]
    if not isinstance(ts, int) or not isinstance(exp, int):
        raise ProtocolError("malformed_time")
    if abs(int(now) - ts) > max_skew_seconds:
        raise ProtocolError("timestamp_skew")
    if int(now) > exp:
        raise ProtocolError("request_expired")
    if exp - ts > 600:                       # sanity: reject absurdly long-lived tokens
        raise ProtocolError("expiry_too_far")

    expected = hmac.new(keyring[key_id].encode("utf-8"), _canonical_body(request),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):    # constant-time
        raise ProtocolError("bad_signature")

    # Replay: only AFTER the signature is proven do we consult/burn the durable nonce store.
    if nonce_seen(request["nonce"]):
        raise ProtocolError("nonce_replayed")
    nonce_remember(request["nonce"], exp)

    return {k: request[k] for k in _SIGNED_FIELDS}
