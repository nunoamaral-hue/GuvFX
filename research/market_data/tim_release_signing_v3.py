"""TIM operational release-signature envelope v3 — algorithm-agile, backwards-compatible, fail-closed.

Why v3 exists. The v0.3 ``dataset_release_signature_v2`` envelope is *structurally* algorithm-agile (it
already carries ``algorithm``/``signing_key_id``/``public_key_fingerprint``/``key_validity``) but its
``signature_value`` is schema-locked to a 64-hex sha256 and its validator **hard-pins** the injected
synthetic placeholder (``synthetic: true`` + ``synthetic-placeholder-v0``). A real signature scheme
(Ed25519 = 64 bytes = 128 hex) does not fit that field, so an operational signer needs a governed
envelope change — this module — rather than only key material (see the GO #2 blocker-closure preflight
and ADR-0124).

What stays the same (ADR-0070 / ADR-0073 / ADR-0076 boundary, preserved):
  * The **producer signs; TIM only verifies.** This module implements verification only. There is no
    key generation and no signing primitive here. ``tim_ed25519`` is verify-only for the same reason.
  * The injected-synthetic placeholder remains fully valid **for synthetic releases** so existing
    synthetic fixtures/flows keep working (backwards compatible).
  * Trust flows from the pinned local trust store (``tim_trust_store``), never from the envelope.

What v3 adds:
  * ``signature_value`` is algorithm-agile — 64 hex for the synthetic placeholder, 128 hex for
    ``ed25519-v1`` — with per-algorithm length enforced and the algorithm closed-world (unknown
    algorithm rejected at validation).
  * ``synthetic`` must be true **iff** the algorithm is the synthetic placeholder (confusion/downgrade
    guard); an operational algorithm with ``synthetic: true`` (or vice-versa) is rejected.
  * The signed material binds a distinct v3 domain separator, the **algorithm**, and the
    **created_at_utc** signing time, in addition to the v2-bound release id / manifest digest / contract
    + validator bundle digests / producer identity / signing key id / public-key fingerprint. Binding
    the algorithm blocks algorithm-relabelling; binding the signing time makes the validity check
    tamper-evident; the distinct domain separator blocks cross-version signature reuse.
  * ``verify_release_signature_v3`` fail-closes on: malformed envelope, manifest-digest mismatch
    (tamper), manifest identity mismatch (K-7), unknown/relabelled algorithm, unknown/revoked signing
    key, fingerprint mismatch, key-not-valid-at-signing-time, invalid signature, and — when the caller
    requires an operational release — any synthetic/placeholder signature (``operational_required``).
  * Verification errors are **value-blind**: a fixed reason label only, never key bytes, signature
    bytes, paths or secrets.

Operational signing is still a human-gated, Sponsor-authorised step: the committed default trust store is
EMPTY, so operational verification fails closed (``unknown_signing_key``) until a real public key is
pinned out-of-band. No real/operational key or algorithm selection is performed in the repository.
"""
from __future__ import annotations

import hmac
import re

from .contracts import (
    ContractError,
    ProhibitedKeyError,
    SHA256_RE,
    SLUG_RE,
    sha256_hex,
)
from .tim_contracts import REL_PATH_RE
from .tim_contracts_v0_3 import (
    SYNTHETIC_ALGORITHM_V2,
    _const,
    _exact_keys,
    _instant,
    _no_prohibited,
    _reject_control_strings,
    _str,
    _str_array,
    validate_manifest_signature_binding,
)
from .tim_ed25519 import ed25519_verify

# ---- algorithm registry -------------------------------------------------------------------------
DOMAIN_SEPARATOR_V3 = "GUVFX-TIM-RELEASE-SIGNATURE-v3"
SIGNATURE_V3_SCHEMA_VERSION = "3.0"

SYNTHETIC_ALGORITHM = SYNTHETIC_ALGORITHM_V2  # "synthetic-placeholder-v0"
ED25519_ALGORITHM = "ed25519-v1"
OPERATIONAL_ALGORITHMS = (ED25519_ALGORITHM,)
KNOWN_ALGORITHMS = (SYNTHETIC_ALGORITHM, ED25519_ALGORITHM)

