"""ADR-0027 — broker-credential ENVELOPE encryption for the in-place login-validation primitive.

Defense-in-depth ABOVE the HMAC-signed request + Tailscale tunnel: the customer's broker password is
sealed to the Windows agent's PUBLIC key so the backend can encrypt but **cannot decrypt** what it just
built, and only the agent (holding the matching private key) can open it, at point of use.

Construction: a standard **ephemeral-static ECIES / sealed box** using only vetted primitives from
``cryptography`` (no custom crypto, no new dependency):

  * X25519 ECDH between a **fresh ephemeral** sender key (discarded immediately — the backend keeps no way
    to decrypt, and each message is forward-secret) and the agent's static recipient key;
  * HKDF-SHA256 to derive a 256-bit AEAD key;
  * AES-256-GCM, whose **AAD binds the ciphertext to (operation, runtime_uuid, correlation_id, nonce)** — a
    ciphertext lifted onto a different request fails authentication (anti-replay / anti-rebind).

Keys are a **distinct scope from the HMAC signing keyring** (`security.md` RULE 6), addressed by ``key_id``
for rotation, loaded from settings/env, never logged. Missing / unknown / malformed keys **fail closed**.
This module NEVER logs plaintext, ciphertext, or key material.
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
    """Canonical, deterministic additional-authenticated-data binding the sealed password to THIS request.
    The agent recomputes it from the outer (HMAC-verified) request; any mismatch fails AEAD auth closed."""
    return json.dumps(
        {"op": str(operation), "runtime_uuid": str(runtime_uuid),
         "correlation_id": str(correlation_id), "nonce": str(nonce), "v": ENVELOPE_VERSION},
        sort_keys=True, separators=(",", ":")).encode("utf-8")


# ── key registries (distinct from the HMAC keyring; loaded lazily; never logged) ───────────────────────────
def _load_json_setting(name: str) -> dict:
    from django.conf import settings
    raw = getattr(settings, name, None) or os.getenv(name, "")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        raise EnvelopeError("envelope_keyring_malformed")


def _active_key_id() -> str:
    from django.conf import settings
    return (getattr(settings, "BROKER_CRED_ENC_KEY_ID", None) or os.getenv("BROKER_CRED_ENC_KEY_ID", "")).strip()


def _recipient_public_key(key_id: str) -> X25519PublicKey:
    """The agent's PUBLIC key for ``key_id`` (backend side — seal only). Fail closed on unknown/malformed."""
    pubs = _load_json_setting("BROKER_CRED_ENC_PUBKEYS")
    raw = pubs.get(key_id)
    if not raw:
        raise EnvelopeError("envelope_unknown_key_id")
    try:
        return X25519PublicKey.from_public_bytes(_b64d(str(raw)))
    except Exception:
        raise EnvelopeError("envelope_bad_public_key")


def _recipient_private_key(key_id: str) -> X25519PrivateKey:
    """The agent's PRIVATE key for ``key_id`` (agent side — open only). Fail closed on unknown/malformed."""
    privs = _load_json_setting("BROKER_CRED_ENC_PRIVKEYS")
    raw = privs.get(key_id)
    if not raw:
        raise EnvelopeError("envelope_unknown_key_id")
    try:
        return X25519PrivateKey.from_private_bytes(_b64d(str(raw)))
    except Exception:
        raise EnvelopeError("envelope_bad_private_key")


# ── seal (backend) / open (agent) ──────────────────────────────────────────────────────────────────────────
def seal(plaintext: bytes, *, aad: bytes, key_id: str | None = None,
         recipient_public_key: X25519PublicKey | None = None) -> dict:
    """Backend: seal ``plaintext`` (the broker password) to the agent's public key, bound to ``aad``. Returns a
    JSON-safe envelope. The ephemeral private key is DISCARDED on return, so the caller cannot decrypt what it
    built. Injecting ``recipient_public_key`` is for tests only; production resolves it from ``key_id``."""
    if not isinstance(plaintext, (bytes, bytearray)):
        raise EnvelopeError("envelope_plaintext_not_bytes")
    kid = (key_id or _active_key_id()).strip()
    if not kid:
        raise EnvelopeError("envelope_no_active_key")
    pub = recipient_public_key or _recipient_public_key(kid)
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(pub)
    key = HKDF(algorithm=hashes.SHA256(), length=_KEY_LEN, salt=None, info=_HKDF_INFO).derive(shared)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, bytes(plaintext), aad)
    eph_pub = eph.public_key().public_bytes_raw()
    # eph (and its private key) go out of scope here — the backend retains no decryption capability.
    return {"v": ENVELOPE_VERSION, "key_id": kid, "epk": _b64e(eph_pub),
            "nonce": _b64e(nonce), "ct": _b64e(ct)}


def open_envelope(envelope: dict, *, aad: bytes,
                  recipient_private_key: X25519PrivateKey | None = None) -> bytes:
    """Agent: open a sealed envelope, verifying the ``aad`` binding. Fail closed on any malformation, unknown
    key, tamper, or aad mismatch (all raise ``EnvelopeError`` with a sanitised code — never key/plaintext)."""
    if not isinstance(envelope, dict) or envelope.get("v") != ENVELOPE_VERSION:
        raise EnvelopeError("envelope_bad_version")
    kid = str(envelope.get("key_id") or "")
    if not kid:
        raise EnvelopeError("envelope_no_key_id")
    priv = recipient_private_key or _recipient_private_key(kid)
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


def backend_enc_configured() -> bool:
    """True iff an active envelope key id + a matching public key are present (boolean presence only)."""
    try:
        kid = _active_key_id()
        return bool(kid) and bool(_load_json_setting("BROKER_CRED_ENC_PUBKEYS").get(kid))
    except EnvelopeError:
        return False
