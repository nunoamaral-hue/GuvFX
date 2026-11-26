"""GFX-TIM-GO2A-PUBLIC-KEY-PIN-040 — admission + validity tests for the pinned operational Ed25519 identity.
synthetic_test_only. Pure standard library (no network / MT5 / NAS / broker / credential / production /
third-party crypto dependency).

Proves, against the MERGED verifier contract (ADR-0124), that the PUBLIC identity provisioned and
custody-verified under packet GFX-TIM-GO2A-ED25519-KEY-PROVISION-039 and now pinned in the committed
operational trust store is an admissible operational trust anchor, that its Sponsor-approved validity
window is enforced at BOTH boundaries by the verifier's actual comparison rule, and that mismatched
metadata, duplicate ids and out-of-window releases all fail closed. Signature accept/reject uses SYNTHETIC
ephemeral-key fixtures only; empty-store rejection is retained in an isolated fixture (TS.empty()). The
real producer private key lives off-repository in the producer host's custody and is NEVER opened, read or
reconstructed here — this file holds only the 64-hex PUBLIC key, its fingerprint and the derived key id.
"""
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from research.market_data import tim_ed25519 as E  # noqa: E402
from research.market_data import tim_release_signing_v3 as S  # noqa: E402
from research.market_data import tim_trust_store as TS  # noqa: E402
from research.market_data.contracts import SLUG_RE, sha256_hex  # noqa: E402

# The PUBLIC identity provisioned + custody-verified under packet 039 (public material only).
REAL_PUBLIC_KEY = "231e0103985ad21656421b54c85a8caf8de66d6f12ec69d59418c862fa3c8b1e"
REAL_FINGERPRINT = "ff37421d2467768a7cc8050932ef683351bb52d900d91b74dc993d0e5fb909d8"
REAL_KEY_ID = "ed25519-ff37421d2467768a7cc8050932ef6833"

# The Sponsor-approved validity window pinned with the entry (GFX-TIM-GO2A-PUBLIC-KEY-PIN-040).
APPROVED_NOT_BEFORE = "2026-11-26T01:06:48Z"
APPROVED_NOT_AFTER = "2027-11-26T01:06:48Z"


def _reviewed_entry(**overrides):
    entry = {
        "signing_key_id": REAL_KEY_ID,
        "algorithm": "ed25519-v1",
        "public_key": REAL_PUBLIC_KEY,
        "public_key_fingerprint": REAL_FINGERPRINT,
        "not_before_utc": APPROVED_NOT_BEFORE,
        "not_after_utc": APPROVED_NOT_AFTER,
        "status": "active",
    }
    entry.update(overrides)
    return entry


def _store_with(*entries):
    return TS.from_dict({"schema_version": "1.0", "keys": [dict(e) for e in entries]})


class TestReviewedIdentityIsDerivedFromPublicBytes(unittest.TestCase):
    """The fingerprint and key id are recomputed from the PUBLIC bytes — no trust in the custodian's labels."""

    def test_fingerprint_recomputes_from_public_key(self):
        self.assertEqual(sha256_hex(bytes.fromhex(REAL_PUBLIC_KEY)), REAL_FINGERPRINT)

    def test_key_id_is_the_deterministic_derivation(self):
        self.assertEqual("ed25519-" + REAL_FINGERPRINT[:32], REAL_KEY_ID)
        self.assertTrue(SLUG_RE.match(REAL_KEY_ID))
        self.assertLessEqual(len(REAL_KEY_ID), 64)

    def test_public_key_is_32_bytes(self):
        self.assertEqual(len(bytes.fromhex(REAL_PUBLIC_KEY)), 32)

    def test_public_key_is_an_acceptable_ed25519_verification_key(self):
        # Canonical, full-order, non-small-order — vettable without the private key.
        self.assertTrue(E.is_acceptable_public_key(bytes.fromhex(REAL_PUBLIC_KEY)))
        # Contrast: the small-order identity point is NOT acceptable.
        self.assertFalse(E.is_acceptable_public_key(b"\x01" + b"\x00" * 31))


class TestReviewedIdentityAdmission(unittest.TestCase):
    """Admission of the reviewed entry (with the approved window) through the merged trust-store loader."""

    def test_reviewed_entry_is_admitted_and_resolvable(self):
        store = _store_with(_reviewed_entry())
        self.assertEqual(store.key_ids(), (REAL_KEY_ID,))
        e = store.resolve(REAL_KEY_ID)
        self.assertIsNotNone(e)
        self.assertEqual(e.algorithm, "ed25519-v1")
        self.assertEqual(e.public_key_bytes, bytes.fromhex(REAL_PUBLIC_KEY))
        self.assertEqual(e.public_key_fingerprint, REAL_FINGERPRINT)
        self.assertTrue(e.is_active)


