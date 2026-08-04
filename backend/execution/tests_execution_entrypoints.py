"""WP1B/WP2 Workstream E (ADR-0029) — execution entry-point inventory DRIFT GUARD.

Fails CI when: a route is UNKNOWN or FIX_REQUIRED; a NEW backend MODULE begins creating ExecutionJobs
without a matching inventory entry; the inventory's exposure-opening job types disagree with
execution.models.BROKER_GATE_BLOCKED_JOB_TYPES; or an opens_exposure route records no enforcement.

Granularity note (honest scope): this guard detects drift at the FILE level — a new creation site inside
an already-inventoried module is not individually surfaced. The SAFETY net does not rely on this test: the
unconditional model-layer gate (ExecutionJob.save -> require_execution_gate + require_not_broker_paused for
BROKER_GATE_BLOCKED_JOB_TYPES, independent of call site) is proven separately in tests_wse.py.
"""
import json
import re
from pathlib import Path

from django.test import SimpleTestCase

from execution.models import BROKER_GATE_BLOCKED_JOB_TYPES

_BACKEND = Path(__file__).resolve().parent.parent
_ARTEFACT = Path(__file__).resolve().parent / "execution_entrypoints.json"
_CREATE_RE = re.compile(
    r"ExecutionJob\.objects\.(?:create|bulk_create|get_or_create|update_or_create)\s*\(")


def _is_test_or_migration(rel: str) -> bool:
    parts = rel.split("/")
    base = parts[-1]
    return (base in ("tests.py", "test.py", "conftest.py")
            or base.startswith(("test_", "tests_"))
            or "tests" in parts[:-1] or "migrations" in parts[:-1])


class ExecutionEntrypointInventoryTests(SimpleTestCase):
    def setUp(self):
        self.doc = json.loads(_ARTEFACT.read_text(encoding="utf-8"))
        self.routes = self.doc["routes"]
        self.files = {r["file"] for r in self.routes}

    def test_no_unknown_or_fix_required(self):
        bad = [(r["file"], r["function"], r["classification"]) for r in self.routes
               if r["classification"] in ("UNKNOWN", "FIX_REQUIRED")]
        self.assertEqual(bad, [], f"routes must not be UNKNOWN/FIX_REQUIRED at merge: {bad}")

    def test_classifications_are_valid(self):
        allowed = set(self.doc["_meta"]["classifications"])
        bad = [(r["file"], r["classification"]) for r in self.routes
               if r["classification"] not in allowed]
        self.assertEqual(bad, [], f"invalid classification(s): {bad}")

    def test_exposure_opening_job_types_match_code(self):
        inv = set(self.doc["_meta"]["exposure_opening_job_types"])
        code = {str(t) for t in BROKER_GATE_BLOCKED_JOB_TYPES}
        self.assertEqual(
            inv, code, "inventory exposure_opening_job_types disagree with BROKER_GATE_BLOCKED_JOB_TYPES")

    def test_opens_exposure_routes_record_enforcement(self):
        bad = [r["file"] for r in self.routes
               if r.get("opens_exposure") and not (r.get("enforcement") or "").strip()]
        self.assertEqual(bad, [], f"opens_exposure routes with no enforcement recorded: {bad}")

    def test_every_backend_creation_file_is_inventoried(self):
        # DRIFT GUARD (file granularity): a backend module that creates ExecutionJobs but is NOT
        # represented in the inventory fails CI. Scans backend/**/*.py (excl tests/migrations). The
        # execution-safety net is the unconditional ExecutionJob.save gate (proven in tests_wse.py), not
        # this governance guard.
        found = set()
        scanned = 0
        for path in _BACKEND.rglob("*.py"):
            rel = path.relative_to(_BACKEND).as_posix()
            if _is_test_or_migration(rel):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            scanned += 1
            if _CREATE_RE.search(text):
                found.add(rel)
        # POSITIVE CONTROL — prove the scan works before trusting a clean result.
        self.assertGreater(scanned, 200, "creation-site scan walked too few files — the walk is broken")
        self.assertIn("execution/services.py", found,
                      "positive control failed: a known ExecutionJob creation site was not detected")
        missing = sorted(found - self.files)
        self.assertEqual(
            missing, [],
            f"un-inventoried ExecutionJob creation site(s) — add to execution_entrypoints.json: {missing}")
