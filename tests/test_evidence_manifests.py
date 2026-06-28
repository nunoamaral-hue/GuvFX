"""Tests for the evidence-manifest schema/format linter (scripts/check_evidence_manifests.py).

Standard-library only. Validates that a well-formed manifest passes and that
missing-field, wrong-type, bad-status and malformed-checksum manifests are caught.
Also asserts that the *real* committed manifests currently lint clean (PASS), so a
genuine schema/format regression in evidence is caught in CI.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_evidence_manifests as cem  # noqa: E402


def _good():
    return {
        "schema_version": "1.0",
        "handoff_id": "GFX-EVD-TEST",
        "packet_id": "GFX-PKT-TEST",
        "created_at_utc": "2026-06-28T00:00:00Z",
        "branch": "main",
        "base_commit": "0" * 40,
        "head_commit": None,
        "commands": ["make check"],
        "expected_results": ["ok"],
        "actual_results": ["ok"],
        "status": "PASS",
        "limitations": ["none"],
        "artefact_locations": ["docs/STATUS.md"],
        "checksums": {"docs/STATUS.md": "sha256:" + "a" * 64},
        "reviewer": None,
    }


class TestEvidenceLinter(unittest.TestCase):
    def setUp(self):
        import json
        with open(cem.SCHEMA_PATH, encoding="utf-8") as handle:
            self.schema = json.load(handle)

    def test_good_manifest_passes(self):
        self.assertEqual(cem.lint_data("good.json", _good(), self.schema), [])

    def test_missing_required_field_flagged(self):
        data = _good()
        del data["status"]
        findings = cem.lint_data("m.json", data, self.schema)
        self.assertTrue(any("missing required field 'status'" in f for f in findings))

    def test_bad_status_enum_flagged(self):
        data = _good()
        data["status"] = "GREEN"
        findings = cem.lint_data("m.json", data, self.schema)
        self.assertTrue(any("status" in f and "not in allowed" in f for f in findings))

    def test_wrong_type_flagged(self):
        data = _good()
        data["commands"] = "make check"  # should be an array
        findings = cem.lint_data("m.json", data, self.schema)
        self.assertTrue(any("'commands' has wrong type" in f for f in findings))

    def test_malformed_checksum_flagged(self):
        data = _good()
        data["checksums"] = {"docs/STATUS.md": "deadbeef"}  # no sha256: prefix / wrong length
        findings = cem.lint_data("m.json", data, self.schema)
        self.assertTrue(any("not a sha256:<64-hex>" in f for f in findings))

    def test_real_committed_manifests_lint_clean(self):
        import glob
        import os
        files = sorted(glob.glob(cem.MANIFEST_GLOB))
        self.assertGreater(len(files), 0, "expected committed evidence manifests")
        all_findings = []
        for path in files:
            all_findings.extend(cem.lint_file(path, self.schema))
        self.assertEqual(all_findings, [], "committed manifests must lint clean: %s" % all_findings)


if __name__ == "__main__":
    unittest.main()
