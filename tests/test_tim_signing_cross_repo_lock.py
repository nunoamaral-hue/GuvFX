"""Cross-repo signing-contract lock tests (GFX-TIM-GO2A-CUSTODY-BINDING-016, ADR-0124 addendum).

Stdlib-only. Pins the byte-exact TIM v3 signing-material contract so the GuvFX verifier and the producer
repo (nunoamaral-hue/guvfx-windows-history-agent) signer cannot drift: the GuvFX verifier's
``signing_material_v3`` over the shared test vector must hash to the same value the producer repo
independently pins, and the merged verifier constants must match the lock file. Also asserts the committed
operational trust store agrees with the lock's recorded custody-binding state (pinned-key count; by ADR-0124
operational verification is dark iff the store is empty).
"""
import hashlib
import json
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from research.market_data import tim_release_signing_v3 as S  # noqa: E402
from research.market_data import tim_trust_store as TS  # noqa: E402

LOCK_PATH = os.path.join(REPO_ROOT, "research", "market_data", "trust_store", "tim_signing_cross_repo_lock.json")


def _load_lock():
    with open(LOCK_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


class TestCrossRepoLock(unittest.TestCase):
    def setUp(self):
        self.lock = _load_lock()

    def test_shared_material_digest_matches_verifier(self):
        v = self.lock["shared_test_vector"]
        material = S.signing_material_v3(
            algorithm=v["algorithm"], release_id=v["release_id"], manifest_digest=v["manifest_digest"],
            contract_bundle_digest=v["contract_bundle_digest"],
            validator_bundle_digest=v["validator_bundle_digest"], producer_identity=v["producer_identity"],
            signing_key_id=v["signing_key_id"], public_key_fingerprint=v["public_key_fingerprint"],
            created_at_utc=v["created_at_utc"])
        self.assertEqual(hashlib.sha256(material).hexdigest(), self.lock["shared_material_sha256"])

    def test_verifier_constants_match_lock(self):
        self.assertEqual(self.lock["domain_separator"], S.DOMAIN_SEPARATOR_V3)
        self.assertEqual(self.lock["algorithm"], S.ED25519_ALGORITHM)
        self.assertEqual(tuple(self.lock["envelope_fields"]), S.SIGNATURE_V3_FIELDS)
        self.assertEqual(self.lock["signature_hex_length"], S._SIG_VALUE_HEXLEN[S.ED25519_ALGORITHM])

    def test_material_field_order_and_join(self):
        v = self.lock["shared_test_vector"]
        material = S.signing_material_v3(
            algorithm=v["algorithm"], release_id=v["release_id"], manifest_digest=v["manifest_digest"],
            contract_bundle_digest=v["contract_bundle_digest"],
            validator_bundle_digest=v["validator_bundle_digest"], producer_identity=v["producer_identity"],
            signing_key_id=v["signing_key_id"], public_key_fingerprint=v["public_key_fingerprint"],
            created_at_utc=v["created_at_utc"])
        parts = material.split(b"\x00")
        self.assertEqual(self.lock["material_join"], "nul_byte")
        self.assertEqual(len(parts), len(self.lock["material_field_order"]))
        self.assertEqual(parts[0], self.lock["domain_separator"].encode())
        self.assertEqual(parts[1], v["algorithm"].encode())
        self.assertEqual(parts[9], v["created_at_utc"].encode())

    def test_key_id_derivation_matches_vector(self):
        v = self.lock["shared_test_vector"]
        derived = "ed25519-" + v["public_key_fingerprint"][:32]
        self.assertEqual(derived, v["signing_key_id"])
        self.assertRegex(derived, r"^[a-z0-9][a-z0-9_-]*$")
        self.assertLessEqual(len(derived), 64)

    def test_operational_trust_store_matches_cross_repo_lock(self):
        store = TS.load_pinned_operational_trust_store()
        cb = self.lock["custody_binding"]
        # The lock's recorded pinned-key count tracks the live committed store.
        self.assertEqual(len(store), cb["operational_trust_store_pinned_keys"])
        # ADR-0124: the operational verification path is dark iff the trust store is empty. Pinning the
        # reviewed public key arms operational VERIFICATION; it starts no signing, release or consumption.
        self.assertIs(cb["operational_path_dark"], len(store) == 0)
        # No private key is ever in this repository (verify-only; the producer holds the private key off-repo).
        self.assertIs(cb["private_key_in_this_repository"], False)

    def test_public_metadata_fields_cover_envelope_identity(self):
        pm = set(self.lock["public_metadata_fields"])
        self.assertEqual(pm, {"algorithm", "public_key", "public_key_fingerprint", "signing_key_id"})

    def test_lock_names_both_repositories(self):
        self.assertEqual(self.lock["verifier"]["repository"], "nunoamaral-hue/GuvFX")
        self.assertEqual(self.lock["producer_signer"]["repository"], "nunoamaral-hue/guvfx-windows-history-agent")

    def test_lock_contains_no_private_or_secret_material(self):
        text = json.dumps(self.lock).lower()
        for forbidden in ("private key", "begin ", "-----", "password", "secret_key"):
            self.assertNotIn(forbidden, text)
        # no 128-hex signature-like or long non-vector blobs beyond the known 64-hex pins
        self.assertNotRegex(json.dumps(self.lock), r"[0-9a-f]{65,}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