# Required hex length of ``signature_value`` per algorithm (sha256 = 64 hex; Ed25519 = 64 bytes = 128 hex).
_SIG_VALUE_HEXLEN = {SYNTHETIC_ALGORITHM: 64, ED25519_ALGORITHM: 128}
_HEX_RE = re.compile(r"^[0-9a-f]+$")

SIGNATURE_V3_FIELDS = ("schema_version", "domain_separator", "release_id", "signed_manifest_path",
                       "manifest_digest", "contract_bundle_digest", "validator_bundle_digest",
                       "producer_identity", "signing_key_id", "public_key_fingerprint", "algorithm",
                       "signature_scheme", "signature_value", "synthetic", "created_at_utc",
                       "key_validity", "limitations")

# ---- value-blind verification reason codes ------------------------------------------------------
R_OK = "ok"
R_ENVELOPE = "invalid_signature_envelope"
R_MANIFEST_DIGEST = "manifest_digest_mismatch"
R_IDENTITY = "identity_mismatch"
R_OPERATIONAL_REQUIRED = "operational_required"
R_UNKNOWN_ALGORITHM = "unknown_algorithm"
R_TRUST_STORE_UNAVAILABLE = "trust_store_unavailable"
R_UNKNOWN_KEY = "unknown_signing_key"
R_KEY_ALGORITHM = "key_algorithm_mismatch"
R_KEY_REVOKED = "key_revoked"
R_FINGERPRINT = "key_fingerprint_mismatch"
R_KEY_WINDOW = "key_not_valid_at_created_at"
R_SIGNATURE = "signature_invalid"

VALUE_BLIND_REASONS = (
    R_OK, R_ENVELOPE, R_MANIFEST_DIGEST, R_IDENTITY, R_OPERATIONAL_REQUIRED, R_UNKNOWN_ALGORITHM,
    R_TRUST_STORE_UNAVAILABLE, R_UNKNOWN_KEY, R_KEY_ALGORITHM, R_KEY_REVOKED, R_FINGERPRINT,
    R_KEY_WINDOW, R_SIGNATURE,
)


class VerifyResult(object):
    """A value-blind verification outcome: ``ok`` (bool) and a fixed ``reason`` label."""
    __slots__ = ("ok", "reason")

    def __init__(self, ok, reason):
        self.ok = bool(ok)
        self.reason = reason

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return "VerifyResult(ok=%r, reason=%r)" % (self.ok, self.reason)


def signing_material_v3(*, algorithm, release_id, manifest_digest, contract_bundle_digest,
                        validator_bundle_digest, producer_identity, signing_key_id,
                        public_key_fingerprint, created_at_utc) -> bytes:
    """The exact bytes a producer signs (and a consumer verifies). Domain-separated, NUL-joined, and
    binding the protocol version, algorithm, full release identity and the signing time. Identical for
    the synthetic and operational paths so the two cannot be confused. Never contains secret material."""
    return b"\x00".join([
        DOMAIN_SEPARATOR_V3.encode("utf-8"),
        algorithm.encode("utf-8"),
        release_id.encode("utf-8"),
        manifest_digest.encode("utf-8"),
        contract_bundle_digest.encode("utf-8"),
        validator_bundle_digest.encode("utf-8"),
        producer_identity.encode("utf-8"),
        signing_key_id.encode("utf-8"),
        public_key_fingerprint.encode("utf-8"),
        created_at_utc.encode("utf-8"),
    ])


def _material_from_envelope(sig) -> bytes:
    return signing_material_v3(
        algorithm=sig["algorithm"], release_id=sig["release_id"],
        manifest_digest=sig["manifest_digest"], contract_bundle_digest=sig["contract_bundle_digest"],
        validator_bundle_digest=sig["validator_bundle_digest"], producer_identity=sig["producer_identity"],
        signing_key_id=sig["signing_key_id"], public_key_fingerprint=sig["public_key_fingerprint"],
        created_at_utc=sig["created_at_utc"])