class TestReviewedIdentityRejections(unittest.TestCase):
    """Mismatched metadata, duplicate ids, and bad windows around the reviewed identity fail closed."""

    def test_fingerprint_that_does_not_match_public_key_is_rejected(self):
        with self.assertRaises(TS.TrustStoreIntegrityError):
            _store_with(_reviewed_entry(public_key_fingerprint="0" * 64))

    def test_public_key_altered_without_matching_fingerprint_is_rejected(self):
        with self.assertRaises(TS.TrustStoreIntegrityError):
            _store_with(_reviewed_entry(public_key="22" * 32))  # fingerprint no longer matches

    def test_unknown_algorithm_is_rejected(self):
        with self.assertRaises(TS.TrustStoreIntegrityError):
            _store_with(_reviewed_entry(algorithm="rsa-2048"))

    def test_duplicate_signing_key_id_is_rejected(self):
        with self.assertRaises(TS.TrustStoreIntegrityError):
            _store_with(_reviewed_entry(), _reviewed_entry())

    def test_window_order_is_enforced(self):
        with self.assertRaises(TS.TrustStoreIntegrityError):
            _store_with(_reviewed_entry(not_before_utc=APPROVED_NOT_AFTER, not_after_utc=APPROVED_NOT_BEFORE))

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(TS.TrustStoreIntegrityError):
            _store_with(_reviewed_entry(status="trusted"))

    def test_extra_or_missing_fields_are_rejected(self):
        extra = _reviewed_entry(); extra["surprise"] = 1
        with self.assertRaises(TS.TrustStoreIntegrityError):
            _store_with(extra)
        missing = _reviewed_entry(); del missing["status"]
        with self.assertRaises(TS.TrustStoreIntegrityError):
            _store_with(missing)


# --- Signature accept/reject exercises the merged verifier with SYNTHETIC ephemeral-key fixtures only. ---
FIXDIR = os.path.join(REPO_ROOT, "tests", "fixtures", "tim_release_signing_v3")
# The synthetic operational fixture envelope's signing time (bound into the signed material).
FIXTURE_CREATED_AT = "2026-01-15T00:00:00Z"


