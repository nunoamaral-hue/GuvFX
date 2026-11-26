"""GUVFX-TIM operational release-signing v3 — repository contract tests. synthetic_test_only.

Proves, offline and stdlib-only (no network / MT5 / NAS / broker / credential / production / third-party
crypto dependency), the operational signing-envelope foundation added under GFX-TIM-GO2-RELEASE-
FOUNDATION-014 / ADR-0124:

  * the pure-stdlib Ed25519 verifier is correct (RFC 8032 vector + genuine signature) and fails closed
    on tamper / wrong key / wrong length / non-canonical scalar;
  * the v3 envelope is algorithm-agile and closed-world: unknown algorithm, wrong signature-value length
    for the declared algorithm, and a synthetic flag inconsistent with the algorithm are all rejected;
  * a genuine operational Ed25519 signature verifies against a PINNED trust-store key, and every
    adversarial variant (tamper, unknown/revoked/fingerprint-mismatched/out-of-window key, manifest
    identity mismatch, cross-version/relabel downgrade, operational-required-vs-synthetic) fails closed
    with a value-blind reason;
  * the synthetic placeholder path stays valid (backwards compatible) and never consults the trust store;
  * the trust store is deterministic and fail-closed (duplicate id, fingerprint mismatch, bad fields),
    and operational verification fails closed against an isolated empty store (the committed store now
    pins exactly the reviewed operational identity, GFX-TIM-GO2A-PUBLIC-KEY-PIN-040);
  * TIM holds no signing authority: the modules import no network/MT5/execution symbol and expose no
    Ed25519 signing / key-generation primitive (producer-signs / TIM-verifies-only, ADR-0070).

The fixtures were produced offline by an EPHEMERAL Ed25519 test key whose private seed was never written;
only the PUBLIC key, its fingerprint and the resulting (public) signatures are committed.
"""
import json
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from research.market_data import tim_ed25519 as E  # noqa: E402
from research.market_data import tim_release_signing_v3 as S  # noqa: E402
from research.market_data import tim_trust_store as TS  # noqa: E402
from research.market_data.contracts import (  # noqa: E402
    ContractError,
    ProhibitedKeyError,
    sha256_hex,
)

FIXDIR = os.path.join(REPO_ROOT, "tests", "fixtures", "tim_release_signing_v3")