def synthetic_sign_v3(*, release_id, manifest_digest, contract_bundle_digest, validator_bundle_digest,
                      producer_identity, signing_key_id, public_key_fingerprint, created_at_utc) -> str:
    """The injected SYNTHETIC signer for v3 (a deliberately non-cryptographic sha256 over the material).
    NOT a real signature; present only so synthetic releases keep a verifiable envelope. The operational
    algorithm/key is NOT chosen here."""
    return sha256_hex(signing_material_v3(
        algorithm=SYNTHETIC_ALGORITHM, release_id=release_id, manifest_digest=manifest_digest,
        contract_bundle_digest=contract_bundle_digest, validator_bundle_digest=validator_bundle_digest,
        producer_identity=producer_identity, signing_key_id=signing_key_id,
        public_key_fingerprint=public_key_fingerprint, created_at_utc=created_at_utc))


def validate_release_signature_v3(obj) -> None:
    """Structurally validate a v3 signature envelope. Raises ContractError/ProhibitedKeyError on any
    defect. Algorithm-agile but closed-world: an unknown algorithm, a wrong-length ``signature_value``
    for the declared algorithm, or a ``synthetic`` flag inconsistent with the algorithm is rejected."""
    _no_prohibited(obj, "signature_v3")
    _reject_control_strings(obj, "signature_v3")
    _exact_keys(obj, SIGNATURE_V3_FIELDS, "signature_v3")
    _const(obj, "schema_version", "signature_v3", SIGNATURE_V3_SCHEMA_VERSION)
    _const(obj, "domain_separator", "signature_v3", DOMAIN_SEPARATOR_V3)
    _str(obj, "release_id", "signature_v3", regex=SLUG_RE, max_len=64)
    _str(obj, "signed_manifest_path", "signature_v3", regex=REL_PATH_RE, max_len=512)
    for f in ("manifest_digest", "contract_bundle_digest", "validator_bundle_digest", "public_key_fingerprint"):
        _str(obj, f, "signature_v3", regex=SHA256_RE, min_len=64, max_len=64)
    _str(obj, "producer_identity", "signature_v3", regex=SLUG_RE, max_len=64)
    _str(obj, "signing_key_id", "signature_v3", regex=SLUG_RE, max_len=64)
    algorithm = _str(obj, "algorithm", "signature_v3", max_len=64)
    if algorithm not in KNOWN_ALGORITHMS:
        raise ContractError("signature_v3: unknown algorithm %r (unknown_algorithm)" % algorithm)
    _const(obj, "signature_scheme", "signature_v3", "detached")
    synthetic = obj["synthetic"]
    if not isinstance(synthetic, bool):
        raise ContractError("signature_v3: synthetic must be a boolean")
    is_synth_alg = algorithm == SYNTHETIC_ALGORITHM
    if synthetic is not is_synth_alg:
        raise ContractError(
            "signature_v3: synthetic must be true iff algorithm is the synthetic placeholder "
            "(algorithm_synthetic_mismatch)")
    sig_value = _str(obj, "signature_value", "signature_v3", max_len=256)
    expect_len = _SIG_VALUE_HEXLEN[algorithm]
    if len(sig_value) != expect_len or not _HEX_RE.match(sig_value):
        raise ContractError(
            "signature_v3: signature_value must be %d lowercase hex for algorithm %r" % (expect_len, algorithm))
    _instant(obj["created_at_utc"], "created_at_utc", "signature_v3")
    kv = obj["key_validity"]
    if kv is not None:
        _exact_keys(kv, ("not_before_utc", "not_after_utc"), "signature_v3.key_validity")
        nb = _instant(kv["not_before_utc"], "not_before_utc", "signature_v3.key_validity")
        na = _instant(kv["not_after_utc"], "not_after_utc", "signature_v3.key_validity")
        if na < nb:
            raise ContractError("signature_v3.key_validity: not_after_utc must be >= not_before_utc")
    elif algorithm in OPERATIONAL_ALGORITHMS:
        raise ContractError("signature_v3: an operational algorithm requires a key_validity window")
    _str_array(obj, "limitations", "signature_v3")


