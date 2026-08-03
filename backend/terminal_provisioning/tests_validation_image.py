"""ADR-0027 Phase 2 — governed build-5833 validation-IMAGE allow-list + fail-closed verification tests.

Imports the ``validation_image`` governance module from the deploy/beta-agent bundle and proves it fails closed
on every contamination class: forbidden account artefacts, missing allow-listed files, hash drift and a missing
run-in layer. Pure filesystem fixtures; the SHA-256 function is injected so no real MT5 binary is required.
"""
import os
import sys
import tempfile

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "beta-agent")
if _BUNDLE not in sys.path:
    sys.path.insert(0, _BUNDLE)

import validation_image as vi          # noqa: E402


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _clean_image(root):
    """A minimal image that PASSES: every allow-listed file + a run-in .ex5 layer, no forbidden artefact."""
    for rel in vi.ALLOW_LIST:
        _write(os.path.join(root, rel.replace("/", os.sep)))
    ex5dir = os.path.join(root, "MQL5", "Include")
    for i in range(vi.MIN_RUN_IN_EX5 + 5):
        _write(os.path.join(ex5dir, f"lib{i}.ex5"))


def _hash_ok(path):
    """Injected SHA that returns the pinned hash for an allow-listed file (so a fixture 'matches')."""
    rel = os.path.relpath(path).replace("\\", "/").lower()
    for a in vi.ALLOW_LIST:
        if rel.endswith(a):
            return vi.SOURCE_HASHES[a]
    return "0" * 64


class ValidationImageAllowListTests(SimpleTestCase):
    def test_clean_image_passes(self):
        with tempfile.TemporaryDirectory() as d:
            _clean_image(d)
            rep = vi.verify_image(d, sha256=_hash_ok)
            self.assertTrue(rep["ok"])
            self.assertEqual(rep["account_artefact_count"], 0)
            self.assertEqual(rep["attached_ea_count"], 0)
            self.assertGreaterEqual(rep["ex5_count"], vi.MIN_RUN_IN_EX5)
            self.assertEqual(rep["terminal_build"], "5.0.0.5833")

    def test_accounts_dat_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            _clean_image(d)
            _write(os.path.join(d, "config", "accounts.dat"))
            with self.assertRaises(vi.ValidationImageError) as cm:
                vi.verify_image(d, sha256=_hash_ok)
            self.assertEqual(cm.exception.reason, "forbidden_artefact_present")

    def test_history_and_logs_rejected(self):
        for bad in (os.path.join("bases", "Default", "history", "EURUSD", "2023.hcc"),
                    os.path.join("logs", "20260803.log")):
            with tempfile.TemporaryDirectory() as d:
                _clean_image(d)
                _write(os.path.join(d, bad))
                with self.assertRaises(vi.ValidationImageError):
                    vi.verify_image(d, sha256=_hash_ok)

    def test_missing_allow_listed_file_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            _clean_image(d)
            os.remove(os.path.join(d, "terminal64.exe"))
            with self.assertRaises(vi.ValidationImageError) as cm:
                vi.verify_image(d, sha256=_hash_ok)
            self.assertEqual(cm.exception.reason, "allow_listed_file_missing")

    def test_hash_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            _clean_image(d)
            with self.assertRaises(vi.ValidationImageError) as cm:
                vi.verify_image(d, sha256=lambda p: "deadbeef")
            self.assertEqual(cm.exception.reason, "allow_listed_file_hash_mismatch")

    def test_run_in_layer_missing_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            for rel in vi.ALLOW_LIST:                        # allow-list present but NO .ex5 run-in
                _write(os.path.join(d, rel.replace("/", os.sep)))
            with self.assertRaises(vi.ValidationImageError) as cm:
                vi.verify_image(d, sha256=_hash_ok)
            self.assertEqual(cm.exception.reason, "run_in_layer_missing")

    def test_missing_dir_rejected(self):
        with self.assertRaises(vi.ValidationImageError):
            vi.verify_image(os.path.join(tempfile.gettempdir(), "gvfx-nope-xyz"), sha256=_hash_ok)


class ValidationImageSourceTests(SimpleTestCase):
    def test_source_hashes_pass(self):
        with tempfile.TemporaryDirectory() as d:
            for rel in vi.ALLOW_LIST:
                _write(os.path.join(d, rel.replace("/", os.sep)))
            vi.verify_source_hashes(d, sha256=_hash_ok)    # no raise

    def test_source_hash_drift_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            for rel in vi.ALLOW_LIST:
                _write(os.path.join(d, rel.replace("/", os.sep)))
            with self.assertRaises(vi.ValidationImageError) as cm:
                vi.verify_source_hashes(d, sha256=lambda p: "deadbeef")
            self.assertEqual(cm.exception.reason, "source_hash_drift")

    def test_source_missing_file_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "terminal64.exe"))       # only one allow-listed file
            with self.assertRaises(vi.ValidationImageError) as cm:
                vi.verify_source_hashes(d, sha256=_hash_ok)
            self.assertEqual(cm.exception.reason, "source_allow_listed_file_missing")


class ValidationImageFingerprintTests(SimpleTestCase):
    def test_fingerprint_stable_and_excludes_logs(self):
        with tempfile.TemporaryDirectory() as d:
            _clean_image(d)
            fp1 = vi.structural_fingerprint(d)
            _write(os.path.join(d, "logs", "20260803.log"), b"volatile")   # a log must NOT change the fp
            fp2 = vi.structural_fingerprint(d)
            self.assertEqual(fp1, fp2)
            self.assertEqual(len(fp1), 64)
