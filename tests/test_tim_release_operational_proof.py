"""GO #2d operational-release LOCAL PROOF — test. synthetic_test_only.

Proves the complete OPERATIONAL v3 release flow with a DISPOSABLE ephemeral Ed25519 key, signed by a
TEST-ONLY pure-python RFC-8032 signer (reusing tim_ed25519's verified point arithmetic) and VERIFIED by the
REAL verifier (verify_operational_release_signature_v3) against an ISOLATED test trust store: manifest, all
nine evidence references, the v3 operational signature, every file SHA-256, READY written LAST, and consumer
acceptance; plus the required failure checks — a tampered file, a tampered manifest, an invalid signature,
and a partial release without READY.

No real key, NAS, network, MT5, broker, credential or production action. The test-only signer is NOT a
production signing primitive and lives only here (the research modules remain verify-only, ADR-0070); the
ephemeral seed is created in memory and never written. The real operational signature is produced on the
producer host by `scripts/tim_signing_provider.py sign-release` over the identical signing_material_v3.
"""
import hashlib
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from research.market_data import tim_ed25519 as E  # noqa: E402
from research.market_data.contracts import canonical_json_bytes  # noqa: E402
import tim_release_v3_operational_proof as P  # noqa: E402

_KV = {"not_before_utc": "2026-01-01T00:00:00Z", "not_after_utc": "2027-01-01T00:00:00Z"}
_CREATED = "2026-06-01T00:00:00Z"


class _DisposableEd25519Provider:
    """TEST ONLY: an ephemeral Ed25519 key with a pure-python RFC-8032 sign (reusing tim_ed25519's verified
    arithmetic). NOT a production signer — it exists only to exercise the real verifier with a disposable
    key. The seed is created in memory and never written, printed or persisted."""

    def __init__(self, seed=None):
        self._seed = seed if seed is not None else os.urandom(32)
        h = hashlib.sha512(self._seed).digest()
        a = bytearray(h[:32])
        a[0] &= 248
        a[31] &= 127
        a[31] |= 64
        self._a = int.from_bytes(a, "little")
        self._prefix = h[32:]
        self._A = E._encodepoint(E._scalarmult(E._B, self._a))

    def public_key_raw(self):
        return self._A

    def sign(self, message):
        m = bytes(message)
        r = int.from_bytes(hashlib.sha512(self._prefix + m).digest(), "little") % E._L
        rpt = E._encodepoint(E._scalarmult(E._B, r))
        k = int.from_bytes(hashlib.sha512(rpt + self._A + m).digest(), "little") % E._L
        s = (r + k * self._a) % E._L
        return rpt + s.to_bytes(32, "little")


def _chmod_tree_writable(root):
    for dp, _dn, fns in os.walk(root):
        try:
            os.chmod(dp, 0o755)
        except OSError:
            pass
        for fn in fns:
            try:
                os.chmod(os.path.join(dp, fn), 0o644)
            except OSError:
                pass


class TestDisposableSigner(unittest.TestCase):
    def test_disposable_key_signs_and_the_real_verifier_accepts(self):
        prov = _DisposableEd25519Provider()
        pub = prov.public_key_raw()
        self.assertEqual(len(pub), 32)
        self.assertTrue(E.is_acceptable_public_key(pub))  # canonical, full-order
        msg = b"GUVFX-TIM disposable proof message"
        sig = prov.sign(msg)
        self.assertEqual(len(sig), 64)
        self.assertTrue(E.ed25519_verify(pub, msg, sig))
        self.assertFalse(E.ed25519_verify(pub, msg + b"x", sig))  # tamper rejected


class TestOperationalProof(unittest.TestCase):
    def test_full_operational_flow_accepts(self):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                s = P._run(tmp, _DisposableEd25519Provider(), created_at_utc=_CREATED, key_validity=_KV)
            finally:
                _chmod_tree_writable(tmp)
        self.assertTrue(s["ok"])
        self.assertTrue(s["synthetic_data"])
        self.assertTrue(s["operational_signature"])
        self.assertEqual(s["signature_algorithm"], "ed25519-v1")
        self.assertEqual(s["signature_schema"], "3.0")
        self.assertFalse(s["signature_synthetic"])  # operational, NOT the synthetic placeholder
        self.assertEqual(s["evidence_roles_bound"], 9)  # all nine evidence references
        self.assertEqual(s["sign_release_fields_keys"], [
            "contract_bundle_digest", "created_at_utc", "key_validity", "limitations",
            "manifest_digest", "producer_identity", "release_id", "validator_bundle_digest"])
        self.assertTrue(s["pre_publish_verify_ok"])
        self.assertEqual(s["pre_publish_verify_reason"], "ok")
        self.assertTrue(s["ready_written_last"])
        self.assertTrue(s["consumer_accepted"])
        self.assertIsNone(s["consumer_reason"])
        self.assertTrue(s["all_file_hashes_match"])
        self.assertTrue(s["evidence_identity_verified"])
        self.assertTrue(s["tampered_signature_rejected"])
        self.assertTrue(s["tampered_manifest_rejected"])
        self.assertTrue(s["file_tamper_detected"])
        self.assertEqual(s["network"], "blocked")
        self.assertEqual(s["nas"], "none")
        self.assertEqual(s["real_signing_key"], "none")


