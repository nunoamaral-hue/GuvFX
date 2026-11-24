"""TIM operational signing trust store — the pinned, local, immutable set of PUBLIC verification keys.

Trust in an operational release flows from THIS store, never from the signature envelope. A verifier
resolves the envelope's ``signing_key_id`` to a pinned entry here, confirms the pinned public key's
fingerprint matches the envelope, checks the validity window and revocation status, and only then
verifies the detached signature against the **pinned** public key. An attacker who rewrites the
envelope's embedded key/fingerprint cannot verify, because the bytes used for verification come from
this store.

Contents are PUBLIC key material only (Ed25519 public keys are not secrets). No private key, seed,
password or token is ever stored, loaded or accepted here. Under ADR-0070 TIM holds no signing
authority; this store lets TIM *verify* producer signatures without holding any secret.

Deterministic key-id selection and fail-closed integrity:
  * exactly one entry per ``signing_key_id``; a duplicate id is a permanent integrity failure
    (``TrustStoreIntegrityError``) — the store never silently picks one;
  * every entry's ``public_key_fingerprint`` must equal sha256(public_key), or load fails closed;
  * rotation = add the new ``signing_key_id`` and keep the previous one active during the overlap
    window; revocation = flip an entry's ``status`` to ``"revoked"`` (kept, not deleted, so a revoked
    key is positively recognised and rejected rather than looking merely unknown).

The committed default operational store is EMPTY: no real key is pinned in the repository. Operational
verification therefore fails closed (``unknown_signing_key``) until a real public key is pinned
out-of-band under a separate Sponsor GO. Tests supply their own test store with an ephemeral test key.
"""
from __future__ import annotations

import json
import os

from .contracts import (
    SHA256_RE,
    SLUG_RE,
    parse_canonical_utc_instant,
    sha256_hex,
)
from .tim_ed25519 import is_acceptable_public_key

TRUST_STORE_SCHEMA_VERSION = "1.0"
KNOWN_ALGORITHMS = ("ed25519-v1",)
STATUSES = ("active", "revoked")
ENTRY_FIELDS = ("signing_key_id", "algorithm", "public_key", "public_key_fingerprint",
                "not_before_utc", "not_after_utc", "status")

# Committed default operational trust store (EMPTY — no real key pinned in the repository).
DEFAULT_TRUST_STORE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "trust_store", "operational_public_keys.json")


class TrustStoreIntegrityError(Exception):
    """Permanent, fail-closed trust-store integrity failure (malformed entry, duplicate key id,
    fingerprint mismatch). Value-blind: the message names the defect class and key id only, never
    the key bytes."""


class TrustStoreEntry(object):
    __slots__ = ("signing_key_id", "algorithm", "public_key_bytes", "public_key_fingerprint",
                 "not_before_utc", "not_after_utc", "status")

    def __init__(self, signing_key_id, algorithm, public_key_bytes, public_key_fingerprint,
                 not_before_utc, not_after_utc, status):
        self.signing_key_id = signing_key_id
        self.algorithm = algorithm
        self.public_key_bytes = public_key_bytes
        self.public_key_fingerprint = public_key_fingerprint
        self.not_before_utc = not_before_utc
        self.not_after_utc = not_after_utc
        self.status = status

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def is_valid_at(self, instant_str: str) -> bool:
        """True iff ``instant_str`` (a canonical UTC instant) lies within [not_before, not_after]."""
        try:
            t = parse_canonical_utc_instant(instant_str)
            nb = parse_canonical_utc_instant(self.not_before_utc)
            na = parse_canonical_utc_instant(self.not_after_utc)
        except Exception:
            return False
        return nb <= t <= na


