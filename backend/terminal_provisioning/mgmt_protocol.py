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
# RELEASE (ADR 0014) is the authoritative completion of the lifecycle and the ONLY operation permitted to
# transition Released -> Available (advance the slot generation + free the slot). It runs OUTSIDE the
# per-runtime mutation lock; it touches no filesystem. Added under protocol_version 1 and advertised via
# NEGOTIATE's supported_operations, so an agent that predates it simply omits it (backward compatible).
PROVISIONING_OPERATIONS = ("MATERIALISE", "START", "VERIFY", "STOP", "TOMBSTONE", "RELEASE")
# CREDENTIALED_OPERATIONS (ADR-0027) carry an envelope-encrypted broker password bound to the signature via
# ``payload_digest``; additive + advertised in NEGOTIATE, so an agent that predates them simply omits them.
# VALIDATE_LOGIN is a NON-destructive, runtime-independent login probe against a dedicated isolated
# validation terminal — it touches no slot and no provisioning lifecycle.
CREDENTIALED_OPERATIONS = ("VALIDATE_LOGIN",)
# NEGOTIATE is a read-only, authenticated handshake (no runtime side-effect) the backend MUST perform to
# agree protocol/agent/manifest versions + supported operations before sending any provisioning request
# (versioned-contract requirement). It is signed like any request but touches no runtime.
HANDSHAKE_OPERATIONS = ("NEGOTIATE",)
# Operations an agent advertises in NEGOTIATE (lifecycle + credentialed; NEGOTIATE itself is implicit).
SUPPORTED_OPERATIONS = PROVISIONING_OPERATIONS + CREDENTIALED_OPERATIONS
ALLOWED_OPERATIONS = PROVISIONING_OPERATIONS + CREDENTIALED_OPERATIONS + HANDSHAKE_OPERATIONS

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


# The SEMANTIC identity of a request for idempotency/conflict purposes — the fields that define WHAT the
# operation does. Deliberately excludes nonce/timestamp/expiry/correlation_id (those legitimately differ
# between a request and its retry) so a resend is idempotent while a genuine conflict (same job+op, other
# runtime/version) fails closed.
_SEMANTIC_FIELDS = ("protocol_version", "provisioning_job_id", "runtime_uuid", "operation")


def _canonical_body(fields: dict) -> bytes:
    """Deterministic canonical serialisation of the signed fields (sorted keys, compact separators). For a
    credentialed op the ``payload_digest`` (a SHA-256 over the credential payload) is folded in when present,
    binding the encrypted-password payload to the signature; lifecycle ops (no payload) sign the identical
    body as before — byte-compatible with the deployed agent."""
    keys = list(_SIGNED_FIELDS)
    if "payload_digest" in fields:
        keys.append("payload_digest")
    return json.dumps({k: fields[k] for k in keys}, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def payload_digest_of(payload) -> str:
    """SHA-256 over the canonical credential payload (login/server/enc_key_id/password envelope). Signed via
    ``payload_digest`` and re-checked by the agent, so any field substitution/tamper fails the signature."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def semantic_digest(fields: dict) -> str:
    """SHA-256 over the SEMANTIC fields — the idempotency/conflict key for (job_id, operation)."""
    body = json.dumps({k: fields[k] for k in _SEMANTIC_FIELDS}, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def sign_request(*, provisioning_job_id, runtime_uuid, operation, correlation_id, keyring: dict,
                 key_id: str, now: int, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 nonce: str | None = None, payload: dict | None = None) -> dict:
    """Build a fully-signed request dict. ``now`` is an integer epoch (caller supplies ``timezone``-based
    time). ``keyring`` maps key_id → secret; ``key_id`` selects the active signing key. ``payload`` (a
    credential payload for a credentialed op) is carried verbatim and BOUND to the signature via a signed
    ``payload_digest`` — the payload itself is never a signed field, so nothing free-form is trusted."""
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
    if payload is not None:
        fields["payload_digest"] = payload_digest_of(payload)   # bound to the signature (below)
        fields["payload"] = payload                              # carried, NOT signed directly
    elif operation in CREDENTIALED_OPERATIONS:
        raise ProtocolError("payload_required")
    fields["signature"] = hmac.new(
        keyring[key_id].encode("utf-8"), _canonical_body(fields), hashlib.sha256).hexdigest()
    return fields


def verify_request(request: dict, *, keyring: dict, now: int, nonce_burn,
                   max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS) -> dict:
    """Independently validate a request (used by the Windows agent). Raises ``ProtocolError`` on any
    failure; returns the validated signed fields on success.

    ``nonce_burn(nonce, expiry) -> bool`` is the durable single-use nonce store (persisted across agent
    restarts): it ATOMICALLY records the nonce and returns True on first use, False on replay — so two
    concurrent identical requests cannot both pass. It is called only AFTER the signature verifies, so an
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

    # Credentialed op: the carried payload (envelope-encrypted password + login/server + enc_key_id) is bound
    # to the (now-verified) signature via ``payload_digest``. Require it and recompute — a substituted payload
    # fails HERE, before the agent ever opens the envelope (whose AAD independently binds the same request
    # context). Stripping ``payload_digest`` would already have failed the signature above.
    if op in CREDENTIALED_OPERATIONS or "payload_digest" in request:
        payload = request.get("payload")
        if not isinstance(payload, dict) or "payload_digest" not in request:
            raise ProtocolError("payload_missing")
        if not hmac.compare_digest(payload_digest_of(payload), str(request["payload_digest"])):
            raise ProtocolError("payload_digest_mismatch")

    # Replay: only AFTER the signature is proven do we atomically burn the durable nonce. A single-call
    # atomic burn (first-use → True, else False) closes the check-then-set race between concurrent
    # identical requests.
    if not nonce_burn(request["nonce"], exp):
        raise ProtocolError("nonce_replayed")

    return {k: request[k] for k in _SIGNED_FIELDS}
