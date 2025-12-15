import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _derive_key_from_secret(secret: str) -> bytes:
    # Deterministic fallback key (dev only). Prefer GUVFX_FERNET_KEY in env.
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _get_fernet() -> Fernet:
    key = os.getenv("GUVFX_FERNET_KEY")
    if key:
        return Fernet(key.encode("utf-8"))

    # Fallback (dev): derive from Django secret if Fernet key isn't provided
    secret = os.getenv("DJANGO_SECRET_KEY") or os.getenv("SECRET_KEY")
    if not secret:
        raise RuntimeError("Missing GUVFX_FERNET_KEY and DJANGO_SECRET_KEY/SECRET_KEY")

    return Fernet(_derive_key_from_secret(secret))


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