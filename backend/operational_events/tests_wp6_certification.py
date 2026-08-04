"""WP6 — validation tests for the multi-tenant certification programme.

Pure-stdlib file/schema checks (no DB) discovered as ``test*.py`` and run under the Django test runner
(backend-test / CI backend). They fail closed on: a missing certification area; an exposure-opening route
that is not covered; a support workflow that is not exercised; an incomplete release gate; a premature
release recommendation; a certification case marked PASS in the repo; a secret-like value; or a broken DARK
invariant. Grounded against docs/operations/broker-connectivity/wp6-*.json and
backend/execution/execution_entrypoints.json.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs" / "operations" / "broker-connectivity"
MATRIX = DOCS_DIR / "wp6-test-matrix.json"
EVIDENCE = DOCS_DIR / "wp6-evidence.json"
GATE = DOCS_DIR / "wp6-release-gate.json"
ENTRYPOINTS = REPO_ROOT / "backend" / "execution" / "execution_entrypoints.json"

AREAS = list("ABCDEFGHIJKL")

REQUIRED_WORKFLOWS = {
    "cannot_add_account", "connection_test_fails", "technical_validation_unavailable",
    "invalid_credentials", "live_account_where_demo_required", "account_disconnected",
    "credential_replaced", "broker_health_degraded", "broker_health_stale", "runtime_paused",
    "controlled_resume_requested", "execution_refused", "operational_timeline_empty",
    "event_visible_operator_not_customer", "duplicate_or_missing_event", "delete_broker_account",
    "credential_removal",
}

REQUIRED_DOCS = [
    "wp6-README.md", "wp6-test-environment.md", "wp6-isolation.md", "wp6-concurrency.md",
    "wp6-execution-safety.md", "wp6-health.md", "wp6-operational-events.md", "wp6-operator-workflow.md",
    "wp6-failure-injection.md", "wp6-recovery.md", "wp6-rollback-rehearsal.md", "wp6-capacity.md",
    "wp6-release-recommendation.md", "wp6-test-matrix.json", "wp6-evidence.json", "wp6-release-gate.json",
]

CASE_FIELDS = {"id", "area", "title", "method", "expected", "evidence", "pass_criteria", "status"}
GATE_ITEM_FIELDS = {"id", "area", "requirement", "evidence_ref", "pass_criteria", "blocks_go",
                    "safety_critical", "status"}

DONE_STATUSES = {"PASS"}

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?:TOKEN|SECRET|PASSWORD|FERNET|API_KEY)\s*=\s*['\"][^'\"$<{][^'\"]{7,}"),
]


def _load(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _exposure_routes() -> set[str]:
    inv = _load(ENTRYPOINTS)
    return {
        f"{r['file']}::{r['function']}"
        for r in inv["routes"]
        if r.get("opens_exposure") is True
    }


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.m = _load(MATRIX)
        self.cases = self.m["cases"]

    def test_all_areas_declared_and_have_a_case(self):
        declared = {a["id"] for a in self.m["areas"]}
        self.assertEqual(declared, set(AREAS), "matrix must declare exactly areas A-L")
        case_areas = {c["area"] for c in self.cases}
        missing = set(AREAS) - case_areas
        self.assertFalse(missing, f"areas with no certification case: {sorted(missing)}")

    def test_every_case_has_required_fields(self):
        ids = set()
        for c in self.cases:
            missing = CASE_FIELDS - set(c)
            self.assertFalse(missing, f"case {c.get('id')} missing fields: {sorted(missing)}")
            self.assertNotIn(c["id"], ids, f"duplicate case id {c['id']}")
            ids.add(c["id"])

    def test_no_case_marked_pass(self):
        for c in self.cases:
            self.assertNotIn(
                c["status"].upper(), DONE_STATUSES,
                f"case {c['id']} is marked PASS — nothing may be certified complete in the repo",
            )

    def test_exposure_route_coverage_declared_exactly(self):
        expected = _exposure_routes()
        declared = set(self.m["route_coverage"]["routes"])
        self.assertEqual(
            declared, expected,
            f"route_coverage must equal the exposure-opening routes.\n"
            f"missing: {sorted(expected - declared)}\nextra: {sorted(declared - expected)}",
        )

    def test_every_exposure_route_covered_by_a_case(self):
        expected = _exposure_routes()
        covered = set()
        for c in self.cases:
            covered.update(c.get("covers_routes", []))
        uncovered = expected - covered
        self.assertFalse(uncovered, f"exposure-opening routes with no covering case: {sorted(uncovered)}")

    def test_all_workflows_covered(self):
        declared = set(self.m["workflow_coverage"]["workflows"])
        self.assertEqual(declared, REQUIRED_WORKFLOWS, "workflow_coverage must list all 17 workflows")
        case_workflows = {c["workflow"] for c in self.cases if c.get("area") == "G" and "workflow" in c}
        missing = REQUIRED_WORKFLOWS - case_workflows
        self.assertFalse(missing, f"support workflows with no area-G case: {sorted(missing)}")

    def test_dark_invariant(self):
        inv = self.m["dark_invariant"]
        self.assertTrue(inv["all_flags_off"])
        self.assertTrue(inv["no_arming"])
        self.assertTrue(inv["no_deployment"])
        self.assertTrue(inv["customer_zero_excluded"])
        self.assertTrue(inv["live_accounts_excluded"])


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.g = _load(GATE)

    def test_no_premature_recommendation(self):
        self.assertIsNone(self.g["recommendation"], "release recommendation must be null until certification completes")
        self.assertFalse(self.g["trusted_beta"]["armed"], "Trusted-Beta must not be armed")
        self.assertFalse(self.g["trusted_beta"]["invitations_open"])
        self.assertFalse(self.g["wp6"]["execution_complete"], "WP6 execution must not be marked complete")

    def test_outcomes_and_matrix_present(self):
        self.assertEqual(set(self.g["outcomes"]), {"GO", "GO_WITH_CONDITIONS", "NO_GO"})
        for k in ("GO", "GO_WITH_CONDITIONS", "NO_GO"):
            self.assertTrue(str(self.g["decision_matrix"][k]).strip(), f"decision_matrix.{k} empty")

    def test_every_gate_item_complete_and_pending(self):
        items = self.g["gate_items"]
        self.assertTrue(items, "gate_items empty")
        for it in items:
            missing = GATE_ITEM_FIELDS - set(it)
            self.assertFalse(missing, f"gate item {it.get('id')} missing fields: {sorted(missing)}")
            for f in ("requirement", "evidence_ref", "pass_criteria"):
                self.assertTrue(str(it[f]).strip(), f"gate item {it['id']} empty {f}")
            self.assertIsInstance(it["blocks_go"], bool)
            self.assertIsInstance(it["safety_critical"], bool)
            self.assertNotEqual(it["status"].upper(), "PASS", f"gate item {it['id']} prematurely PASS")

    def test_safety_critical_areas_have_gate_items(self):
        crit = set(self.g["safety_critical_areas"])
        covered = {it["area"] for it in self.g["gate_items"] if it["safety_critical"]}
        missing = crit - covered
        self.assertFalse(missing, f"safety-critical areas without a safety-critical gate item: {sorted(missing)}")


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.e = _load(EVIDENCE)

    def test_manifest_schema_referenced_and_fields_present(self):
        self.assertEqual(self.e["evidence_manifest_schema"], "evidence/schema/evidence-manifest.schema.json")
        self.assertTrue((REPO_ROOT / self.e["evidence_manifest_schema"]).exists(),
                        "referenced evidence-manifest schema must exist")
        self.assertTrue(self.e["manifest_required_fields"])

    def test_every_area_has_an_evidence_requirement(self):
        areas = {r["area"] for r in self.e["requirements"]}
        missing = set(AREAS) - areas
        self.assertFalse(missing, f"areas with no evidence requirement: {sorted(missing)}")

    def test_never_included_list_present(self):
        never = self.e["what_must_never_be_included"]
        self.assertTrue(any("secret" in x.lower() for x in never))
        self.assertTrue(any("customer zero" in x.lower() or "live-account" in x.lower() for x in never))


class RequiredDocsTests(unittest.TestCase):
    def test_all_required_docs_exist(self):
        for name in REQUIRED_DOCS:
            self.assertTrue((DOCS_DIR / name).exists(), f"required WP6 document missing: {name}")


class NoSecretsTests(unittest.TestCase):
    def test_json_artefacts_have_no_secret_like_values(self):
        for path in (MATRIX, EVIDENCE, GATE):
            text = path.read_text(encoding="utf-8")
            for pat in SECRET_PATTERNS:
                self.assertIsNone(pat.search(text), f"secret-like value in {path.name}: {pat.pattern}")


if __name__ == "__main__":
    unittest.main()
