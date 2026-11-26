"""GO #2d operational-release command-line workflow — test (GFX-TIM GO #2d, ADR-0127). synthetic_test_only.

Exercises the ACTUAL command-line entry points of ``tools/tim_release_cli.py`` end-to-end — build ->
request.fields.json -> (external signer) -> release.signature.json -> assemble -> publish -> validate — with
a file handoff to the PINNED producer signer contract and back. The signer is a DISPOSABLE ephemeral Ed25519
key signed by a TEST-ONLY pure-python RFC-8032 signer (reusing tim_ed25519's verified arithmetic; identical
to the merged proof test), verified by the REAL verifier against an ISOLATED test trust store, and published
to a LOCAL mount substitute.

Covers the happy path plus the required failure checks: interrupted publication, missing READY, tampering, a
wrong key, and a write-free consumer run (also: an expired key window, altered candidate files, overwrite /
existing release id, and the explicit mount identity check). No real key, NAS, network, MT5, broker,
credential or production action occurs; the CI evidence here is DISTINCT from live NAS acceptance. The
test-only signer is NOT a production primitive and lives only in this test (the research/tools modules stay
verify-and-build only, ADR-0070).
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO_ROOT, os.path.join(REPO_ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from research.market_data import tim_ed25519 as E  # noqa: E402
from research.market_data.contracts import sha256_hex  # noqa: E402
import tim_release_v3_operational_proof as P  # noqa: E402
import tim_release_cli as CLI  # noqa: E402

_KV = {"not_before_utc": "2026-01-01T00:00:00Z", "not_after_utc": "2027-01-01T00:00:00Z"}
_CREATED = "2026-06-01T00:00:00Z"  # CLI.DEFAULT_CREATED_AT, inside _KV


class _DisposableEd25519Provider:
    """TEST ONLY: an ephemeral Ed25519 key with a pure-python RFC-8032 sign (reusing tim_ed25519's verified
    arithmetic). NOT a production signer. The seed is created in memory (a fixed seed here only to keep the
    derived key id stable across the one built template and the signer)."""

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


def _key_id(provider):
    return "ed25519-" + sha256_hex(bytes(provider.public_key_raw()))[:32]


def _write_store(path, provider, kv):
    """Serialize an ISOLATED single-key trust store pinning the disposable public key."""
    pub = bytes(provider.public_key_raw())
    fp = sha256_hex(pub)
    store = {"schema_version": "1.0", "keys": [{
        "signing_key_id": "ed25519-" + fp[:32], "algorithm": "ed25519-v1",
        "public_key": pub.hex(), "public_key_fingerprint": fp,
        "not_before_utc": kv["not_before_utc"], "not_after_utc": kv["not_after_utc"], "status": "active"}]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(store, fh)


def _producer_sign(fields_path, out_path, provider):
    """The PINNED signer contract (file handoff): read request.fields.json, sign via the disposable key over
    the REAL signing_material_v3 (P.build_v3_envelope == the producer signer replica), and write
    release.signature.json in the producer ``--out`` format (json.dumps sort_keys), exclusive-create."""
    with open(fields_path, "r", encoding="utf-8") as fh:
        fields = json.load(fh)
    env, _pub = P.build_v3_envelope(provider, fields)
    data = (json.dumps(env, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return env


def _run(argv):
    """Invoke the real CLI main(), capturing stdout, returning (rc, last_receipt_dict)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = CLI.main(argv)
    line = buf.getvalue().strip().splitlines()[-1]
    return rc, json.loads(line)


def _chmod_tree(root, *, dir_mode, file_mode):
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            try:
                os.chmod(os.path.join(dp, fn), file_mode)
            except OSError:
                pass
        try:
            os.chmod(dp, dir_mode)
        except OSError:
            pass