def _load_json(name):
    with open(os.path.join(FIXDIR, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_raw(name):
    with open(os.path.join(FIXDIR, name), "rb") as fh:
        return fh.read()


MANIFEST_BYTES = _load_raw("signed_manifest.json")
MANIFEST = json.loads(MANIFEST_BYTES.decode("utf-8"))
OP = _load_json("signature_v3__ed25519_operational.json")
SYNTH = _load_json("signature_v3__synthetic.json")
STORE_DICT = _load_json("operational_trust_store.json")
STORE = TS.from_dict(STORE_DICT)


def _op():
    return json.loads(json.dumps(OP))  # deep copy


def _synth():
    return json.loads(json.dumps(SYNTH))


def _store_entry_raw():
    return json.loads(json.dumps(STORE_DICT["keys"][0]))


# RFC 8032 Ed25519 test vector 1 (empty message) — a fixed external positive control.
_RFC_PUB = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
_RFC_SIG = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")


class TestEd25519Verifier(unittest.TestCase):
    def test_rfc8032_vector1_positive_control(self):
        self.assertTrue(E.ed25519_verify(_RFC_PUB, b"", _RFC_SIG))

    def test_rfc8032_vector1_tamper_negative_control(self):
        self.assertFalse(E.ed25519_verify(_RFC_PUB, b"x", _RFC_SIG))
        bad = bytearray(_RFC_SIG); bad[0] ^= 0x01
        self.assertFalse(E.ed25519_verify(_RFC_PUB, b"", bytes(bad)))

    def test_rejects_wrong_lengths(self):
        self.assertFalse(E.ed25519_verify(_RFC_PUB, b"", _RFC_SIG[:-1]))
        self.assertFalse(E.ed25519_verify(_RFC_PUB[:-1], b"", _RFC_SIG))
        self.assertFalse(E.ed25519_verify(b"", b"", b""))

    def test_rejects_non_canonical_s(self):
        # S == L is non-canonical (must be < L); the malleability guard must reject it.
        sig = _RFC_SIG[:32] + (E._L).to_bytes(32, "little")
        self.assertFalse(E.ed25519_verify(_RFC_PUB, b"", sig))

    def test_malleability_guard_rejects_genuine_twin(self):
        # A GENUINE signature (R, S) has a malleable twin (R, S+L) that the core equation would accept
        # ([S+L]B == [S]B because L.B == identity); only the S < L guard rejects it. This exercises the
        # guard for real — deleting it makes the twin verify and fails this test.
        entry = STORE.resolve(OP["signing_key_id"])
        material = S.signing_material_v3(
            algorithm=OP["algorithm"], release_id=OP["release_id"], manifest_digest=OP["manifest_digest"],
            contract_bundle_digest=OP["contract_bundle_digest"],
            validator_bundle_digest=OP["validator_bundle_digest"], producer_identity=OP["producer_identity"],
            signing_key_id=OP["signing_key_id"], public_key_fingerprint=OP["public_key_fingerprint"],
            created_at_utc=OP["created_at_utc"])
        sig = bytes.fromhex(OP["signature_value"])
        self.assertTrue(E.ed25519_verify(entry.public_key_bytes, material, sig))
        twin = sig[:32] + (int.from_bytes(sig[32:], "little") + E._L).to_bytes(32, "little")
        self.assertFalse(E.ed25519_verify(entry.public_key_bytes, material, twin))

    def test_rejects_small_order_public_key(self):
        # The identity point (order 1) is small order; it must never be accepted as a verification key.
        identity = b"\x01" + b"\x00" * 31
        self.assertFalse(E.is_acceptable_public_key(identity))
        self.assertFalse(E.ed25519_verify(identity, b"anything", b"\x00" * 64))
        # A genuine full-order producer key IS acceptable.
        self.assertTrue(E.is_acceptable_public_key(STORE.resolve(OP["signing_key_id"]).public_key_bytes))

    def test_verifier_has_no_sign_or_keygen(self):
        for banned in ("sign", "generate", "keygen", "private", "seed"):
            self.assertFalse(hasattr(E, banned), "verify-only module must not expose %r" % banned)


class TestEnvelopeValidation(unittest.TestCase):
    def test_operational_envelope_is_well_formed(self):
        self.assertIsNone(S.validate_release_signature_v3(_op()))

    def test_synthetic_envelope_is_well_formed(self):
        self.assertIsNone(S.validate_release_signature_v3(_synth()))

    def test_unknown_algorithm_rejected(self):
        e = _op(); e["algorithm"] = "rsa-2048"
        with self.assertRaises(ContractError):
            S.validate_release_signature_v3(e)

    def test_operational_sig_value_must_be_128_hex(self):
        e = _op(); e["signature_value"] = "a" * 64  # a synthetic-length value under an operational alg
        with self.assertRaises(ContractError):
            S.validate_release_signature_v3(e)

    def test_synthetic_sig_value_must_be_64_hex(self):
        e = _synth(); e["signature_value"] = "a" * 128
        with self.assertRaises(ContractError):
            S.validate_release_signature_v3(e)

    def test_non_hex_signature_value_rejected(self):
        e = _op(); e["signature_value"] = "Z" * 128
        with self.assertRaises(ContractError):
            S.validate_release_signature_v3(e)

    def test_synthetic_flag_must_match_algorithm(self):
        e = _op(); e["synthetic"] = True  # operational alg but synthetic:true
        with self.assertRaises(ContractError):
            S.validate_release_signature_v3(e)
        e2 = _synth(); e2["synthetic"] = False
        with self.assertRaises(ContractError):
            S.validate_release_signature_v3(e2)

    def test_operational_requires_key_validity_window(self):
        e = _op(); e["key_validity"] = None
        with self.assertRaises(ContractError):
            S.validate_release_signature_v3(e)

    def test_bad_schema_version_and_domain_separator(self):
        e = _op(); e["schema_version"] = "2.0"
        with self.assertRaises(ContractError):
            S.validate_release_signature_v3(e)
        e2 = _op(); e2["domain_separator"] = "GUVFX-TIM-RELEASE-SIGNATURE-v2"
        with self.assertRaises(ContractError):
            S.validate_release_signature_v3(e2)

    def test_missing_and_extra_fields_rejected(self):
        e = _op(); del e["algorithm"]
        with self.assertRaises(ContractError):
            S.validate_release_signature_v3(e)
        e2 = _op(); e2["surprise"] = 1
        with self.assertRaises(ContractError):
            S.validate_release_signature_v3(e2)

    def test_prohibited_key_rejected(self):
        e = _op(); e["__proto__"] = "x"
        with self.assertRaises((ProhibitedKeyError, ContractError)):
            S.validate_release_signature_v3(e)


class TestOperationalVerify(unittest.TestCase):
    def test_valid_operational_verifies(self):
        r = S.verify_release_signature_v3_detailed(_op(), MANIFEST_BYTES, trust_store=STORE)
        self.assertTrue(r.ok)
        self.assertEqual(r.reason, S.R_OK)
        self.assertTrue(S.verify_release_signature_v3(_op(), MANIFEST_BYTES, trust_store=STORE))

    def test_valid_operational_verifies_with_manifest_binding(self):
        r = S.verify_release_signature_v3_detailed(_op(), MANIFEST_BYTES, trust_store=STORE, manifest=MANIFEST)
        self.assertTrue(r.ok)

    def test_lowlevel_material_verifies_against_pinned_key(self):
        entry = STORE.resolve(OP["signing_key_id"])
        material = S.signing_material_v3(
            algorithm=OP["algorithm"], release_id=OP["release_id"], manifest_digest=OP["manifest_digest"],
            contract_bundle_digest=OP["contract_bundle_digest"],
            validator_bundle_digest=OP["validator_bundle_digest"], producer_identity=OP["producer_identity"],
            signing_key_id=OP["signing_key_id"], public_key_fingerprint=OP["public_key_fingerprint"],
            created_at_utc=OP["created_at_utc"])
        self.assertTrue(E.ed25519_verify(entry.public_key_bytes, material, bytes.fromhex(OP["signature_value"])))

    def test_tampered_manifest_bytes_rejected(self):
        tampered = bytearray(MANIFEST_BYTES); tampered[5] ^= 0x01
        r = S.verify_release_signature_v3_detailed(_op(), bytes(tampered), trust_store=STORE)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, S.R_MANIFEST_DIGEST)

    def test_tampered_signature_rejected(self):
        e = _op()
        c = "0" if e["signature_value"][0] != "0" else "1"
        e["signature_value"] = c + e["signature_value"][1:]
        r = S.verify_release_signature_v3_detailed(e, MANIFEST_BYTES, trust_store=STORE)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, S.R_SIGNATURE)

    def test_unknown_key_when_store_lacks_it(self):
        r = S.verify_release_signature_v3_detailed(_op(), MANIFEST_BYTES, trust_store=TS.empty())
        self.assertEqual(r.reason, S.R_UNKNOWN_KEY)

    def test_committed_store_pins_reviewed_identity_and_is_unknown_for_other_keys(self):
        # The committed operational store now pins exactly the reviewed operational identity
        # (GFX-TIM-GO2A-PUBLIC-KEY-PIN-040). The fixture's SYNTHETIC key id is NOT that identity, so an
        # operational verify against the committed store still fails closed. Empty-store rejection is
        # retained in an isolated fixture by test_unknown_key_when_store_lacks_it (TS.empty()).
        pinned = TS.load_pinned_operational_trust_store()
        self.assertEqual(len(pinned), 1)
        r = S.verify_release_signature_v3_detailed(_op(), MANIFEST_BYTES, trust_store=pinned)
        self.assertEqual(r.reason, S.R_UNKNOWN_KEY)

    def test_trust_store_unavailable_for_operational(self):
        r = S.verify_release_signature_v3_detailed(_op(), MANIFEST_BYTES, trust_store=None)
        self.assertEqual(r.reason, S.R_TRUST_STORE_UNAVAILABLE)

    def test_fingerprint_mismatch_rejected(self):
        # A pinned entry with the SAME key id but a different (structurally valid) public key.
        raw = _store_entry_raw()
        other_pub = "22" * 32
        raw["public_key"] = other_pub
        raw["public_key_fingerprint"] = sha256_hex(bytes.fromhex(other_pub))
        store = TS.from_dict({"schema_version": "1.0", "keys": [raw]})
        r = S.verify_release_signature_v3_detailed(_op(), MANIFEST_BYTES, trust_store=store)
        self.assertEqual(r.reason, S.R_FINGERPRINT)

    def test_revoked_key_rejected(self):
        raw = _store_entry_raw(); raw["status"] = "revoked"
        store = TS.from_dict({"schema_version": "1.0", "keys": [raw]})
        r = S.verify_release_signature_v3_detailed(_op(), MANIFEST_BYTES, trust_store=store)
        self.assertEqual(r.reason, S.R_KEY_REVOKED)

    def test_key_not_valid_at_created_at(self):
        raw = _store_entry_raw()
        raw["not_before_utc"] = "2026-06-01T00:00:00Z"  # after the envelope's created_at (2026-01-15)
        raw["not_after_utc"] = "2026-12-31T00:00:00Z"
        store = TS.from_dict({"schema_version": "1.0", "keys": [raw]})
        r = S.verify_release_signature_v3_detailed(_op(), MANIFEST_BYTES, trust_store=store)
        self.assertEqual(r.reason, S.R_KEY_WINDOW)

    def test_key_algorithm_mismatch(self):
        entry = STORE.resolve(OP["signing_key_id"])
        future = TS.TrustStoreEntry(entry.signing_key_id, "future-alg-v9", entry.public_key_bytes,
                                    entry.public_key_fingerprint, entry.not_before_utc,
                                    entry.not_after_utc, "active")
        store = TS.TrustStore([future])
        r = S.verify_release_signature_v3_detailed(_op(), MANIFEST_BYTES, trust_store=store)
        self.assertEqual(r.reason, S.R_KEY_ALGORITHM)

    def test_identity_mismatch_k7(self):
        bad_manifest = json.loads(json.dumps(MANIFEST)); bad_manifest["release_id"] = "rel_other_release"
        r = S.verify_release_signature_v3_detailed(_op(), MANIFEST_BYTES, trust_store=STORE, manifest=bad_manifest)
        self.assertEqual(r.reason, S.R_IDENTITY)

    def test_require_operational_rejects_synthetic(self):
        r = S.verify_release_signature_v3_detailed(_synth(), MANIFEST_BYTES, require_operational=True)
        self.assertEqual(r.reason, S.R_OPERATIONAL_REQUIRED)

    def test_operational_wrapper_accepts_operational(self):
        r = S.verify_operational_release_signature_v3(_op(), MANIFEST_BYTES, trust_store=STORE)
        self.assertTrue(r.ok)
        self.assertEqual(r.reason, S.R_OK)

    def test_operational_wrapper_rejects_synthetic(self):
        r = S.verify_operational_release_signature_v3(_synth(), MANIFEST_BYTES, trust_store=STORE)
        self.assertEqual(r.reason, S.R_OPERATIONAL_REQUIRED)

    def test_operational_wrapper_requires_trust_store(self):
        r = S.verify_operational_release_signature_v3(_op(), MANIFEST_BYTES, trust_store=None)
        self.assertEqual(r.reason, S.R_TRUST_STORE_UNAVAILABLE)

    def test_algorithm_relabel_downgrade_is_rejected_at_validation(self):
        # Relabel an operational envelope as synthetic without changing the 128-hex signature: the
        # per-algorithm length check rejects it as a malformed envelope (no path to acceptance).
        e = _op(); e["algorithm"] = S.SYNTHETIC_ALGORITHM; e["synthetic"] = True
        r = S.verify_release_signature_v3_detailed(e, MANIFEST_BYTES, trust_store=STORE)
        self.assertEqual(r.reason, S.R_ENVELOPE)

    def test_cross_version_separator_rejected(self):
        e = _op(); e["domain_separator"] = "GUVFX-TIM-RELEASE-SIGNATURE-v2"
        r = S.verify_release_signature_v3_detailed(e, MANIFEST_BYTES, trust_store=STORE)
        self.assertEqual(r.reason, S.R_ENVELOPE)


