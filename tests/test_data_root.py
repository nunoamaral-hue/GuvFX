"""Tests for the GUVFX_DATA_ROOT preflight validator (scripts/check_data_root.py).

Standard-library only; uses temporary directories, never the real NAS. Proves the
gate accepts a marked/labelled dedicated target, rejects unmarked/missing/inside-git
roots, and that the result never contains the absolute path (privacy).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_data_root as cdr  # noqa: E402


class TestDataRootValidator(unittest.TestCase):
    def test_unset_root_fails(self):
        r = cdr.validate(None)
        self.assertFalse(r["data_root_set"])
        self.assertFalse(r["gate_pass"])

    def test_missing_dir_fails(self):
        r = cdr.validate("/nonexistent/guvfxdata/should-not-exist-xyz")
        self.assertFalse(r["exists_dir"])
        self.assertFalse(r["gate_pass"])

    def test_marked_dedicated_target_passes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "data_landing"
            root.mkdir()
            (root / cdr.MARKER).write_text("marker")
            r = cdr.validate(str(root))
            self.assertTrue(r["approved_marker_or_label"])
            self.assertEqual(r["logical_label"], "GuvFXData")
            self.assertTrue(r["outside_git"])
            self.assertTrue(r["writable_atomic_rename"])
            self.assertEqual(r["free_space_class"], "sufficient")
            self.assertTrue(r["gate_pass"])

    def test_label_named_target_passes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "GuvFXData"
            root.mkdir()
            r = cdr.validate(str(root))
            self.assertTrue(r["approved_marker_or_label"])
            self.assertTrue(r["gate_pass"])

    def test_unmarked_unlabeled_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "some_random_dir"
            root.mkdir()
            r = cdr.validate(str(root))
            self.assertFalse(r["approved_marker_or_label"])
            self.assertFalse(r["gate_pass"])

    def test_inside_git_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "GuvFXData"
            root.mkdir()
            (root / cdr.MARKER).write_text("m")
            (root / ".git").mkdir()
            r = cdr.validate(str(root))
            self.assertFalse(r["outside_git"])
            self.assertFalse(r["gate_pass"])

    def test_result_never_contains_path(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "GuvFXData"
            root.mkdir()
            (root / cdr.MARKER).write_text("m")
            blob = json.dumps(cdr.validate(str(root)))
            self.assertNotIn(str(root), blob)
            self.assertNotIn(d, blob)


if __name__ == "__main__":
    unittest.main()
