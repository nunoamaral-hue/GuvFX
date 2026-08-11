"""Beta Readiness Stream 7C - the host-side envelope opener (ADR-0027 sealed box, PRIVATE-key half).

The backend seals the Windows account password to the host's PUBLIC key
(``host_executor._default_seal_password`` -> ``broker_cred_envelope.seal`` with the
``HOSTED_EXECUTOR_ENC_*`` registry). The host - and ONLY the host - holds the matching PRIVATE key and opens
it here to feed ``Provision-GuvfxAccount.ps1`` over stdin. The backend cannot decrypt what it sealed.

The AAD reconstructed here is byte-identical to the seal side:
    bind_aad(operation="PROVISION_IDENTITY", runtime_uuid=f"account:{account_id}",
             correlation_id=..., nonce=...)
so a substituted/relayed envelope (wrong account, correlation, or nonce) fails AEAD auth closed. The private
key is a DISTINCT scope from the HMAC keyring (``HOSTED_EXECUTOR_ENC_PRIVKEYS``, RULE 3/6). This module never
logs plaintext, ciphertext, or key material; every failure is a sanitised ``HostProtocolError``.
"""
from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from broker_cred_envelope import EnvelopeError, bind_aad, open_envelope
from hosted_workspace.host_protocol import HostProtocolError

PROVISION_OPERATION = "PROVISION_IDENTITY"


def _parse_privkeys(raw) -> dict:
    if isinstance(raw, dict):
        d = raw
    else:
        try:
            d = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            raise ValueError("HOSTED_EXECUTOR_ENC_PRIVKEYS is not valid JSON")
    if not isinstance(d, dict) or not d:
        raise ValueError("HOSTED_EXECUTOR_ENC_PRIVKEYS must be a non-empty JSON object")
    return d


def _private_key_for(privkeys: dict, key_id: str) -> X25519PrivateKey:
    raw = privkeys.get(key_id)
    if not raw:
        raise HostProtocolError("envelope_unknown_key_id")
    try:
        return X25519PrivateKey.from_private_bytes(base64.b64decode(str(raw).encode("ascii"), validate=True))
    except Exception:  # noqa: BLE001 - malformed key material, sanitised
        raise HostProtocolError("envelope_bad_private_key")


def make_envelope_opener(enc_privkeys_raw):
    """Build the ``envelope_open`` callable ``host_agent_dispatch.dispatch`` injects. Parses the private
    keyring ONCE at construction (a malformed keyring is a startup failure), then returns a closure that opens
    exactly one sealed Windows password per PROVISION_IDENTITY request, fail-closed."""
    privkeys = _parse_privkeys(enc_privkeys_raw)

    def envelope_open(payload, *, account_id, correlation_id, nonce) -> bytes:
        if not isinstance(payload, dict):
            raise HostProtocolError("envelope_missing")
        key_id = str(payload.get("key_id") or "")
        if not key_id:
            raise HostProtocolError("envelope_no_key_id")
        priv = _private_key_for(privkeys, key_id)
        aad = bind_aad(operation=PROVISION_OPERATION, runtime_uuid=f"account:{int(account_id)}",
                       correlation_id=str(correlation_id), nonce=str(nonce))
        try:
            return open_envelope(payload, aad=aad, recipient_private_key=priv)
        except EnvelopeError:
            raise HostProtocolError("envelope_open_failed")

    return envelope_open