class TestSyntheticVerify(unittest.TestCase):
    def test_valid_synthetic_verifies_without_store(self):
        r = S.verify_release_signature_v3_detailed(_synth(), MANIFEST_BYTES)
        self.assertTrue(r.ok)
        self.assertEqual(r.reason, S.R_OK)

    def test_synthetic_ignores_trust_store(self):
        self.assertTrue(S.verify_release_signature_v3(_synth(), MANIFEST_BYTES, trust_store=None))

    def test_synthetic_tamper_rejected(self):
        e = _synth()
        c = "0" if e["signature_value"][0] != "0" else "1"
        e["signature_value"] = c + e["signature_value"][1:]
        r = S.verify_release_signature_v3_detailed(e, MANIFEST_BYTES)
        self.assertEqual(r.reason, S.R_SIGNATURE)

    def test_synthetic_tampered_manifest_rejected(self):
        tampered = bytearray(MANIFEST_BYTES); tampered[0] ^= 0x01
        r = S.verify_release_signature_v3_detailed(_synth(), bytes(tampered))
        self.assertEqual(r.reason, S.R_MANIFEST_DIGEST)


class TestTrustStoreIntegrity(unittest.TestCase):
    def test_resolve_is_deterministic(self):
        self.assertIsNotNone(STORE.resolve(OP["signing_key_id"]))
        self.assertIsNone(STORE.resolve("no-such-key"))

    def test_duplicate_key_id_fails_closed(self):
        raw = _store_entry_raw()
        with self.assertRaises(TS.TrustStoreIntegrityError):
            TS.from_dict({"schema_version": "1.0", "keys": [raw, json.loads(json.dumps(raw))]})

    def test_fingerprint_must_match_public_key(self):
        raw = _store_entry_raw(); raw["public_key_fingerprint"] = "0" * 64
        with self.assertRaises(TS.TrustStoreIntegrityError):
            TS.from_dict({"schema_version": "1.0", "keys": [raw]})

    def test_unknown_algorithm_entry_fails(self):
        raw = _store_entry_raw(); raw["algorithm"] = "rsa"
        with self.assertRaises(TS.TrustStoreIntegrityError):
            TS.from_dict({"schema_version": "1.0", "keys": [raw]})

    def test_bad_status_fails(self):
        raw = _store_entry_raw(); raw["status"] = "trusted"
        with self.assertRaises(TS.TrustStoreIntegrityError):
            TS.from_dict({"schema_version": "1.0", "keys": [raw]})

    def test_malformed_public_key_fails(self):
        raw = _store_entry_raw(); raw["public_key"] = "xyz"
        with self.assertRaises(TS.TrustStoreIntegrityError):
            TS.from_dict({"schema_version": "1.0", "keys": [raw]})

    def test_bad_schema_version_fails(self):
        with self.assertRaises(TS.TrustStoreIntegrityError):
            TS.from_dict({"schema_version": "9.9", "keys": []})

    def test_window_order_enforced(self):
        raw = _store_entry_raw()
        raw["not_before_utc"] = "2027-01-01T00:00:00Z"; raw["not_after_utc"] = "2026-01-01T00:00:00Z"
        with self.assertRaises(TS.TrustStoreIntegrityError):
            TS.from_dict({"schema_version": "1.0", "keys": [raw]})

    def test_low_order_public_key_rejected_at_pin_time(self):
        # The identity point is a structurally valid 32-byte key with a consistent fingerprint, but it is
        # small order and must be refused at pin time (belt-and-braces with the verifier's rejection).
        identity_hex = "01" + "00" * 31
        raw = _store_entry_raw()
        raw["public_key"] = identity_hex
        raw["public_key_fingerprint"] = sha256_hex(bytes.fromhex(identity_hex))
        with self.assertRaises(TS.TrustStoreIntegrityError):
            TS.from_dict({"schema_version": "1.0", "keys": [raw]})