def _snapshot(root):
    snap = {}
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            st = os.stat(p)
            snap[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return snap


class _ReleaseCliBase(unittest.TestCase):
    """Builds ONE synthetic release template per class (duckdb, ~8s) and copies it per test."""

    @classmethod
    def setUpClass(cls):
        cls._root = tempfile.mkdtemp(prefix="tim-rel-cli-template-")
        cls.provider = _DisposableEd25519Provider(seed=b"\x11" * 32)  # fixed -> stable key id
        cls.kid = _key_id(cls.provider)
        cls.store_path = os.path.join(cls._root, "isolated_store.json")
        _write_store(cls.store_path, cls.provider, _KV)
        rc, receipt = _run(["build", "--root", cls._root, "--signing-key-id", cls.kid])
        assert rc == 0 and receipt["state"] == "UNSEALED", receipt
        cls.template_candidate = os.path.join(cls._root, "staging", P.RELEASE_ID)

    @classmethod
    def tearDownClass(cls):
        _chmod_tree(cls._root, dir_mode=0o755, file_mode=0o644)
        shutil.rmtree(cls._root, ignore_errors=True)

    def _fresh_candidate(self):
        """A pristine, UNSEALED copy of the template candidate under a new temp root. Returns (tmp, dir)."""
        tmp = tempfile.mkdtemp(prefix="tim-rel-cli-")
        self.addCleanup(lambda: (_chmod_tree(tmp, dir_mode=0o755, file_mode=0o644),
                                 shutil.rmtree(tmp, ignore_errors=True)))
        candidate = os.path.join(tmp, "staging", P.RELEASE_ID)
        os.makedirs(os.path.dirname(candidate))
        shutil.copytree(self.template_candidate, candidate)
        return tmp, candidate

    def _sign(self, tmp, candidate, provider=None):
        out = os.path.join(tmp, "release.signature.json")
        _producer_sign(os.path.join(candidate, "request.fields.json"), out, provider or self.provider)
        return out

    def _mount(self, tmp, *, with_marker=True, scope="exchange/releases"):
        mount = os.path.join(tmp, "mount")
        os.makedirs(mount)
        if with_marker:
            with open(os.path.join(mount, CLI.SCOPE_MARKER), "w", encoding="utf-8") as fh:
                json.dump({"producer_identity": "guvfx-tim-producer", "scope": scope}, fh)
        return mount

    def _seal(self, tmp, candidate, store_path=None):
        out = self._sign(tmp, candidate)
        rc, receipt = _run(["assemble", "--candidate", candidate, "--signature", out,
                            "--trust-store", store_path or self.store_path])
        self.assertEqual(rc, 0, receipt)
        self.assertEqual(receipt["state"], "SEALED")
        return receipt


class TestFullHandoff(_ReleaseCliBase):
    def test_build_request_contract(self):
        _tmp, candidate = self._fresh_candidate()
        with open(os.path.join(candidate, "request.fields.json"), "r", encoding="utf-8") as fh:
            fields = json.load(fh)
        self.assertEqual(sorted(fields), list(CLI.REQUEST_FIELDS))
        CLI.validate_request_fields(fields)  # pinned producer contract accepts it
        bad = dict(fields)
        bad.pop("manifest_digest")
        with self.assertRaises(CLI.CliError):
            CLI.validate_request_fields(bad)
        # Candidate is UNSEALED: no signature / READY yet.
        self.assertFalse(os.path.exists(os.path.join(candidate, "manifest.sig.json")))
        self.assertFalse(os.path.exists(os.path.join(candidate, "READY")))

    def test_full_flow_accepts(self):
        tmp, candidate = self._fresh_candidate()
        asm = self._seal(tmp, candidate)
        self.assertEqual(asm["algorithm"], "ed25519-v1")
        self.assertFalse(asm["signature_synthetic"])        # operational, not the synthetic placeholder
        self.assertTrue(asm["ready_written_last"])
        self.assertFalse(asm["altered_files"])
        self.assertEqual(asm["evidence_references"], 9)
        self.assertEqual(asm["real_signing_key"], "none")

        mount = self._mount(tmp)
        rc, pub = _run(["publish", "--candidate", candidate, "--mount-root", mount,
                        "--local-substitute"])
        self.assertEqual(rc, 0, pub)
        self.assertEqual(pub["result"], "published")
        self.assertTrue(pub["ready_written_last"])
        self.assertTrue(pub["identity_verified"])
        self.assertEqual(pub["real_nas"], "none")
        self.assertTrue(pub["mount_is_local_substitute"])

        release_dir = os.path.join(mount, "exchange", "releases", P.RELEASE_ID)
        self.assertTrue(os.path.isfile(os.path.join(release_dir, "READY")))
        rc, val = _run(["validate", "--release-dir", release_dir, "--trust-store", self.store_path])
        self.assertEqual(rc, 0, val)
        self.assertTrue(val["accepted"])
        self.assertIsNone(val["reason"])
        self.assertTrue(val["operational_signature_verified"])
        self.assertEqual(val["evidence_references_verified"], 9)
        self.assertEqual(val["files_verified"], 11)
        self.assertTrue(val["evidence_identity_verified"])
        self.assertEqual(val["algorithm"], "ed25519-v1")
        self.assertFalse(val["signature_synthetic"])
        self.assertEqual(val["trust_store"], "pinned")


class TestAssembleRejections(_ReleaseCliBase):
    def test_rejects_wrong_key_via_committed_store(self):
        # The committed pinned store does NOT contain the disposable key -> unknown_signing_key.
        tmp, candidate = self._fresh_candidate()
        out = self._sign(tmp, candidate)
        rc, r = _run(["assemble", "--candidate", candidate, "--signature", out])  # default = committed store
        self.assertEqual(rc, 2)
        self.assertEqual(r["reason"], "unknown_signing_key")
        self.assertFalse(os.path.exists(os.path.join(candidate, "manifest.sig.json")))
        self.assertFalse(os.path.exists(os.path.join(candidate, "READY")))

    def test_rejects_expired_window(self):
        tmp, candidate = self._fresh_candidate()
        out = self._sign(tmp, candidate)
        narrow = os.path.join(tmp, "narrow_store.json")
        _write_store(narrow, self.provider, {"not_before_utc": "2026-01-01T00:00:00Z",
                                             "not_after_utc": "2026-03-01T00:00:00Z"})  # excludes _CREATED
        rc, r = _run(["assemble", "--candidate", candidate, "--signature", out, "--trust-store", narrow])
        self.assertEqual(rc, 2)
        self.assertEqual(r["reason"], "key_not_valid_at_created_at")

    def test_rejects_tampered_signature(self):
        tmp, candidate = self._fresh_candidate()
        out = self._sign(tmp, candidate)
        with open(out, "r", encoding="utf-8") as fh:
            env = json.load(fh)
        sv = env["signature_value"]
        env["signature_value"] = ("0" if sv[0] != "0" else "1") + sv[1:]
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(env, sort_keys=True) + "\n")
        rc, r = _run(["assemble", "--candidate", candidate, "--signature", out, "--trust-store", self.store_path])
        self.assertEqual(rc, 2)
        self.assertEqual(r["reason"], "signature_invalid")

    def test_rejects_mismatched_manifest(self):
        tmp, candidate = self._fresh_candidate()
        out = self._sign(tmp, candidate)
        with open(os.path.join(candidate, "manifest.json"), "ab") as fh:
            fh.write(b" ")  # changes sha256(manifest) -> request binding breaks
        rc, r = _run(["assemble", "--candidate", candidate, "--signature", out, "--trust-store", self.store_path])
        self.assertEqual(rc, 2)
        self.assertIn(r["reason"], ("request_manifest_mismatch", "manifest_digest_mismatch", "identity_mismatch"))

    def test_rejects_altered_files(self):
        tmp, candidate = self._fresh_candidate()
        out = self._sign(tmp, candidate)
        pq = os.path.join(candidate, "eurusd_m1.parquet")
        with open(pq, "r+b") as fh:
            data = bytearray(fh.read())
            data[len(data) // 2] ^= 0x01
            fh.seek(0)
            fh.write(data)
        rc, r = _run(["assemble", "--candidate", candidate, "--signature", out, "--trust-store", self.store_path])
        self.assertEqual(rc, 2)
        self.assertEqual(r["reason"], "altered_files")

    def test_rejects_already_sealed(self):
        tmp, candidate = self._fresh_candidate()
        self._seal(tmp, candidate)
        out2 = os.path.join(tmp, "again.signature.json")
        _producer_sign(os.path.join(candidate, "request.fields.json"), out2, self.provider)
        rc, r = _run(["assemble", "--candidate", candidate, "--signature", out2, "--trust-store", self.store_path])
        self.assertEqual(rc, 2)
        self.assertEqual(r["reason"], "already_sealed")


class TestPublish(_ReleaseCliBase):
    def test_refuses_overwrite_existing_release(self):
        tmp, candidate = self._fresh_candidate()
        self._seal(tmp, candidate)
        mount = self._mount(tmp)
        rc, _ = _run(["publish", "--candidate", candidate, "--mount-root", mount, "--local-substitute"])
        self.assertEqual(rc, 0)
        rc, r = _run(["publish", "--candidate", candidate, "--mount-root", mount, "--local-substitute"])
        self.assertEqual(rc, 2)
        self.assertEqual(r["reason"], "release_exists")

    def test_requires_mount_identity(self):
        tmp, candidate = self._fresh_candidate()
        self._seal(tmp, candidate)
        mount = self._mount(tmp, with_marker=False)
        rc, r = _run(["publish", "--candidate", candidate, "--mount-root", mount, "--local-substitute"])
        self.assertEqual(rc, 2)
        self.assertEqual(r["reason"], "mount_identity_unverifiable")
        # explicit opt-out proceeds with identity_verified=false
        rc, r = _run(["publish", "--candidate", candidate, "--mount-root", mount, "--local-substitute",
                      "--no-identity-check"])
        self.assertEqual(rc, 0, r)
        self.assertFalse(r["identity_verified"])

    def test_interrupted_publication_leaves_consumer_invisible(self):
        tmp, candidate = self._fresh_candidate()
        self._seal(tmp, candidate)
        # Corrupt a payload file AFTER sealing: the destination hash verify fails mid-publish -> no READY.
        pq = os.path.join(candidate, "eurusd_m1.parquet")
        with open(pq, "r+b") as fh:
            data = bytearray(fh.read())
            data[0] ^= 0x01
            fh.seek(0)
            fh.write(data)
        mount = self._mount(tmp)
        rc, r = _run(["publish", "--candidate", candidate, "--mount-root", mount, "--local-substitute"])
        self.assertEqual(rc, 3)
        self.assertEqual(r["result"], "failed")
        self.assertEqual(r["reason"], "published_file_hash_mismatch")
        self.assertFalse(r["ready_written"])
        self.assertFalse(r["consumer_visible"])
        self.assertIn("cleanup", r)
        release_dir = os.path.join(mount, "exchange", "releases", P.RELEASE_ID)
        self.assertFalse(os.path.isfile(os.path.join(release_dir, "READY")))
        # A consumer cannot accept a READY-less partial.
        rc, val = _run(["validate", "--release-dir", release_dir, "--trust-store", self.store_path])
        self.assertEqual(rc, 4)
        self.assertFalse(val["accepted"])
        self.assertEqual(val["reason"], "missing_ready")

    def test_cleanup_on_failure_removes_partial(self):
        tmp, candidate = self._fresh_candidate()
        self._seal(tmp, candidate)
        pq = os.path.join(candidate, "eurusd_m1.parquet")
        with open(pq, "r+b") as fh:
            data = bytearray(fh.read())
            data[0] ^= 0x01
            fh.seek(0)
            fh.write(data)
        mount = self._mount(tmp)
        rc, r = _run(["publish", "--candidate", candidate, "--mount-root", mount, "--local-substitute",
                      "--cleanup-on-failure"])
        self.assertEqual(rc, 3)
        self.assertTrue(r["cleanup"]["performed"])
        self.assertFalse(os.path.exists(os.path.join(mount, "exchange", "releases", P.RELEASE_ID)))


class TestConsumerValidate(_ReleaseCliBase):
    def _publish(self, tmp, candidate):
        mount = self._mount(tmp)
        rc, _ = _run(["publish", "--candidate", candidate, "--mount-root", mount, "--local-substitute"])
        self.assertEqual(rc, 0)
        return os.path.join(mount, "exchange", "releases", P.RELEASE_ID)

    def test_missing_ready(self):
        tmp, candidate = self._fresh_candidate()
        self._seal(tmp, candidate)
        release_dir = self._publish(tmp, candidate)
        os.remove(os.path.join(release_dir, "READY"))
        rc, val = _run(["validate", "--release-dir", release_dir, "--trust-store", self.store_path])
        self.assertEqual(rc, 4)
        self.assertEqual(val["reason"], "missing_ready")

    def test_detects_tampered_published_file(self):
        tmp, candidate = self._fresh_candidate()
        self._seal(tmp, candidate)
        release_dir = self._publish(tmp, candidate)
        pq = os.path.join(release_dir, "eurusd_m1.parquet")
        with open(pq, "r+b") as fh:
            data = bytearray(fh.read())
            data[len(data) // 2] ^= 0x01
            fh.seek(0)
            fh.write(data)
        rc, val = _run(["validate", "--release-dir", release_dir, "--trust-store", self.store_path])
        self.assertEqual(rc, 4)
        self.assertEqual(val["reason"], "file_hash_mismatch")

    def test_validate_is_write_free(self):
        tmp, candidate = self._fresh_candidate()
        self._seal(tmp, candidate)
        release_dir = self._publish(tmp, candidate)
        _chmod_tree(release_dir, dir_mode=0o555, file_mode=0o444)
        try:
            before = _snapshot(release_dir)
            rc, val = _run(["validate", "--release-dir", release_dir, "--trust-store", self.store_path])
            after = _snapshot(release_dir)
        finally:
            _chmod_tree(release_dir, dir_mode=0o755, file_mode=0o644)
        self.assertEqual(rc, 0, val)
        self.assertTrue(val["accepted"])
        self.assertEqual(before, after)  # the read-only consumer wrote nothing

    def test_validate_wrong_key_via_committed_store(self):
        tmp, candidate = self._fresh_candidate()
        self._seal(tmp, candidate)
        release_dir = self._publish(tmp, candidate)
        rc, val = _run(["validate", "--release-dir", release_dir])  # committed store: key not pinned
        self.assertEqual(rc, 4)
        self.assertEqual(val["reason"], "unknown_signing_key")


class TestBuildGuards(_ReleaseCliBase):
    def test_refuses_root_within_repo(self):
        rc, r = _run(["build", "--root", REPO_ROOT, "--signing-key-id", self.kid])
        self.assertEqual(rc, 2)
        self.assertEqual(r["reason"], "root_within_repo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