def _fix(name):
    with open(os.path.join(FIXDIR, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _fix_raw(name):
    with open(os.path.join(FIXDIR, name), "rb") as fh:
        return fh.read()


_MANIFEST_BYTES = _fix_raw("signed_manifest.json")
_OP = _fix("signature_v3__ed25519_operational.json")
_STORE_DICT = _fix("operational_trust_store.json")
_STORE = TS.from_dict(_STORE_DICT)


def _op():
    return json.loads(json.dumps(_OP))


def _synthetic_store_entry():
    return json.loads(json.dumps(_STORE_DICT["keys"][0]))


def _fixture_store_with_window(not_before_utc, not_after_utc):
    raw = _synthetic_store_entry()
    raw["not_before_utc"] = not_before_utc
    raw["not_after_utc"] = not_after_utc
    return TS.from_dict({"schema_version": "1.0", "keys": [raw]})


class TestSyntheticSignatureVerify(unittest.TestCase):
    """Signature tests use a synthetic ephemeral key (no real private key is ever present)."""

    def test_genuine_synthetic_key_operational_signature_is_accepted(self):
        r = S.verify_operational_release_signature_v3(_op(), _MANIFEST_BYTES, trust_store=_STORE)
        self.assertTrue(r.ok)
        self.assertEqual(r.reason, S.R_OK)

    def test_tampered_signature_is_rejected(self):
        e = _op()
        c = "0" if e["signature_value"][0] != "0" else "1"
        e["signature_value"] = c + e["signature_value"][1:]
        r = S.verify_operational_release_signature_v3(e, _MANIFEST_BYTES, trust_store=_STORE)
        self.assertEqual(r.reason, S.R_SIGNATURE)

    def test_tampered_manifest_is_rejected(self):
        tampered = bytearray(_MANIFEST_BYTES); tampered[5] ^= 0x01
        r = S.verify_operational_release_signature_v3(_op(), bytes(tampered), trust_store=_STORE)
        self.assertEqual(r.reason, S.R_MANIFEST_DIGEST)

    def test_revoked_key_is_rejected(self):
        raw = _synthetic_store_entry(); raw["status"] = "revoked"
        store = TS.from_dict({"schema_version": "1.0", "keys": [raw]})
        r = S.verify_operational_release_signature_v3(_op(), _MANIFEST_BYTES, trust_store=store)
        self.assertEqual(r.reason, S.R_KEY_REVOKED)

    def test_unknown_key_against_an_isolated_empty_store(self):
        # Empty-store rejection coverage retained in an isolated fixture (never the committed store).
        r = S.verify_operational_release_signature_v3(_op(), _MANIFEST_BYTES, trust_store=TS.empty())
        self.assertEqual(r.reason, S.R_UNKNOWN_KEY)

    # --- Both validity boundaries, exercised through the verifier's ACTUAL comparison (nb <= t <= na). ---
    def test_release_at_the_lower_window_bound_is_accepted(self):
        store = _fixture_store_with_window(FIXTURE_CREATED_AT, "2027-01-01T00:00:00Z")
        r = S.verify_operational_release_signature_v3(_op(), _MANIFEST_BYTES, trust_store=store)
        self.assertEqual(r.reason, S.R_OK)  # not_before is inclusive

    def test_release_one_second_before_the_lower_bound_is_rejected(self):
        store = _fixture_store_with_window("2026-01-15T00:00:01Z", "2027-01-01T00:00:00Z")
        r = S.verify_operational_release_signature_v3(_op(), _MANIFEST_BYTES, trust_store=store)
        self.assertEqual(r.reason, S.R_KEY_WINDOW)

    def test_release_at_the_upper_window_bound_is_accepted(self):
        store = _fixture_store_with_window("2025-01-01T00:00:00Z", FIXTURE_CREATED_AT)
        r = S.verify_operational_release_signature_v3(_op(), _MANIFEST_BYTES, trust_store=store)
        self.assertEqual(r.reason, S.R_OK)  # not_after is inclusive

    def test_release_one_second_after_the_upper_bound_is_rejected(self):
        store = _fixture_store_with_window("2025-01-01T00:00:00Z", "2026-01-14T23:59:59Z")
        r = S.verify_operational_release_signature_v3(_op(), _MANIFEST_BYTES, trust_store=store)
        self.assertEqual(r.reason, S.R_KEY_WINDOW)


class TestCommittedOperationalStorePin(unittest.TestCase):
    """The committed operational store pins exactly the reviewed identity with the approved window."""

    def test_committed_store_pins_exactly_the_reviewed_identity(self):
        pinned = TS.load_pinned_operational_trust_store()
        self.assertEqual(len(pinned), 1)
        self.assertEqual(pinned.key_ids(), (REAL_KEY_ID,))
        e = pinned.resolve(REAL_KEY_ID)
        self.assertEqual(e.algorithm, "ed25519-v1")
        self.assertEqual(e.public_key_bytes, bytes.fromhex(REAL_PUBLIC_KEY))
        self.assertEqual(e.public_key_fingerprint, REAL_FINGERPRINT)
        self.assertTrue(e.is_active)
        self.assertTrue(E.is_acceptable_public_key(e.public_key_bytes))
        self.assertEqual(e.not_before_utc, APPROVED_NOT_BEFORE)
        self.assertEqual(e.not_after_utc, APPROVED_NOT_AFTER)

    def test_pinned_window_boundaries_follow_the_verifier_comparison(self):
        e = TS.load_pinned_operational_trust_store().resolve(REAL_KEY_ID)
        self.assertIsNotNone(e)
        self.assertTrue(e.is_valid_at(APPROVED_NOT_BEFORE))         # lower bound inclusive
        self.assertFalse(e.is_valid_at("2026-11-26T01:06:47Z"))    # one second before -> fails closed
        self.assertTrue(e.is_valid_at(APPROVED_NOT_AFTER))         # upper bound inclusive
        self.assertFalse(e.is_valid_at("2027-11-26T01:06:49Z"))    # one second after -> fails closed
        self.assertFalse(e.is_valid_at("not-a-timestamp"))         # malformed instant fails closed

    def test_committed_store_entries_hold_only_public_fields(self):
        with open(TS.DEFAULT_TRUST_STORE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for entry in data.get("keys", []):
            self.assertEqual(set(entry.keys()), set(TS.ENTRY_FIELDS))
            self.assertNotIn("private_key", entry)
            self.assertNotIn("seed", entry)

    def test_no_signing_or_keygen_primitive_is_reachable(self):
        # TIM verifies only (ADR-0070): no operational signer / key generator exists here.
        for banned in ("sign", "generate", "keygen", "private", "seed"):
            self.assertFalse(hasattr(E, banned))
        self.assertFalse(hasattr(S, "ed25519_sign"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