class TestFailureChecks(unittest.TestCase):
    """Build + sign + seal into a WRITABLE dir, then prove each rejection through consumer_accept."""

    def _build_sealed(self, tmp, *, corrupt_signature=False, write_ready=True):
        prov = _DisposableEd25519Provider()
        staging, releases = os.path.join(tmp, "staging"), os.path.join(tmp, "releases")
        os.makedirs(staging)
        os.makedirs(releases)
        _fp, kid = P.derive_key_id(prov.public_key_raw())
        built = P.build_release(staging, signing_key_id=kid, producer_identity=P.PRODUCER_IDENTITY)
        fields = P.sign_release_fields(built, producer_identity=P.PRODUCER_IDENTITY, created_at_utc=_CREATED,
                                       key_validity=_KV, limitations=P.LIMITATIONS)
        env, pub_hex = P.build_v3_envelope(prov, fields)
        store = P.isolated_trust_store(pub_hex, _KV)
        if corrupt_signature:
            sv = env["signature_value"]
            env["signature_value"] = ("0" if sv[0] != "0" else "1") + sv[1:]
        rel_dir = built["rel_dir"]
        if write_ready:
            P.seal(rel_dir, built, env)
        else:  # partial release: manifest + signature present, READY absent
            P.SMOKE._write(rel_dir, "manifest.json", built["manifest"])
            with open(os.path.join(rel_dir, "manifest.sig.json"), "wb") as fh:
                fh.write(canonical_json_bytes(env))
        os.rename(rel_dir, os.path.join(releases, P.RELEASE_ID))
        return releases, store

    def test_happy_path_accepts(self):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                releases, store = self._build_sealed(tmp)
                r = P.consumer_accept(releases, P.RELEASE_ID, store)
            finally:
                _chmod_tree_writable(tmp)
        self.assertTrue(r["accepted"])
        self.assertIsNone(r["reason"])

    def test_tampered_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                releases, store = self._build_sealed(tmp)
                pq = os.path.join(releases, P.RELEASE_ID, "eurusd_m1.parquet")
                with open(pq, "r+b") as fh:
                    data = bytearray(fh.read())
                    data[len(data) // 2] ^= 0x01
                    fh.seek(0)
                    fh.write(data)
                r = P.consumer_accept(releases, P.RELEASE_ID, store)
            finally:
                _chmod_tree_writable(tmp)
        self.assertFalse(r["accepted"])
        self.assertEqual(r["reason"], "file_hash_mismatch")

    def test_tampered_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                releases, store = self._build_sealed(tmp)
                with open(os.path.join(releases, P.RELEASE_ID, "manifest.json"), "ab") as fh:
                    fh.write(b" ")
                r = P.consumer_accept(releases, P.RELEASE_ID, store)
            finally:
                _chmod_tree_writable(tmp)
        self.assertFalse(r["accepted"])
        self.assertIn(r["reason"], ("ready_binding_mismatch", "manifest_digest_mismatch"))

    def test_invalid_signature_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                releases, store = self._build_sealed(tmp, corrupt_signature=True)
                r = P.consumer_accept(releases, P.RELEASE_ID, store)
            finally:
                _chmod_tree_writable(tmp)
        self.assertFalse(r["accepted"])
        self.assertEqual(r["reason"], "signature_invalid")

    def test_partial_release_without_ready_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                releases, store = self._build_sealed(tmp, write_ready=False)
                r = P.consumer_accept(releases, P.RELEASE_ID, store)
            finally:
                _chmod_tree_writable(tmp)
        self.assertFalse(r["accepted"])
        self.assertEqual(r["reason"], "missing_ready")


if __name__ == "__main__":
    unittest.main(verbosity=2)
