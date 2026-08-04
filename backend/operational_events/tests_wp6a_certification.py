"""WP6A — validation tests for the shared-environment operational certification record.

Pure-stdlib file/schema checks (no DB) run under the Django test runner. They fail closed on: a missing
certification area; a verdict outside the vocabulary; executed-evidence integrity (module counts must sum to
the recorded total, and all-passed must be true); a premature/overstated pilot decision; WP6B being claimed
complete; a broken DARK invariant; a missing document; or a secret-like value.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs" / "operations" / "broker-connectivity"
CERT = DOCS_DIR / "wp6a-certification.json"

REQUIRED_AREAS = list("ABCDEFGHI")
VERDICTS = {"PASS", "HOST-VERIFIED", "DEFERRED-WP6B", "PARTIAL", "FAIL"}
DECISIONS = {"GO", "GO_WITH_CONDITIONS", "NO_GO"}
REQUIRED_DOCS = ["wp6a-certification.md", "wp6a-pilot-recommendation.md", "wp6a-certification.json"]

# WP6B scope items the record must list as deferred (packet WP6B DEFERRAL).
WP6B_REQUIRED = {
    "multi-user isolation", "concurrency", "load", "capacity", "failure injection",
    "agent failures", "bridge failures", "worker failures", "database recovery", "stress", "throughput",
}

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?:TOKEN|SECRET|PASSWORD|FERNET|API_KEY)\s*=\s*['\"][^'\"$<{][^'\"]{7,}"),
]


def _load(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


class AreaTests(unittest.TestCase):
    def setUp(self):
        self.c = _load(CERT)

    def test_all_areas_present_with_valid_verdicts(self):
        areas = {a["id"]: a for a in self.c["areas"]}
        missing = set(REQUIRED_AREAS) - set(areas)
        self.assertFalse(missing, f"certification areas missing: {sorted(missing)}")
        for aid, a in areas.items():
            self.assertIn(a["verdict"], VERDICTS, f"area {aid} bad verdict {a['verdict']}")
            self.assertTrue(str(a["evidence"]).strip(), f"area {aid} has empty evidence")
            self.assertIn("host_verified_items", a)


class ExecutedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.e = _load(CERT)["executed_evidence"]

    def test_all_passed_and_make_check_green(self):
        self.assertTrue(self.e["backend_all_passed"], "backend tests must all pass")
        self.assertTrue(self.e["frontend_all_passed"], "frontend tests must all pass")
        self.assertTrue(self.e["make_check_green"], "make check must be green")

    def test_module_counts_sum_to_total(self):
        s = sum(self.e["backend_modules"].values())
        self.assertEqual(
            s, self.e["backend_tests_run"],
            f"backend_modules sum ({s}) != backend_tests_run ({self.e['backend_tests_run']})",
        )

    def test_evidence_is_substantive(self):
        # A shared-environment certification with a handful of tests would be non-credible.
        self.assertGreaterEqual(self.e["backend_tests_run"], 300)
        self.assertGreaterEqual(self.e["frontend_tests_run"], 40)


class PilotRecommendationTests(unittest.TestCase):
    def setUp(self):
        self.r = _load(CERT)["pilot_recommendation"]

    def test_decision_valid_and_honest(self):
        self.assertIn(self.r["decision"], DECISIONS)
        # A GO or GO_WITH_CONDITIONS must enumerate its conditions and the blocks-Trusted-Beta list,
        # so readiness is never overstated as unconditional.
        if self.r["decision"] in ("GO", "GO_WITH_CONDITIONS"):
            self.assertTrue(self.r["conditions"], "GO/GO_WITH_CONDITIONS must list conditions")
            self.assertTrue(self.r["blocks_trusted_beta"], "must list what blocks Trusted Beta")
            self.assertTrue(self.r["does_not_block_internal_pilot"])
        self.assertIn("internal_pilot_limits", self.r)
        self.assertEqual(self.r["internal_pilot_limits"]["max_users"], "5-10")
        self.assertFalse(self.r["internal_pilot_limits"]["automatic_arming"])
        self.assertFalse(self.r["internal_pilot_limits"]["unattended_validation"])


class WP6BDeferralTests(unittest.TestCase):
    def setUp(self):
        self.d = _load(CERT)["wp6b_deferred"]

    def test_wp6b_not_complete(self):
        self.assertFalse(self.d["complete"], "WP6B must not be claimed complete")

    def test_wp6b_scope_covers_required_items(self):
        scope = {s.lower() for s in self.d["scope"]}
        missing = WP6B_REQUIRED - scope
        self.assertFalse(missing, f"WP6B deferred scope missing: {sorted(missing)}")


class DarkInvariantTests(unittest.TestCase):
    def test_dark(self):
        inv = _load(CERT)["dark_invariant"]
        for k in ("all_flags_off", "no_arming", "no_deployment", "no_destructive_testing",
                  "no_failure_injection", "live_accounts_excluded"):
            self.assertTrue(inv[k], f"dark_invariant.{k} must be true")


class DocsAndSecretsTests(unittest.TestCase):
    def test_required_docs_exist(self):
        for name in REQUIRED_DOCS:
            self.assertTrue((DOCS_DIR / name).exists(), f"required WP6A document missing: {name}")

    def test_no_secret_like_values(self):
        text = CERT.read_text(encoding="utf-8")
        for pat in SECRET_PATTERNS:
            self.assertIsNone(pat.search(text), f"secret-like value in {CERT.name}: {pat.pattern}")


if __name__ == "__main__":
    unittest.main()
