"""IPR Area E — validation of the beta arming-flag inventory.

Pure-stdlib checks (no DB). Fail closed on: any required BETA arming flag missing from the inventory; any
missing a definition site / default; any not defaulting OFF (in the inventory AND in source); or a
secret-like value in the file. Kept as a NEW file so the shared WP5.4 readiness test is not modified.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FLAGS_JSON = REPO_ROOT / "docs" / "operations" / "broker-connectivity" / "feature-flags.json"

REQUIRED_BETA_FLAGS = {"BETA_ONBOARDING_ENABLED", "BETA_RUNTIMES_ENABLED", "BETA_SELF_SERVE_ARM_ENABLED",
                       "BETA_ADMISSION_ARM_ENABLED"}

# Each beta flag's accessor source file — asserted to carry an OFF (falsey) default.
SOURCE_DEFAULT_OFF = {
    "BETA_ONBOARDING_ENABLED": REPO_ROOT / "backend" / "billing" / "beta.py",
    "BETA_RUNTIMES_ENABLED": REPO_ROOT / "backend" / "terminal_provisioning" / "beta_capacity.py",
    "BETA_SELF_SERVE_ARM_ENABLED": REPO_ROOT / "backend" / "strategies" / "views.py",
    # ADR-0045 — beta-admission-derived arm authorization (accessor in strategies/views.py).
    "BETA_ADMISSION_ARM_ENABLED": REPO_ROOT / "backend" / "strategies" / "views.py",
}

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:TOKEN|SECRET|PASSWORD|FERNET|API_KEY)\s*=\s*['\"][^'\"$<{][^'\"]{7,}"),
]


class BetaFlagInventoryTests(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads(FLAGS_JSON.read_text(encoding="utf-8"))
        self.section = self.doc.get("beta_arming_flags", {})
        self.flags = {f["name"]: f for f in self.section.get("flags", [])}

    def test_all_beta_arming_flags_present(self):
        self.assertEqual(set(self.flags), REQUIRED_BETA_FLAGS)

    def test_section_declares_defaults_off(self):
        self.assertTrue(self.section.get("all_defaults_off") is True)

    def test_each_flag_documented_and_off(self):
        for name, f in self.flags.items():
            self.assertTrue(f.get("definition_site"), f"{name} missing definition_site")
            self.assertEqual(f.get("default"), "OFF", f"{name} inventory default must be OFF")
            self.assertIn(f.get("risk"), {"AMBER", "RED"}, f"{name} risk must be graded")
            self.assertTrue(f.get("effect_when_enabled"))

    def test_source_defaults_are_off(self):
        # The accessor must read an env var with an empty/falsey default (never a truthy literal).
        for name, path in SOURCE_DEFAULT_OFF.items():
            src = path.read_text(encoding="utf-8")
            self.assertRegex(
                src, rf'os\.getenv\(\s*["\']{re.escape(name)}["\']\s*,\s*["\']\s*["\']\s*\)',
                f"{name} accessor must default to empty string (OFF) in {path.name}",
            )

    def test_no_secret_like_values(self):
        text = FLAGS_JSON.read_text(encoding="utf-8")
        for pat in SECRET_PATTERNS:
            self.assertIsNone(pat.search(text), f"secret-like value in feature-flags.json: {pat.pattern}")


if __name__ == "__main__":
    unittest.main()
