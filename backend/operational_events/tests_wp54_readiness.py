"""WP5.4 — validation tests for the Trusted-Beta operations-readiness package.

These are pure-stdlib file/schema checks (no DB, no Django models). They run under the Django test
runner (backend-test / CI backend job) because they are discovered as ``test*.py``. They fail closed on:

  * a known broker-connectivity flag appearing in source without inventory coverage;
  * any inventory flag missing a default or owner, defaulting ON, or with a dangling/circular dependency;
  * a checklist item missing owner/evidence/pass-rule, or wrongly marked complete;
  * an arming step without a rollback, or a missing partial-arming state;
  * a required document that does not exist;
  * a secret-like value in the machine-readable artefacts;
  * a flag whose repository default is not OFF;
  * WP6 being marked authorised/complete, or Trusted-Beta armed.

Grounded against the merged repo; see docs/operations/broker-connectivity/ and feature-flags.json.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs" / "operations" / "broker-connectivity"
FLAGS_JSON = DOCS_DIR / "feature-flags.json"
CHECKLIST_JSON = DOCS_DIR / "readiness-checklist.json"

# The canonical broker-connectivity / operational-event arming flags that MUST be inventoried.
REQUIRED_FLAGS = {
    "BROKER_CONNECTIVITY_ENABLED",
    "BROKER_CONNECTIVITY_EXECUTION_GATE",
    "BROKER_CONNECTIVITY_HEALTH_ENABLED",
    "NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED",
    "OPERATIONS_EVENTS_ENABLED",
    "NEXT_PUBLIC_OPERATIONS_ENABLED",
}

# Files that DEFINE (read) the arming flags — scanned for undocumented flags + OFF defaults.
FLAG_DEFINITION_FILES = [
    REPO_ROOT / "backend" / "trading" / "broker_connectivity.py",
    REPO_ROOT / "backend" / "execution" / "broker_gate.py",
    REPO_ROOT / "backend" / "reliability" / "constants.py",
    REPO_ROOT / "backend" / "operational_events" / "constants.py",
    REPO_ROOT / "frontend" / "src" / "lib" / "flags.ts",
]

# A flag NAME is an ARMING flag (must be inventoried) if it matches one of these.
ARMING_FLAG_PATTERNS = [
    re.compile(r"^BROKER_CONNECTIVITY_[A-Z_]+$"),
    re.compile(r"^OPERATIONS_EVENTS_ENABLED$"),
    re.compile(r"^NEXT_PUBLIC_(?:BROKER_CONNECTIVITY|OPERATIONS)[A-Z_]*ENABLED$"),
]

FLAG_REQUIRED_FIELDS = {
    "name", "owner", "scope", "layer", "default", "timing", "definition_site", "depends_on",
    "effect_when_enabled", "effect_when_disabled", "deploy_requirement", "verification",
    "rollback", "risk", "sponsor_approval_required", "adr",
}

CHECKLIST_ITEM_FIELDS = {
    "id", "category", "item", "owner", "evidence_required", "pass_rule",
    "blocks_wp6", "blocks_arming", "status",
}

ARMING_STEP_FIELDS = {"order", "name", "purpose", "sponsor_gate", "stop_condition", "rollback"}

PARTIAL_STATE_FIELDS = {
    "id", "state", "customer_impact", "trading_impact",
    "new_exposure_possible", "safest_action", "flag_rollback",
}

# Every partial-arming state WP5.4 Workstream E requires be represented.
REQUIRED_PARTIAL_STATES = {
    "customer_frontend_only", "customer_backend_only", "operational_events_only",
    "operator_frontend_only", "health_enabled_no_gate", "execution_gate_enabled",
    "mixed_frontend_backend_versions", "provisioner_protocol_mismatch", "agent_unavailable",
    "validation_image_failure", "migration_failure", "operational_timeline_unavailable",
    "health_not_converging", "execution_refusal_spike",
}

REQUIRED_DOCS = [
    "README.md", "feature-flags.md", "feature-flags.json", "arming-runbook.md",
    "rollback-matrix.md", "incident-response.md", "support-playbook.md", "monitoring-spec.md",
    "trusted-beta-readiness.md", "evidence-pack.md", "readiness-checklist.json",
]

# Truthy tokens the code treats as ON — a repo default must NOT be any of these.
TRUTHY_TOKENS = {"1", "true", "yes", "on"}

# Secret-like patterns that must NEVER appear in the machine-readable artefacts.
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # a flag/token being ASSIGNED a concrete value (env-style) — names alone are fine
    re.compile(r"(?:TOKEN|SECRET|PASSWORD|FERNET|API_KEY)\s*=\s*['\"][^'\"$<{][^'\"]{7,}"),
]


def _load(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _scan_env_flag_names(text: str) -> set[str]:
    """Extract env-var names read via os.getenv("X"...) or process.env.X."""
    names: set[str] = set()
    names.update(re.findall(r"os\.getenv\(\s*['\"]([A-Z0-9_]+)['\"]", text))
    names.update(re.findall(r"process\.env\.([A-Z0-9_]+)", text))
    return names


class FeatureFlagInventoryTests(unittest.TestCase):
    def setUp(self):
        self.inv = _load(FLAGS_JSON)
        self.flags = {f["name"]: f for f in self.inv["flags"]}

    def test_all_required_flags_present(self):
        missing = REQUIRED_FLAGS - set(self.flags)
        self.assertFalse(missing, f"feature-flags.json missing required flags: {sorted(missing)}")

    def test_every_flag_has_required_fields_owner_and_default(self):
        for name, flag in self.flags.items():
            missing = FLAG_REQUIRED_FIELDS - set(flag)
            self.assertFalse(missing, f"flag {name} missing fields: {sorted(missing)}")
            self.assertTrue(str(flag["owner"]).strip(), f"flag {name} has empty owner")
            self.assertIn("default", flag, f"flag {name} has no default")

    def test_required_arming_flags_default_off(self):
        for name in REQUIRED_FLAGS:
            self.assertEqual(
                self.flags[name]["default"].upper(), "OFF",
                f"flag {name} must default OFF in the inventory",
            )

    def test_dependencies_reference_existing_flags(self):
        for name, flag in self.flags.items():
            for dep in flag.get("depends_on", []):
                self.assertIn(dep, self.flags, f"flag {name} depends on unknown flag {dep}")

    def test_no_circular_dependency(self):
        graph = {n: list(f.get("depends_on", [])) for n, f in self.flags.items()}
        WHITE, GREY, BLACK = 0, 1, 2
        colour = {n: WHITE for n in graph}

        def visit(node, stack):
            colour[node] = GREY
            for dep in graph.get(node, []):
                if colour.get(dep) == GREY:
                    self.fail(f"circular flag dependency: {' -> '.join(stack + [dep])}")
                if colour.get(dep) == WHITE:
                    visit(dep, stack + [dep])
            colour[node] = BLACK

        for n in graph:
            if colour[n] == WHITE:
                visit(n, [n])

    def test_no_known_flag_in_source_without_inventory_coverage(self):
        """Every arming flag referenced in a definition file must be inventoried."""
        found: set[str] = set()
        for path in FLAG_DEFINITION_FILES:
            self.assertTrue(path.exists(), f"flag definition file missing: {path}")
            text = path.read_text(encoding="utf-8")
            for candidate in _scan_env_flag_names(text):
                if any(p.match(candidate) for p in ARMING_FLAG_PATTERNS):
                    found.add(candidate)
        # every required flag must actually be found in source (grounds the inventory in code)
        not_in_source = REQUIRED_FLAGS - found
        self.assertFalse(not_in_source, f"required flags not found in source: {sorted(not_in_source)}")
        # and every arming flag found in source must be inventoried (no hidden flag)
        uncovered = found - set(self.flags)
        self.assertFalse(uncovered, f"arming flag(s) in source but not inventoried: {sorted(uncovered)}")

    def test_repo_default_off_for_backend_and_frontend(self):
        """No command/definition enables a flag in repository defaults."""
        # backend: os.getenv("FLAG", "<default>") default must be falsey
        for path in FLAG_DEFINITION_FILES:
            text = path.read_text(encoding="utf-8")
            for name, default in re.findall(
                r"os\.getenv\(\s*['\"]([A-Z0-9_]+)['\"]\s*,\s*['\"]([^'\"]*)['\"]", text
            ):
                if any(p.match(name) for p in ARMING_FLAG_PATTERNS):
                    self.assertNotIn(
                        default.strip().lower(), TRUTHY_TOKENS,
                        f"{name} defaults ON ('{default}') in {path.name} — must default OFF",
                    )
        # frontend: flags.ts must gate NEXT_PUBLIC_* via truthy(process.env.X), never hardcode true
        flags_ts = (REPO_ROOT / "frontend" / "src" / "lib" / "flags.ts").read_text(encoding="utf-8")
        self.assertIn("truthy(process.env.NEXT_PUBLIC_OPERATIONS_ENABLED)", flags_ts)
        self.assertIn("truthy(process.env.NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED)", flags_ts)


class ReadinessChecklistTests(unittest.TestCase):
    def setUp(self):
        self.cl = _load(CHECKLIST_JSON)

    def test_wp6_not_authorised_or_complete(self):
        self.assertFalse(self.cl["wp6"]["authorised"], "WP6 must not be marked authorised")
        self.assertFalse(self.cl["wp6"]["complete"], "WP6 must not be marked complete")

    def test_trusted_beta_not_armed(self):
        self.assertFalse(self.cl["trusted_beta"]["armed"], "Trusted-Beta must not be armed")
        self.assertFalse(self.cl["trusted_beta"]["invitations_open"], "invitations must not be open")

    def test_every_checklist_item_has_owner_evidence_and_pass_rule(self):
        items = self.cl["prearming_checklist"]
        self.assertTrue(items, "prearming_checklist is empty")
        for it in items:
            missing = CHECKLIST_ITEM_FIELDS - set(it)
            self.assertFalse(missing, f"checklist item {it.get('id')} missing fields: {sorted(missing)}")
            for f in ("owner", "evidence_required", "pass_rule"):
                self.assertTrue(str(it[f]).strip(), f"checklist item {it['id']} has empty {f}")
            self.assertIsInstance(it["blocks_wp6"], bool)
            self.assertIsInstance(it["blocks_arming"], bool)

    def test_no_readiness_item_marked_complete(self):
        for it in self.cl["prearming_checklist"]:
            self.assertNotEqual(
                it["status"].upper(), "PASS",
                f"checklist item {it['id']} is marked PASS — nothing may be certified complete in the repo",
            )

    def test_every_arming_step_has_a_rollback_and_stop_condition(self):
        steps = self.cl["arming_sequence"]
        self.assertTrue(steps, "arming_sequence is empty")
        for st in steps:
            missing = ARMING_STEP_FIELDS - set(st)
            self.assertFalse(missing, f"arming step {st.get('name')} missing fields: {sorted(missing)}")
            self.assertTrue(str(st["rollback"]).strip(), f"arming step {st['name']} has no rollback")
            self.assertTrue(str(st["stop_condition"]).strip(), f"arming step {st['name']} has no stop condition")
        orders = [s["order"] for s in steps]
        self.assertEqual(orders, sorted(orders), "arming_sequence is not in ascending order")

    def test_execution_gate_step_requires_wp6(self):
        gate = [s for s in self.cl["arming_sequence"] if "EXECUTION" in s["name"].upper()]
        self.assertTrue(gate, "no execution enforcement arming step found")
        self.assertTrue(all(s.get("wp6_required") for s in gate),
                        "execution enforcement arming must require WP6")

    def test_all_required_partial_states_represented(self):
        present = {p["id"] for p in self.cl["partial_arming_states"]}
        missing = REQUIRED_PARTIAL_STATES - present
        self.assertFalse(missing, f"partial-arming states not represented: {sorted(missing)}")
        for p in self.cl["partial_arming_states"]:
            miss = PARTIAL_STATE_FIELDS - set(p)
            self.assertFalse(miss, f"partial state {p.get('id')} missing fields: {sorted(miss)}")
            self.assertIsInstance(p["new_exposure_possible"], bool)


class RequiredDocumentsTests(unittest.TestCase):
    def test_all_required_documents_exist(self):
        for name in REQUIRED_DOCS:
            self.assertTrue((DOCS_DIR / name).exists(), f"required document missing: {name}")

    def test_checklist_required_documents_match_and_exist(self):
        cl = _load(CHECKLIST_JSON)
        listed = cl["required_documents"]
        self.assertEqual(sorted(listed), sorted(REQUIRED_DOCS),
                         "readiness-checklist.required_documents drifted from the canonical set")
        for name in listed:
            self.assertTrue((DOCS_DIR / name).exists(), f"listed required document missing: {name}")


class NoSecretsInArtefactsTests(unittest.TestCase):
    def test_json_artefacts_have_no_secret_like_values(self):
        for path in (FLAGS_JSON, CHECKLIST_JSON):
            text = path.read_text(encoding="utf-8")
            for pat in SECRET_PATTERNS:
                m = pat.search(text)
                self.assertIsNone(m, f"secret-like value in {path.name}: {pat.pattern}")


if __name__ == "__main__":
    unittest.main()
