"""Validation of the Hosted-workspace arming-flag inventory (ADR-0033).

Pure-stdlib (no DB), mirroring operational_events/tests_ipr_beta_flags.py. Fail closed on: any of the
three HOSTED_* flags missing from the inventory; any missing a definition site / OFF default / risk
grade; the source accessor not defaulting OFF; or a secret-like value in the file. Kept as a NEW file
so the shared WP5.4 readiness test is not modified.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FLAGS_JSON = REPO_ROOT / "docs" / "operations" / "broker-connectivity" / "feature-flags.json"
FLAGS_SRC = REPO_ROOT / "backend" / "hosted_workspace" / "flags.py"

REQUIRED_HOSTED_FLAGS = {
    "HOSTED_PERSISTENT_MT5_ENABLED",
    "HOSTED_MT5_REMOTEAPP_ENABLED",
    "HOSTED_MT5_ACTIVE_ACCOUNT_POLLING_ENABLED",
}

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:TOKEN|SECRET|PASSWORD|FERNET|API_KEY)\s*=\s*['\"][^'\"$<{][^'\"]{7,}"),
]


class HostedFlagInventoryTests(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads(FLAGS_JSON.read_text(encoding="utf-8"))
        self.section = self.doc.get("hosted_workspace_flags", {})
        self.flags = {f["name"]: f for f in self.section.get("flags", [])}

    def test_all_three_present(self):
        self.assertEqual(set(self.flags), REQUIRED_HOSTED_FLAGS)

    def test_section_declares_defaults_off(self):
        self.assertIs(self.section.get("all_defaults_off"), True)

    def test_each_flag_documented_and_off(self):
        for name, f in self.flags.items():
            self.assertTrue(f.get("definition_site"), f"{name} missing definition_site")
            self.assertEqual(f.get("default"), "OFF", f"{name} inventory default must be OFF")
            self.assertIn(f.get("risk"), {"AMBER", "RED"}, f"{name} risk must be graded")
            self.assertTrue(f.get("effect_when_enabled"))
            self.assertTrue(f.get("sponsor_approval_required"))

    def test_source_helper_defaults_off_and_accessors_pass_no_truthy_default(self):
        src = FLAGS_SRC.read_text(encoding="utf-8")
        # The shared helper must default to an empty (falsey) string.
        self.assertRegex(src, r'def\s+_flag\(\s*name:\s*str\s*,\s*default:\s*str\s*=\s*""\s*\)')
        # Each accessor must call _flag("NAME") with NO second (default) argument -> inherits OFF.
        for name in REQUIRED_HOSTED_FLAGS:
            self.assertRegex(
                src, rf'_flag\(\s*["\']{re.escape(name)}["\']\s*\)',
                f"{name} accessor must call _flag(name) with no truthy default",
            )

    def test_no_secret_like_values(self):
        text = FLAGS_JSON.read_text(encoding="utf-8")
        for pat in SECRET_PATTERNS:
            self.assertIsNone(pat.search(text), f"secret-like value in feature-flags.json: {pat.pattern}")


if __name__ == "__main__":
    unittest.main()
