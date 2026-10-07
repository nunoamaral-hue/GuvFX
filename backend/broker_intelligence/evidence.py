"""Broker Intelligence — raw-message evidence store (WP2).

The durable, write-once home for the raw bytes of a broker message. ``BrokerEvent``/``EvidenceBlob`` keep only a
POINTER + integrity hash (never the payload inline), so the bulk/raw data lives here, outside the DB and outside
Git (data rule). The store is **content-addressed** (key derived from the sha256 of the bytes) which makes it
inherently write-once + de-duplicating: a given key maps to exactly one byte-sequence, and re-putting identical
bytes is idempotent. Raw evidence is immutable — never edited in place; a correction is a NEW message (new hash,
new blob). Suspect data is quarantined, never destroyed.

Backend: a local filesystem rooted at a CONFIGURABLE path (``BROKER_INTELLIGENCE_EVIDENCE_ROOT`` setting/env), not
a hard-coded or home path. A later deploy points it at a persistent volume owned by the isolated ingestion service
(WP3); nothing here holds a mailbox/broker credential or any execution authority.

DARK: this module has no caller until WP3 wires the ingestion worker; it is exercised only by unit tests in WP2.
"""
from __future__ import annotations

import hashlib
import os
import tempfile

from django.conf import settings
from django.utils import timezone


def evidence_root() -> str:
    """The configurable filesystem root for stored raw evidence. ``BROKER_INTELLIGENCE_EVIDENCE_ROOT`` (setting
    first, then env); falls back to ``<BASE_DIR>/var/broker_intelligence_evidence`` for dev/test. Never a personal
    or home path; prod sets it explicitly to a persistent, ingestion-service-owned volume."""
    root = getattr(settings, "BROKER_INTELLIGENCE_EVIDENCE_ROOT", None) or os.getenv(
        "BROKER_INTELLIGENCE_EVIDENCE_ROOT")
    if not root:
        base = str(getattr(settings, "BASE_DIR", "")) or os.getcwd()
        root = os.path.join(base, "var", "broker_intelligence_evidence")
    return str(root)


def _sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _key_for(sha256: str) -> str:
    """Sharded, content-addressed storage key (relative to the root): ``ab/cd/<sha256>``. Deterministic in the
    hash only — carries no account id / user / broker / PII."""
    return f"{sha256[:2]}/{sha256[2:4]}/{sha256}"


class EvidenceStoreError(RuntimeError):
    """Raised on an integrity violation (hash mismatch on read, or a key collision with differing bytes)."""


class EvidenceStore:
    """Filesystem-backed, content-addressed, write-once evidence store."""

    def __init__(self, root: str | None = None):
        self._root = root or evidence_root()

    def _abs(self, key: str) -> str:
        return os.path.join(self._root, key)

    def put(self, raw: bytes, *, content_type: str = "message/rfc822",
            source: str = "EMAIL", received_at=None):
        """Persist ``raw`` write-once and return (``EvidenceBlob``, created: bool). Idempotent: identical bytes
        de-dup to the existing blob. The payload is written atomically (tmp + ``os.replace``) and only if absent;
        an existing file whose bytes don't re-hash to the key is a hard integrity error (never overwritten)."""
        from .models import EvidenceBlob

        sha = _sha256_hex(raw)
        key = _key_for(sha)
        existing = EvidenceBlob.objects.filter(sha256=sha).first()
        if existing is not None:
            # De-dup: the blob (and, content-addressed, its bytes) already exist. Verify the file still matches.
            self._verify_file(existing)
            return existing, False

        abs_path = self._abs(key)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        if os.path.exists(abs_path):
            # A file is present without a blob row (e.g. an interrupted prior put). It MUST re-hash to the key,
            # or the store is corrupt — never silently overwrite raw evidence.
            with open(abs_path, "rb") as fh:
                if _sha256_hex(fh.read()) != sha:
                    raise EvidenceStoreError(f"evidence key collision with differing bytes: {key}")
        else:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(abs_path), prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(raw)
                os.replace(tmp, abs_path)   # atomic publish
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

        blob = EvidenceBlob.objects.create(
            sha256=sha, storage_backend="file", storage_key=key, byte_size=len(raw),
            content_type=content_type, source=source, received_at=received_at or timezone.now())
        return blob, True

    def _verify_file(self, blob) -> None:
        abs_path = self._abs(blob.storage_key)
        with open(abs_path, "rb") as fh:
            if _sha256_hex(fh.read()) != blob.sha256:
                raise EvidenceStoreError(f"stored evidence tampered: {blob.storage_key}")

    def read(self, blob) -> bytes:
        """Return the raw bytes for a blob, re-verifying the hash (tamper detection). Raises on mismatch."""
        abs_path = self._abs(blob.storage_key)
        with open(abs_path, "rb") as fh:
            raw = fh.read()
        if _sha256_hex(raw) != blob.sha256:
            raise EvidenceStoreError(f"stored evidence tampered: {blob.storage_key}")
        return raw

    def verify(self, blob) -> bool:
        """True iff the stored bytes still match the recorded hash. Non-raising convenience wrapper."""
        try:
            self._verify_file(blob)
            return True
        except (OSError, EvidenceStoreError):
            return False