def verify_release_signature_v3_detailed(signature, manifest_bytes, *, trust_store=None, manifest=None,
                                         require_operational=False) -> VerifyResult:
    """Fail-closed verification returning a value-blind ``VerifyResult``.

    ``trust_store`` is a ``tim_trust_store.TrustStore`` and is REQUIRED for operational algorithms (the
    public key is resolved from it, never from the envelope). ``manifest`` (the parsed manifest dict),
    when given, additionally cross-binds the envelope to the manifest's release context (K-7).
    ``require_operational=True`` rejects any synthetic/placeholder signature outright.
    """
    try:
        validate_release_signature_v3(signature)
    except (ContractError, ProhibitedKeyError):
        return VerifyResult(False, R_ENVELOPE)

    # Tamper guard: the envelope's manifest_digest must be sha256(manifest_bytes).
    if sha256_hex(bytes(manifest_bytes)) != signature["manifest_digest"]:
        return VerifyResult(False, R_MANIFEST_DIGEST)

    # K-7 identity binding against the manifest's own release context.
    if manifest is not None:
        try:
            validate_manifest_signature_binding(manifest, signature)
        except ContractError:
            return VerifyResult(False, R_IDENTITY)

    algorithm = signature["algorithm"]
    material = _material_from_envelope(signature)

    if algorithm == SYNTHETIC_ALGORITHM:
        if require_operational:
            return VerifyResult(False, R_OPERATIONAL_REQUIRED)
        expected = sha256_hex(material)
        ok = hmac.compare_digest(expected, signature["signature_value"])
        return VerifyResult(ok, R_OK if ok else R_SIGNATURE)

    if algorithm == ED25519_ALGORITHM:
        if trust_store is None:
            return VerifyResult(False, R_TRUST_STORE_UNAVAILABLE)
        entry = trust_store.resolve(signature["signing_key_id"])
        if entry is None:
            return VerifyResult(False, R_UNKNOWN_KEY)
        if entry.algorithm != algorithm:
            return VerifyResult(False, R_KEY_ALGORITHM)
        if not entry.is_active:
            return VerifyResult(False, R_KEY_REVOKED)
        # The public key used for verification comes from the PINNED store. Its fingerprint must match
        # the envelope's (the fingerprint is also bound into the signed material, so a mismatch cannot
        # produce a valid signature anyway — this is an explicit, early, value-blind rejection).
        if not hmac.compare_digest(entry.public_key_fingerprint, signature["public_key_fingerprint"]):
            return VerifyResult(False, R_FINGERPRINT)
        if not entry.is_valid_at(signature["created_at_utc"]):
            return VerifyResult(False, R_KEY_WINDOW)
        try:
            sig_bytes = bytes.fromhex(signature["signature_value"])
        except ValueError:
            return VerifyResult(False, R_SIGNATURE)
        ok = ed25519_verify(entry.public_key_bytes, material, sig_bytes)
        return VerifyResult(ok, R_OK if ok else R_SIGNATURE)

    # Unreachable: validate_release_signature_v3 already closed the algorithm world. Defense in depth.
    return VerifyResult(False, R_UNKNOWN_ALGORITHM)


def verify_release_signature_v3(signature, manifest_bytes, *, trust_store=None, manifest=None,
                                require_operational=False) -> bool:
    """Boolean fail-closed verify (drop-in parity with ``verify_release_signature_v2``). See
    ``verify_release_signature_v3_detailed`` for the value-blind reason code."""
    return verify_release_signature_v3_detailed(
        signature, manifest_bytes, trust_store=trust_store, manifest=manifest,
        require_operational=require_operational).ok


def verify_operational_release_signature_v3(signature, manifest_bytes, *, trust_store,
                                            manifest=None) -> VerifyResult:
    """The verify an OPERATIONAL consumer should call: it hard-codes ``require_operational=True`` so any
    synthetic/placeholder signature is rejected (``operational_required``) rather than silently accepted,
    and ``trust_store`` is mandatory (a positional-only ``None`` is not accepted). Returns a value-blind
    ``VerifyResult``. Use this on the first-release admission path instead of the general verifier so the
    synthetic non-cryptographic path can never be mistaken for an operational signature."""
    if trust_store is None:
        return VerifyResult(False, R_TRUST_STORE_UNAVAILABLE)
    return verify_release_signature_v3_detailed(
        signature, manifest_bytes, trust_store=trust_store, manifest=manifest, require_operational=True)