class TestValueBlindAndSecretFree(unittest.TestCase):
    def test_reason_codes_are_fixed_value_blind_labels(self):
        self.assertIn(S.R_OK, S.VALUE_BLIND_REASONS)
        for r in S.VALUE_BLIND_REASONS:
            self.assertRegex(r, r"^[a-z_]+$")

    def test_fixtures_carry_no_private_key_material(self):
        banned = ("private", "secret", "password", "token", "seed", "begin ", "-----")
        for name in sorted(os.listdir(FIXDIR)):
            text = _load_raw(name).decode("utf-8").lower()
            for b in banned:
                self.assertNotIn(b, text, "fixture %s contains forbidden token %r" % (name, b))

    def test_trust_store_holds_only_public_fields(self):
        for entry in STORE_DICT["keys"]:
            self.assertEqual(set(entry.keys()), set(TS.ENTRY_FIELDS))
            self.assertNotIn("private_key", entry)
            self.assertNotIn("seed", entry)


class TestModuleBoundary(unittest.TestCase):
    MODULES = (
        "research/market_data/tim_ed25519.py",
        "research/market_data/tim_trust_store.py",
        "research/market_data/tim_release_signing_v3.py",
    )
    # Import-level tokens that would indicate network / MT5 / execution capability leaking in.
    FORBIDDEN_IMPORTS = ("import socket", "import urllib", "import requests", "import subprocess",
                         "import boto3", "MetaTrader5", "import http")
    FORBIDDEN_SYMBOLS = ("place_order", "open_trade", "execution_job", "order_transport", "allow_live")

    def test_modules_import_no_network_or_execution(self):
        for rel in self.MODULES:
            with open(os.path.join(REPO_ROOT, rel), "r", encoding="utf-8") as fh:
                src = fh.read()
            for tok in self.FORBIDDEN_IMPORTS:
                self.assertNotIn(tok, src, "%s must not %s" % (rel, tok))
            for tok in self.FORBIDDEN_SYMBOLS:
                self.assertNotIn(tok, src, "%s must not reference %r" % (rel, tok))

    def test_no_operational_signing_primitive_exposed(self):
        # TIM verifies only (ADR-0070). No operational Ed25519 signer / key generator exists.
        self.assertFalse(hasattr(S, "ed25519_sign"))
        self.assertTrue(hasattr(S, "verify_release_signature_v3"))
        # synthetic_sign_v3 exists and is explicitly the non-cryptographic placeholder only.
        self.assertTrue(hasattr(S, "synthetic_sign_v3"))

    def test_cryptography_library_not_imported(self):
        for rel in self.MODULES:
            with open(os.path.join(REPO_ROOT, rel), "r", encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("import cryptography", src)
            self.assertNotIn("from cryptography", src)
            self.assertNotIn("import nacl", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
