import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, MultiFernet

logger = logging.getLogger(__name__)

# Phase 3 / ADR-0019 (SEC-CRYPTO-001): customer-credential encryption is decoupled from
# DJANGO_SECRET_KEY. ENCRYPTION uses an explicit, operator-provided key (GUVFX_FERNET_KEY, or a
# GUVFX_FERNET_KEYS rotation list whose FIRST entry is primary). DECRYPTION tries every configured
# key via MultiFernet, so existing ciphertext keeps decrypting during the transition. The legacy key
# derived from DJANGO_SECRET_KEY is retained for READ backward-compat only and is never the primary
# once an explicit key exists. Run `manage.py reencrypt_customer_credentials` to move stored
# ciphertext onto the explicit primary so a later DJANGO_SECRET_KEY rotation can never destroy it.


def _derive_key_from_secret(secret: str) -> bytes:
    # Legacy key derived from DJANGO_SECRET_KEY (SEC-CRYPTO-001). Read-only compat once an explicit
    # GUVFX_FERNET_KEY is configured; never used to encrypt new ciphertext in that case.
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _explicit_keys() -> list:
    """The operator-provided encryption keys, primary first. Empty if none configured.

    SINGLE source of truth for "is an explicit key configured": both `_key_list()` and
    `encryption_key_status()` derive that answer from here, so they can never disagree. A
    separator-/whitespace-only `GUVFX_FERNET_KEYS` (e.g. " , ") yields [] here — it is NOT an
    explicit key — which is exactly what stops it from silently bypassing the decoupling gate.
    """
    multi = (os.getenv("GUVFX_FERNET_KEYS") or "").strip()
    if multi:
        return [k.strip() for k in multi.split(",") if k.strip()]
    single = (os.getenv("GUVFX_FERNET_KEY") or "").strip()
    if single:
        return [single]
    return []


def _derived_key():
    """The legacy DJANGO_SECRET_KEY-derived key (str), or None if no signing secret is set."""
    secret = os.getenv("DJANGO_SECRET_KEY") or os.getenv("SECRET_KEY")
    if secret:
        return _derive_key_from_secret(secret).decode("utf-8")
    return None


def _key_list() -> list:
    """Ordered Fernet keys. encrypt() uses the FIRST; decrypt() tries ALL (MultiFernet).

    Order: explicit key(s) first (newest → oldest for rotation), then — for read backward-compat
    only — the DJANGO_SECRET_KEY-derived legacy key. Fails closed if no key material exists at all.
    """
    explicit = _explicit_keys()
    derived = _derived_key()

    if explicit:
        # Decoupled going forward: encrypt with the explicit primary; keep the derived key ONLY to
        # read ciphertext written before decoupling.
        keys = list(explicit)
        if derived and derived not in keys:
            keys.append(derived)
        return keys

    if derived:
        # No explicit key: preserve current behaviour (derive from DJANGO_SECRET_KEY) but WARN loudly.
        # In this mode a DJANGO_SECRET_KEY rotation silently destroys every stored credential.
        logger.warning(
            "customer-credential encryption is deriving its key from DJANGO_SECRET_KEY "
            "(SEC-CRYPTO-001): set GUVFX_FERNET_KEY and run `manage.py "
            "reencrypt_customer_credentials` so rotating the signing key cannot destroy credentials")
        return [derived]

    raise RuntimeError("Missing GUVFX_FERNET_KEY/GUVFX_FERNET_KEYS and DJANGO_SECRET_KEY/SECRET_KEY")


def _get_fernet() -> MultiFernet:
    return MultiFernet([Fernet(k.encode("utf-8")) for k in _key_list()])


def encrypt_password(plaintext: str) -> str:
    if plaintext is None:
        return ""
    plaintext = plaintext.strip()
    if not plaintext:
        return ""
    f = _get_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_password(ciphertext: str) -> str:
    if ciphertext is None:
        return ""
    ciphertext = ciphertext.strip()
    if not ciphertext:
        return ""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def encryption_key_status() -> dict:
    """Non-secret diagnostics on key posture (never returns key material; never raises; no side
    effects). Reports whether an explicit key is configured and how many *usable* read keys exist.
    ``read_key_count`` counts only keys that actually construct a Fernet (a malformed key is not
    counted — it fails closed at encrypt/decrypt anyway). ``derived_from_django_secret_key`` True
    means encryption is still deriving from DJANGO_SECRET_KEY, i.e. SEC-CRYPTO-001 is still live."""
    explicit = _explicit_keys()
    derived = _derived_key()
    candidate = list(explicit)
    if derived and derived not in candidate:
        candidate.append(derived)
    usable = 0
    for k in candidate:
        try:
            Fernet(k.encode("utf-8"))
            usable += 1
        except Exception:
            pass
    return {
        "explicit_key_configured": bool(explicit),
        "read_key_count": usable,
        "derived_from_django_secret_key": (not explicit) and usable > 0,
    }
