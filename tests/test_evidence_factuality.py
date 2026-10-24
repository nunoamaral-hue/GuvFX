"""Tests for point-in-time evidence verification."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_evidence_factuality as cef  # noqa: E402


class EvidenceFactualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Test")
        (self.repo / "report.txt").write_text("Ran 3 tests in 0.01s\n\nOK\n", encoding="utf-8")
        (self.repo / "doc.md").write_text("evidence\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "fixture")
        self.commit = self._git("rev-parse", "HEAD").strip()

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True)

    def _manifest(self):
        checksum = hashlib.sha256((self.repo / "doc.md").read_bytes()).hexdigest()
        return {
            "head_commit": self.commit,
            "artefact_locations": ["doc.md", "report.txt"],
            "checksums": {"doc.md": "sha256:" + checksum},
            "factuality": {
                "tracked_file_count": 2,
                "require_clean_tree": True,
                "test_runs": [{
                    "output_path": "report.txt",
                    "framework": "unittest",
                    "expected_count": 3,
                    "expected_status": "PASS",
                }],
            },
        }

    def test_verified_commit_claims_pass(self):
        self.assertEqual(cef.verify_data("manifest.json", self._manifest(), str(self.repo)), [])

    def test_checksum_and_test_count_mismatch_fail(self):
        manifest = self._manifest()
        manifest["checksums"]["doc.md"] = "sha256:" + "0" * 64
        manifest["factuality"]["test_runs"][0]["expected_count"] = 4
        findings = cef.verify_data("manifest.json", manifest, str(self.repo))
        self.assertTrue(any("checksum mismatch" in item for item in findings))
        self.assertTrue(any("test count" in item for item in findings))

    def test_unsafe_path_is_rejected(self):
        manifest = self._manifest()
        manifest["artefact_locations"] = ["../outside"]
        findings = cef.verify_data("manifest.json", manifest, str(self.repo))
        self.assertTrue(any("unsafe artefact path" in item for item in findings))

    def test_dirty_tree_is_rejected(self):
        (self.repo / "untracked.txt").write_text("dirty", encoding="utf-8")
        findings = cef.verify_data("manifest.json", self._manifest(), str(self.repo))
        self.assertTrue(any("working tree is not clean" in item for item in findings))

    def test_non_ancestor_commit_is_rejected(self):
        first = self.commit
        manifest = self._manifest()
        self._git("checkout", "-q", "--orphan", "other")
        for path in self.repo.iterdir():
            if path.name != ".git" and path.is_file():
                path.unlink()
        (self.repo / "other.txt").write_text("other", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "other")
        manifest["head_commit"] = first
        findings = cef.verify_data("manifest.json", manifest, str(self.repo))
        self.assertTrue(any("not an ancestor" in item for item in findings))


if __name__ == "__main__":
    unittest.main()
