"""WP3b — encrypted credential store for the isolated ingestion service.

Holds OAuth tokens for connected mailboxes ENCRYPTED AT REST, keyed by an opaque ``credential_ref``. The DB
(``ConnectedMailbox``) stores ONLY the ref — never the token (no secret in the DB, per the §4 architecture). The
token's plaintext never touches the database, the repo, or a log line.

Design:
* **Fernet (AES-128-CBC + HMAC) symmetric encryption.** The key comes from ``BROKER_INTELLIGENCE_CREDENTIAL_KEY``
  (settings-override-then-env; a url-safe base64 32-byte Fernet key). **Fail-closed**: if the key is missing or
  malformed, every store/load RAISES ``CredentialStoreError`` — a token is NEVER written in plaintext and a read
  never silently returns an unencrypted value.
* **Filesystem store** under ``BROKER_INTELLIGENCE_CREDENTIAL_ROOT`` (configurable, outside the DB/Git — mirrors the
  evidence store). One ``<credential_ref>.enc`` file per credential; written 0600; the ref is opaque
  (``secrets.token_hex``), never the email and never guessable.
* Stores/returns a plain JSON token dict (access_token / refresh_token / expiry / scope / token_type). The caller
  (``gmail_oauth``) owns the token shape; this module only encrypts/decrypts + persists.

Secret-free surface: ``__repr__``/logs never include the token; errors carry only the ref (opaque) + a reason.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Optional

from django.conf import settings


class CredentialStoreError(Exception):
    """Raised fail-closed on any key/encryption/IO problem — never degrades to a plaintext read/write."""


def _conf(name: str, default: str = "") -> str:
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val or "")


def _fernet():
    """Build the Fernet cipher from the configured key. Fail-closed: a missing/invalid key raises (never plaintext)."""
    key = _conf("BROKER_INTELLIGENCE_CREDENTIAL_KEY").strip()
    if not key:
        raise CredentialStoreError("BROKER_INTELLIGENCE_CREDENTIAL_KEY is not set; refusing to store/read a credential "
                                   "in plaintext (fail-closed)")
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — malformed key -> fail closed, never plaintext
        raise CredentialStoreError(f"credential key is not a valid Fernet key: {type(exc).__name__}") from exc


def _root() -> Path:
    root = _conf("BROKER_INTELLIGENCE_CREDENTIAL_ROOT").strip()
    if not root:
        raise CredentialStoreError("BROKER_INTELLIGENCE_CREDENTIAL_ROOT is not set")
    p = Path(root)
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)   # least-privilege: only the owner may list/traverse the credential root
    except OSError:
        pass
    return p


def _path_for(credential_ref: str) -> Path:
    # Defensive: the ref is minted here as hex, but never let a caller-supplied ref escape the store root.
    ref = str(credential_ref or "")
    if not ref or "/" in ref or "\\" in ref or ".." in ref:
        raise CredentialStoreError("invalid credential_ref")
    return _root() / f"{ref}.enc"


def store_token(token: dict, *, credential_ref: Optional[str] = None) -> str:
    """Encrypt + persist an OAuth token dict. Returns the opaque ``credential_ref`` (minted if not supplied, e.g. on a
    token refresh that overwrites in place). Fail-closed on any key/IO error — never writes plaintext."""
    if not isinstance(token, dict):
        raise CredentialStoreError("token must be a dict")
    ref = credential_ref or ("cr" + secrets.token_hex(16))
    f = _fernet()
    try:
        blob = f.encrypt(json.dumps(token).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CredentialStoreError(f"encryption failed: {type(exc).__name__}") from exc
    path = _path_for(ref)
    # Create the temp 0600 ATOMICALLY (mkstemp opens O_CREAT|O_EXCL at 0600) so the ciphertext is never briefly
    # world-readable under a permissive umask; os.replace then preserves that mode on the final file.
    import tempfile
    fd, tmp_name = tempfile.mkstemp(prefix=ref, suffix=".enc.tmp", dir=str(_root()))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    os.replace(tmp_name, path)   # atomic; keeps the 0600 mode
    return ref


def load_token(credential_ref: str) -> Optional[dict]:
    """Decrypt + return the token dict for ``credential_ref``, or None if absent. Fail-closed: a key/decrypt error
    RAISES (never returns ciphertext or a partial value)."""
    path = _path_for(credential_ref)
    if not path.exists():
        return None
    f = _fernet()
    try:
        data = f.decrypt(path.read_bytes())
        return json.loads(data.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — tampered/corrupt/wrong-key -> fail closed
        raise CredentialStoreError(f"decryption failed for credential: {type(exc).__name__}") from exc


def delete_token(credential_ref: str) -> bool:
    """Remove a stored credential (e.g. on mailbox disconnect). Returns True if a file was removed."""
    path = _path_for(credential_ref)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