def _validate_entry(raw, index):
    where = "trust_store.keys[%d]" % index
    if not isinstance(raw, dict):
        raise TrustStoreIntegrityError(where + ": entry must be an object")
    if set(raw.keys()) != set(ENTRY_FIELDS):
        raise TrustStoreIntegrityError(where + ": entry must have exactly the required keys")
    key_id = raw["signing_key_id"]
    if not (isinstance(key_id, str) and SLUG_RE.match(key_id) and len(key_id) <= 64):
        raise TrustStoreIntegrityError(where + ": signing_key_id malformed")
    algorithm = raw["algorithm"]
    if algorithm not in KNOWN_ALGORITHMS:
        raise TrustStoreIntegrityError(where + ": unknown algorithm for key id %r" % key_id)
    pub_hex = raw["public_key"]
    if not (isinstance(pub_hex, str) and SHA256_RE.match(pub_hex)):
        raise TrustStoreIntegrityError(where + ": public_key must be 32-byte lowercase hex")
    fp = raw["public_key_fingerprint"]
    if not (isinstance(fp, str) and SHA256_RE.match(fp)):
        raise TrustStoreIntegrityError(where + ": public_key_fingerprint must be 64 lowercase hex")
    pub_bytes = bytes.fromhex(pub_hex)
    if sha256_hex(pub_bytes) != fp:
        raise TrustStoreIntegrityError(
            where + ": public_key_fingerprint does not match sha256(public_key) for key id %r" % key_id)
    if algorithm == "ed25519-v1" and not is_acceptable_public_key(pub_bytes):
        # Refuse to pin a degenerate / low-order / non-canonical Ed25519 key (belt-and-braces with the
        # verifier's own small-order rejection): such a key must never become a trust anchor.
        raise TrustStoreIntegrityError(
            where + ": public_key is not an acceptable Ed25519 verification key for key id %r" % key_id)
    status = raw["status"]
    if status not in STATUSES:
        raise TrustStoreIntegrityError(where + ": unknown status for key id %r" % key_id)
    nb_raw, na_raw = raw["not_before_utc"], raw["not_after_utc"]
    try:
        nb = parse_canonical_utc_instant(nb_raw)
        na = parse_canonical_utc_instant(na_raw)
    except Exception:
        raise TrustStoreIntegrityError(where + ": not_before_utc/not_after_utc must be canonical UTC instants")
    if na < nb:
        raise TrustStoreIntegrityError(where + ": not_after_utc must be >= not_before_utc")
    return TrustStoreEntry(key_id, algorithm, pub_bytes, fp, nb_raw, na_raw, status)


class TrustStore(object):
    """An immutable, deterministic lookup of pinned public verification keys."""

    def __init__(self, entries):
        by_id = {}
        for i, entry in enumerate(entries):
            if entry.signing_key_id in by_id:
                raise TrustStoreIntegrityError(
                    "trust_store: duplicate signing_key_id %r (integrity failure)" % entry.signing_key_id)
            by_id[entry.signing_key_id] = entry
        self._by_id = by_id

    def resolve(self, signing_key_id):
        """Deterministic exact lookup. Returns the single pinned entry or None (never a guess)."""
        return self._by_id.get(signing_key_id)

    def key_ids(self):
        return tuple(sorted(self._by_id))

    def __len__(self):
        return len(self._by_id)


def from_dict(data) -> TrustStore:
    if not isinstance(data, dict):
        raise TrustStoreIntegrityError("trust_store: top level must be an object")
    if data.get("schema_version") != TRUST_STORE_SCHEMA_VERSION:
        raise TrustStoreIntegrityError("trust_store: schema_version must be %r" % TRUST_STORE_SCHEMA_VERSION)
    keys = data.get("keys")
    if not isinstance(keys, list):
        raise TrustStoreIntegrityError("trust_store: keys must be a list")
    entries = [_validate_entry(raw, i) for i, raw in enumerate(keys)]
    return TrustStore(entries)


def load(path) -> TrustStore:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return from_dict(data)


def load_pinned_operational_trust_store() -> TrustStore:
    """Load the committed default operational trust store (EMPTY in the repository)."""
    return load(DEFAULT_TRUST_STORE_PATH)


def empty() -> TrustStore:
    return TrustStore([])
