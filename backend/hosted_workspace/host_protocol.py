"""hosted_workspace.host_protocol — Beta Readiness Stream 5: the signed backend↔host provisioning CONTRACT.

The narrow, allow-listed wire contract for `prepare_hosted_slot`'s host-executor seam. It mirrors the proven
`terminal_provisioning.mgmt_protocol` construction (HMAC-SHA256 over a canonical body, bounded skew, short
expiry, single-use nonce, rotatable key_id, constant-time compare) but carries a HOSTED slot identity —
``account_id`` — NOT a runtime UUID / generation / ProvisioningJob (the Windows-primitive boundary,
architecture.md: a primitive that needs a UUID/job is a design error — pass it the slot).

What CANNOT be expressed on this wire (Nuno requirement / no-RCE): a PowerShell string, a command line, an
executable path, a filesystem path, a username, a task/service definition, or arbitrary env. A request carries
ONLY: ``operation`` (from a fixed allow-list) + ``account_id`` (an int) + a small typed ``params`` dict (bound
to the signature via ``params_digest``) + optionally a sealed credential ``payload`` (PROVISION_IDENTITY's
Windows password, sealed with the ADR-0027 envelope and bound via ``payload_digest``). The host derives the
Windows identity and every path from ``account_id`` server-side — there is nowhere to smuggle a path or command.

This module is self-contained (RULE 3 corollary): the Django side imports it; the host-side dispatcher
implements the identical contract locally (it runs as a separate host service and does not import backend). The
two MUST agree; they share this file's construction by copy on the host, exactly like the standalone bridge and
validate worker mirror ``mgmt_protocol``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets

HOSTED_PROTOCOL_VERSION = 1

# The provisioning operations the host may be asked to perform. Each maps, on the host, to exactly ONE
# reviewed, version-controlled primitive (see host_agent_dispatch.OP_PRIMITIVES). No arbitrary operation.
HOSTED_OPERATIONS = (
    "PROVISION_IDENTITY",        # New-LocalUser guvfx_u_<id> (non-admin) + runtime folder tree
    "APPLY_WORKSPACE_ACL",       # G5: exactly SYSTEM+Administrators+user, inheritance broken, read-back
    "ROLLBACK_WORKSPACE_ACL",    # restore the pre-apply DACL snapshot
    "MATERIALISE_RUNTIME",       # copy the golden clean portable MT5 into runtime_root\terminal
    "APPLY_AUTOTRADING_CONFIG",  # write [Experts] AllowLiveTrading=1 Enabled=1 (CAPABILITY only) to common.ini
    "ENSURE_RDP_MEMBERSHIP",     # add guvfx_u_<id> to Remote Desktop Users (scoped)
    "ENSURE_SINGLE_SESSION",     # fSingleSessionPerUser=1 (host-global, idempotent)
    "ENSURE_REMOTEAPP",          # publish/verify the per-account RemoteApp alias (guvfx_mt5_<id>) /portable
    "REMOVE_REMOTEAPP",          # tenant rollback: remove ONLY this account's RemoteApp alias
    "PREPARE_OBSERVER",          # register the read-only session-bound observer task
    "OBSERVE_WORKSPACE",         # 9E: trigger the account's session-bound observer once + return its snapshot (RO)
    "APPLY_APPLOCKER_AUDIT",     # AppLocker AuditOnly TENANT MERGE for this identity (additive; NEVER -Enforce)
    "REMOVE_APPLOCKER_TENANT",   # tenant rollback: remove ONLY this account's AppLocker deny contribution
    "VERIFY_SLOT",               # read-only re-verification of the whole slot
    "ACTIVATE_ORDER_BRIDGE",     # start THIS node's dedicated pin-enforcing order bridge + health-check (server-derived slot)
    "ACTIVATE_TENANT_BRIDGE",    # P0-B1.1: start THIS tenant's OWN pin-enforcing order bridge on its per-tenant PORT (multi-tenant host)
    "RELAUNCH_TERMINAL",         # AJ#6.3: graceful in-session close+relaunch of THIS tenant's own MT5 (capability recovery; NEVER CZ; no order)
)
# Operations that carry a sealed credential payload (the Windows account password). Additive.
CREDENTIALED_HOSTED_OPERATIONS = ("PROVISION_IDENTITY",)

# Fields covered by the signature. Excludes ``signature`` itself and the opaque ``payload`` (bound via digest).
_SIGNED_FIELDS = ("protocol_version", "account_id", "operation", "params_digest",
                  "timestamp", "expiry", "nonce", "correlation_id", "key_id")

DEFAULT_TTL_SECONDS = 30
DEFAULT_MAX_SKEW_SECONDS = 30
_MAX_EXPIRY_WINDOW = 600


class HostProtocolError(Exception):
    """Request failed protocol validation. ``reason_code`` is user-safe / sanitised (never a secret)."""
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _digest(obj) -> str:
    body = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def params_digest_of(params: dict | None) -> str:
    """SHA-256 over the canonical typed params (a small dict of scalars). Signed, so any tamper fails closed."""
    return _digest(params or {})


def payload_digest_of(payload: dict | None) -> str:
    """SHA-256 over the sealed credential envelope. Signed, so a substituted envelope fails the signature."""
    return _digest(payload or {})


def _canonical_body(fields: dict) -> bytes:
    keys = list(_SIGNED_FIELDS)
    if "payload_digest" in fields:
        keys.append("payload_digest")
    return json.dumps({k: fields[k] for k in keys}, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_hosted_request(*, account_id, operation, correlation_id, keyring: dict, key_id: str, now: int,
                        params: dict | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                        nonce: str | None = None, payload: dict | None = None) -> dict:
    """Build a fully-signed hosted request. ``account_id`` is the slot identity (an int). ``params`` are typed
    scalars bound via ``params_digest``. ``payload`` (a SEALED envelope for PROVISION_IDENTITY) is carried
    verbatim and bound via ``payload_digest`` — never a signed field, so nothing free-form is trusted."""
    if operation not in HOSTED_OPERATIONS:
        raise HostProtocolError("operation_not_allowed")
    if key_id not in keyring:
        raise HostProtocolError("unknown_key_id")
    try:
        acct = int(account_id)
    except (TypeError, ValueError):
        raise HostProtocolError("account_id_not_int")
    if acct <= 0:
        raise HostProtocolError("account_id_out_of_range")
    fields = {
        "protocol_version": HOSTED_PROTOCOL_VERSION,
        "account_id": acct,
        "operation": operation,
        "params_digest": params_digest_of(params),
        "timestamp": int(now),
        "expiry": int(now) + int(ttl_seconds),
        "nonce": nonce or secrets.token_hex(16),
        "correlation_id": str(correlation_id),
        "key_id": str(key_id),
    }
    fields["params"] = dict(params or {})           # carried, bound via params_digest (never signed directly)
    if payload is not None:
        fields["payload_digest"] = payload_digest_of(payload)
        fields["payload"] = payload                  # sealed envelope, carried; bound via payload_digest
    elif operation in CREDENTIALED_HOSTED_OPERATIONS:
        raise HostProtocolError("payload_required")
    fields["signature"] = hmac.new(
        keyring[key_id].encode("utf-8"), _canonical_body(fields), hashlib.sha256).hexdigest()
    return fields


def verify_hosted_request(request: dict, *, keyring: dict, now: int, nonce_burn,
                          max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS) -> dict:
    """Independently validate a request (host side). Raises ``HostProtocolError`` on ANY failure; returns the
    validated signed fields + carried params on success. ``nonce_burn(nonce, expiry) -> bool`` is the durable
    single-use store (True first use, False replay), called ONLY after the signature verifies."""
    if not isinstance(request, dict):
        raise HostProtocolError("malformed_request")
    if request.get("protocol_version") != HOSTED_PROTOCOL_VERSION:
        raise HostProtocolError("unsupported_protocol_version")
    op = request.get("operation")
    if op not in HOSTED_OPERATIONS:
        raise HostProtocolError("operation_not_allowed")
    for f in _SIGNED_FIELDS:
        if f not in request:
            raise HostProtocolError("missing_field")
    sig = request.get("signature")
    if not isinstance(sig, str) or not sig:
        raise HostProtocolError("missing_signature")
    key_id = request["key_id"]
    if key_id not in keyring:
        raise HostProtocolError("unknown_key_id")
    if not isinstance(request["account_id"], int) or request["account_id"] <= 0:
        raise HostProtocolError("account_id_invalid")

    ts, exp = request["timestamp"], request["expiry"]
    if not isinstance(ts, int) or not isinstance(exp, int):
        raise HostProtocolError("malformed_time")
    if abs(int(now) - ts) > max_skew_seconds:
        raise HostProtocolError("timestamp_skew")
    if int(now) > exp:
        raise HostProtocolError("request_expired")
    if exp - ts > _MAX_EXPIRY_WINDOW:
        raise HostProtocolError("expiry_too_far")

    expected = hmac.new(keyring[key_id].encode("utf-8"), _canonical_body(request), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HostProtocolError("bad_signature")

    # Params are bound to the (now-verified) signature via params_digest — recompute so a substituted params
    # dict fails HERE. (The signed body already commits to params_digest; this catches a stripped/edited params.)
    if not hmac.compare_digest(params_digest_of(request.get("params") or {}), str(request["params_digest"])):
        raise HostProtocolError("params_digest_mismatch")

    # Credentialed op: the sealed payload is bound via payload_digest (its AEAD/AAD independently re-binds the
    # request context on open). Require it and recompute; a substituted envelope fails here.
    if op in CREDENTIALED_HOSTED_OPERATIONS or "payload_digest" in request:
        payload = request.get("payload")
        if not isinstance(payload, dict) or "payload_digest" not in request:
            raise HostProtocolError("payload_missing")
        if not hmac.compare_digest(payload_digest_of(payload), str(request["payload_digest"])):
            raise HostProtocolError("payload_digest_mismatch")

    if not nonce_burn(request["nonce"], exp):
        raise HostProtocolError("nonce_replayed")

    out = {k: request[k] for k in _SIGNED_FIELDS}
    out["params"] = dict(request.get("params") or {})
    if "payload" in request:
        out["payload"] = request["payload"]
    return out


# ── Response authentication ────────────────────────────────────────────────────────────────────────────────
# The host signs its response so the backend can trust the read-back (e.g. the G5 ACL rows it verifies). The
# response HMAC binds the result to the request's correlation_id + nonce, so a MITM cannot forge a "clean" ACL.
def sign_hosted_response(*, result: dict, correlation_id: str, nonce: str, keyring: dict, key_id: str) -> dict:
    if key_id not in keyring:
        raise HostProtocolError("unknown_key_id")
    body = {"result": result, "correlation_id": str(correlation_id), "nonce": str(nonce), "key_id": str(key_id)}
    body["signature"] = hmac.new(keyring[key_id].encode("utf-8"),
                                 _digest(body).encode("utf-8"), hashlib.sha256).hexdigest()
    return body


def verify_hosted_response(response: dict, *, correlation_id: str, nonce: str, keyring: dict) -> dict:
    if not isinstance(response, dict):
        raise HostProtocolError("malformed_response")
    key_id = response.get("key_id")
    if key_id not in keyring:
        raise HostProtocolError("unknown_key_id")
    if str(response.get("correlation_id")) != str(correlation_id) or str(response.get("nonce")) != str(nonce):
        raise HostProtocolError("response_context_mismatch")
    sig = response.get("signature")
    if not isinstance(sig, str) or not sig:
        raise HostProtocolError("missing_signature")
    body = {k: response[k] for k in ("result", "correlation_id", "nonce", "key_id") if k in response}
    expected = hmac.new(keyring[key_id].encode("utf-8"), _digest(body).encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HostProtocolError("bad_response_signature")
    result = response.get("result")
    if not isinstance(result, dict):
        raise HostProtocolError("malformed_result")
    return result
