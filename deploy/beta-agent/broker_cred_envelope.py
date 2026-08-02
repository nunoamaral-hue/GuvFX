"""ADR-0027 — broker-credential ENVELOPE crypto, AGENT copy (Django-free).

Byte-for-byte the SAME construction as the backend module
(``backend/terminal_provisioning/broker_cred_envelope.py``): ephemeral-static ECIES / sealed box —
X25519 ECDH → HKDF-SHA256 → AES-256-GCM whose AAD binds the ciphertext to
(operation, runtime_uuid, correlation_id, nonce). A parity test asserts ``bind_aad`` produces identical
bytes on both sides, so a ciphertext sealed by the backend opens here and nowhere else.

The ONLY difference from the backend copy is the key SOURCE: the backend reads Django settings/env; this
standalone agent has no Django, so keys are resolved from the process environment (an approved Windows
secret mechanism at deploy time). The agent holds the PRIVATE key (open only); it never seals in
production. Missing / unknown / malformed keys FAIL CLOSED. This module never logs plaintext, ciphertext,
or key material.
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ENVELOPE_VERSION = 1
_HKDF_INFO = b"guvfx-broker-cred-envelope-v1"
_KEY_LEN = 32          # AES-256


class EnvelopeError(Exception):
    """Sealing/opening failed. ``reason_code`` is user-safe/sanitised — never contains key or plaintext."""
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"), validate=True)


def bind_aad(*, operation: str, runtime_uuid: str, correlation_id: str, nonce: str) -> bytes:
    """Canonical, deterministic AAD binding the sealed password to THIS request. MUST be byte-identical to
    the backend's ``bind_aad`` — the agent recomputes it from the outer (HMAC-verified) request and any
    mismatch fails AEAD auth closed."""
    return json.dumps(
        {"op": str(operation), "runtime_uuid": str(runtime_uuid),
         "correlation_id": str(correlation_id), "nonce": str(nonce), "v": ENVELOPE_VERSION},
        sort_keys=True, separators=(",", ":")).encode("utf-8")


# ── key registry (PRIVATE key = the agent's role; env-sourced; never logged) ───────────────────────────────
def _load_json_env(name: str, env: dict | None = None) -> dict:
    raw = (env if env is not None else os.environ).get(name, "")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        raise EnvelopeError("envelope_keyring_malformed")


def recipient_private_key_for(key_id: str, env: dict | None = None) -> X25519PrivateKey:
    """The agent's PRIVATE key for ``key_id`` (open only). Fail closed on unknown/malformed."""
    privs = _load_json_env("BROKER_CRED_ENC_PRIVKEYS", env)
    raw = privs.get(key_id)
    if not raw:
        raise EnvelopeError("envelope_unknown_key_id")
    try:
        return X25519PrivateKey.from_private_bytes(_b64d(str(raw)))
    except Exception:
        raise EnvelopeError("envelope_bad_private_key")


def agent_enc_configured(env: dict | None = None) -> bool:
    """True iff at least one private key is present (boolean presence only — never the key)."""
    try:
        return bool(_load_json_env("BROKER_CRED_ENC_PRIVKEYS", env))
    except EnvelopeError:
        return False


def seal(plaintext: bytes, *, aad: bytes, recipient_public_key: X25519PublicKey) -> dict:
    """Seal ``plaintext`` to a recipient PUBLIC key, bound to ``aad`` — same construction as the backend.
    Kept for the cross-side parity test; the production agent never seals (it has no recipient pubkey). The
    ephemeral private key is DISCARDED on return."""
    if not isinstance(plaintext, (bytes, bytearray)):
        raise EnvelopeError("envelope_plaintext_not_bytes")
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(recipient_public_key)
    key = HKDF(algorithm=hashes.SHA256(), length=_KEY_LEN, salt=None, info=_HKDF_INFO).derive(shared)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, bytes(plaintext), aad)
    return {"v": ENVELOPE_VERSION, "key_id": "", "epk": _b64e(eph.public_key().public_bytes_raw()),
            "nonce": _b64e(nonce), "ct": _b64e(ct)}


def open_envelope(envelope: dict, *, aad: bytes,
                  recipient_private_key: X25519PrivateKey | None = None,
                  env: dict | None = None) -> bytes:
    """Open a sealed envelope, verifying the ``aad`` binding. The private key is INJECTED (tests) or resolved
    from the environment by the envelope's own ``key_id`` (production). Fail closed on any malformation,
    unknown key, tamper, or aad mismatch — every raise is an ``EnvelopeError`` with a sanitised code."""
    if not isinstance(envelope, dict) or envelope.get("v") != ENVELOPE_VERSION:
        raise EnvelopeError("envelope_bad_version")
    priv = recipient_private_key
    if priv is None:
        kid = str(envelope.get("key_id") or "")
        if not kid:
            raise EnvelopeError("envelope_no_key_id")
        priv = recipient_private_key_for(kid, env)
    try:
        eph_pub = X25519PublicKey.from_public_bytes(_b64d(str(envelope["epk"])))
        nonce = _b64d(str(envelope["nonce"]))
        ct = _b64d(str(envelope["ct"]))
    except Exception:
        raise EnvelopeError("envelope_malformed")
    shared = priv.exchange(eph_pub)
    key = HKDF(algorithm=hashes.SHA256(), length=_KEY_LEN, salt=None, info=_HKDF_INFO).derive(shared)
    try:
        return AESGCM(key).decrypt(nonce, ct, aad)      # aad mismatch / tamper → InvalidTag
    except Exception:
        raise EnvelopeError("envelope_auth_failed")
