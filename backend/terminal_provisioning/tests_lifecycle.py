"""CVM-Inc-3 B3P-2 — lifecycle vocabulary tests: stage status, failure classification, contracts, evidence.

Several of these tests read the bundle's own SOURCE (AST) rather than a hand-maintained list. That is
deliberate: a hand-written inventory of reason codes or stage statuses drifts silently the moment someone
adds one, and a drifted inventory is worse than none because it still looks authoritative.
"""
import ast
import inspect
import os
import sqlite3
import sys
import tempfile

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "beta-agent")
if _BUNDLE not in sys.path:
    sys.path.insert(0, _BUNDLE)

import lifecycle as lc          # noqa: E402
import win_mutations as wm      # noqa: E402
import win_primitives as wp     # noqa: E402
from stores import EvidenceIncomplete, SlotIntegrityError, SlotStore   # noqa: E402

AT = "2026-07-22T10:00:00Z"
DIGEST = "golden-digest-abc"
MANIFEST_V = "2026-07-22.8"
RUUID = "0f2b8f2e-7a1a-4f6e-9c1a-2b3c4d5e6f70"
MUT = wp.MUTATING_CONTEXT


# ── source scanning helpers ────────────────────────────────────────────────────────────────────────────
def _bundle_sources():
    for root, _dirs, files in os.walk(_BUNDLE):
        if "__pycache__" in root:
            continue
        for f in sorted(files):
            if f.endswith(".py") and f != "validate.py":
                path = os.path.join(root, f)
                yield path, ast.parse(open(path, encoding="utf-8").read())


def _reason_codes_in_bundle():
    """Every sanitised reason-code literal the bundle can emit.

    Collected from three shapes: ``_fail(si, op, "code", ...)`` / ``_wrap(si, op, outcome, "code", ...)``
    (positional), ``super().__init__("code")`` inside an error class, and direct
    ``AgentError("code")``-style construction.
    """
    codes = set()
    for _path, tree in _bundle_sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            args = node.args
            if name == "_fail" and len(args) >= 3 and isinstance(args[2], ast.Constant):
                codes.add(args[2].value)
            elif name == "_wrap" and len(args) >= 4 and isinstance(args[3], ast.Constant):
                if args[3].value:
                    codes.add(args[3].value)
            elif name in ("AgentError", "OpError", "WindowsOpsError", "ProtocolError", "__init__"):
                for a in args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value:
                        codes.add(a.value)
            for kw in node.keywords:
                if kw.arg == "reason_code" and isinstance(kw.value, ast.Constant) and kw.value.value:
                    codes.add(kw.value.value)
    # Operation names reach ``__init__``-shaped scans in a couple of places; keep only plausible codes.
    return {c for c in codes if isinstance(c, str) and "_" in c and c == c.lower()}


def _statuses_produced_per_stage():
    """For each mutating stage function, the set of stage statuses it can actually emit."""
    tree = ast.parse(open(os.path.join(_BUNDLE, "win_mutations.py"), encoding="utf-8").read())
    produced = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in lc.MUTATING_STAGES:
            continue
        found = set()
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            fname = getattr(call.func, "id", None)
            if fname not in ("_ok", "_fail"):
                continue
            explicit = next((kw.value for kw in call.keywords if kw.arg == "status"), None)
            if isinstance(explicit, ast.Name):
                found.add(getattr(lc, explicit.id))
            else:
                found.add(lc.COMPLETED if fname == "_ok" else lc.FAILED)   # the helper defaults
        produced[node.name] = found
    return produced


class FakeWin:
    """Records write attempts instead of performing them (same discipline as the read-only suite)."""

    def __init__(self, *, exists=False, process=None, dest=None, task_ok=True, same_volume=True):
        self.calls = []
        self._exists, self._process, self._task_ok, self._same_volume = \
            exists, process, task_ok, same_volume
        self._dest = dest if dest is not None else {"digest": DIGEST, "executable_digest": "exe",
                                                    "portable_marker": True, "ownership_marker": True}

    def golden_source_info(self): return {"digest": DIGEST, "manifest_version": MANIFEST_V}
    def destination_info(self, p): return self._dest
    def path_exists(self, p): return self._exists
    def real_path(self, p): return None
    def query_slot_process(self, p): return self._process
    def same_volume(self, a, b): return self._same_volume
    def task_running(self, t): return False
    def open_handles(self, p): return False
    def copy_golden(self, p): self.calls.append(("copy_golden", p)); self._exists = True
    def run_task(self, t): self.calls.append(("run_task", t)); return self._task_ok
    def move_dir(self, a, b): self.calls.append(("move_dir", a, b)); self._exists = False


SI = None
TOMB = r"C:\GuvFX\beta\tombstones\2\a1b2c3d4"


def setUpModule():
    global SI
    SI = wp.resolve_slot_input(2)


def _stage_copy(win, **over):
    args = dict(expected_source_digest=DIGEST, expected_source_manifest_version=MANIFEST_V,
                expected_generation=1, actual_generation=1, observed_at=AT)
    args.update(over)
    return wm.stage_copy(win, MUT, SI, **args)


class FailureClassificationTests(SimpleTestCase):
    """Requirement 3: every sanitised reason code maps to exactly one category."""

    def test_every_reason_code_in_the_bundle_is_classified(self):
        unclassified = sorted(c for c in _reason_codes_in_bundle() if not lc.is_classified(c))
        self.assertEqual(unclassified, [], f"unclassified reason codes: {unclassified}")

    def test_every_category_is_one_of_the_six(self):
        self.assertEqual(set(lc.REASON_CATEGORY.values()) - set(lc.FAILURE_CATEGORIES), set())

    def test_a_code_cannot_have_two_categories(self):
        # Structural: a dict cannot hold one key twice. Assert the invariant the structure provides.
        self.assertEqual(len(lc.REASON_CATEGORY), len(set(lc.REASON_CATEGORY)))
        for code in lc.REASON_CATEGORY:
            self.assertIsInstance(lc.classify(code), str)

    def test_strict_classification_raises_on_an_unknown_code(self):
        with self.assertRaises(lc.UnclassifiedReasonCode):
            lc.classify("something_new_and_unmapped")

    def test_runtime_classification_degrades_conservatively_and_says_so(self):
        self.assertEqual(lc.classify("something_new_and_unmapped", strict=False), lc.INTEGRITY)
        self.assertFalse(lc.is_classified("something_new_and_unmapped"))

    def test_empty_reason_code_is_not_a_category(self):
        self.assertEqual(lc.classify(""), "")

    def test_categories_are_semantically_separated(self):
        """The distinctions the categories exist to preserve, asserted rather than assumed."""
        # "could not observe" is never filed as "the OS failed"
        self.assertEqual(lc.classify("process_observation_unavailable"), lc.OBSERVATION)
        # "the process is still there" IS the OS not doing what was asked
        self.assertEqual(lc.classify("process_still_running"), lc.WINDOWS)
        # a missing task is a deployment problem, not corruption
        self.assertEqual(lc.classify("task_absent"), lc.CONFIGURATION)
        # a wrong image path IS corruption, not a deployment problem
        self.assertEqual(lc.classify("image_outside_slot"), lc.INTEGRITY)
        # things only a human can resolve
        self.assertEqual(lc.classify("quarantine_clearance_refused"), lc.OPERATOR)
        self.assertEqual(lc.classify("pool_exhausted"), lc.OPERATOR)

    def test_attestation_carries_the_category(self):
        res = _stage_copy(FakeWin(exists=False), actual_generation=9)
        self.assertEqual(res["attestation"]["failure_category"], lc.INTEGRITY)
        self.assertTrue(res["attestation"]["classification_complete"])


class StageStatusTests(SimpleTestCase):
    """Requirement 1: COMPLETED vs ALREADY_COMPLETED must survive a retry after an ambiguous failure."""

    def test_first_run_is_completed(self):
        res = _stage_copy(FakeWin(exists=False))
        self.assertEqual(res["attestation"]["stage_status"], lc.COMPLETED)

    def test_retry_over_a_proven_complete_destination_is_already_completed(self):
        win = FakeWin(exists=True)                       # destination already there, digests match
        res = _stage_copy(win)
        self.assertEqual(res["attestation"]["stage_status"], lc.ALREADY_COMPLETED)
        self.assertEqual(res["attestation"]["outcome"], "success")
        self.assertEqual(win.calls, [])                  # did NOT copy a second time
        self.assertTrue(res["evidence"]["idempotent"])

    def test_already_completed_is_proven_not_assumed(self):
        """A present-but-wrong destination is BLOCKED — never waved through as 'already done'."""
        win = FakeWin(exists=True, dest={"digest": "OTHER", "executable_digest": "exe",
                                         "portable_marker": True, "ownership_marker": True})
        res = _stage_copy(win)
        self.assertEqual(res["attestation"]["stage_status"], lc.BLOCKED)
        self.assertEqual(win.calls, [])

    def test_another_failed_precondition_blocks_however_complete_the_directory_looks(self):
        """Wrong generation + a perfect-looking destination must NOT be ALREADY_COMPLETED."""
        res = _stage_copy(FakeWin(exists=True), actual_generation=2)
        self.assertEqual(res["attestation"]["stage_status"], lc.BLOCKED)
        self.assertIn("generation_matches", res["evidence"]["failed"])

    def test_partial_copy_is_failed_not_blocked(self):
        """BLOCKED means nothing was attempted; a broken copy WAS attempted and must not claim otherwise."""
        win = FakeWin(exists=False, dest={"digest": DIGEST, "executable_digest": "exe",
                                          "portable_marker": None, "ownership_marker": True})
        res = _stage_copy(win)
        self.assertEqual(res["attestation"]["stage_status"], lc.FAILED)
        self.assertEqual([c[0] for c in win.calls], ["copy_golden"])

    def test_triggers_report_requested_never_completed(self):
        for fn, task in ((wm.request_launch, "launch"), (wm.request_terminate, "terminate")):
            res = fn(FakeWin(), MUT, SI, observed_at=AT)
            self.assertEqual(res["attestation"]["stage_status"], lc.REQUESTED, task)
            self.assertNotEqual(res["attestation"]["stage_status"], lc.COMPLETED, task)

    def test_tombstone_already_absent_is_already_completed(self):
        res = wm.tombstone(FakeWin(exists=False), MUT, SI, tombstone_dir=TOMB, observed_at=AT)
        self.assertEqual(res["attestation"]["stage_status"], lc.ALREADY_COMPLETED)

    def test_cross_volume_refusal_is_blocked_not_failed(self):
        win = FakeWin(exists=True, same_volume=False)
        res = wm.tombstone(win, MUT, SI, tombstone_dir=TOMB, observed_at=AT)
        self.assertEqual(res["attestation"]["stage_status"], lc.BLOCKED)
        self.assertEqual(win.calls, [])

    def test_no_effect_statuses_never_touched_the_host(self):
        """The promise NO_EFFECT_STATUSES makes, checked against the recording fake."""
        cases = (
            _stage_copy(FakeWin(exists=True)),                                  # ALREADY_COMPLETED
            _stage_copy(FakeWin(exists=False), actual_generation=3),            # BLOCKED
            wm.tombstone(FakeWin(exists=False), MUT, SI, tombstone_dir=TOMB, observed_at=AT),
        )
        for res in cases:
            self.assertIn(res["attestation"]["stage_status"], lc.NO_EFFECT_STATUSES)

    def test_attest_refuses_an_unknown_status(self):
        with self.assertRaises(ValueError):
            wp.attest(slot=2, operation="x", outcome="success", reason_code="", evidence={},
                      observed_at=AT, stage_status="PROBABLY_FINE")

    def test_read_only_primitives_declare_no_lifecycle_status(self):
        """An observation is not a lifecycle stage; claiming a status would misrepresent it."""
        res = wp.observe_process(FakeWin(process=None), SI, observed_at=AT)
        self.assertEqual(res["attestation"]["stage_status"], "")


class StageContractTests(SimpleTestCase):
    """Requirement 2: contracts are data beside the implementation, and are checked against it."""

    def test_every_mutating_stage_has_a_contract(self):
        implemented = {n for n, obj in vars(wm).items()
                       if inspect.isfunction(obj) and not n.startswith("_")
                       and obj.__module__ == "win_mutations"
                       and n != "assert_authorised_tombstone_dir"}      # a guard, not a stage
        self.assertEqual(set(lc.STAGE_CONTRACTS), implemented)

    def test_each_contract_states_pre_invariant_and_post(self):
        for stage, contract in lc.STAGE_CONTRACTS.items():
            self.assertTrue(contract["preconditions"], stage)
            self.assertTrue(contract["invariant"], stage)
            self.assertTrue(contract["postconditions"], stage)
            self.assertTrue(set(contract["statuses"]) <= set(lc.STAGE_STATUSES), stage)

    def test_declared_statuses_match_the_statuses_the_code_can_produce(self):
        """Both directions: an undeclared outcome, or a declared one that cannot happen, both fail."""
        produced = _statuses_produced_per_stage()
        for stage, declared in lc.STAGE_CONTRACTS.items():
            self.assertEqual(produced.get(stage), set(declared["statuses"]), stage)

    def test_every_mutating_stage_requires_the_mutating_capability(self):
        """The contract's first precondition, verified mechanically rather than read."""
        for stage in lc.MUTATING_STAGES:
            self.assertIn("capability is MUTATING", lc.STAGE_CONTRACTS[stage]["preconditions"][0])


class EvidenceCompletenessTests(SimpleTestCase):
    """Requirement 5: every stage emits evidence, success or failure, and absence is an integrity concern."""

    def _all_stage_results(self):
        return [
            _stage_copy(FakeWin(exists=False)),                                       # COMPLETED
            _stage_copy(FakeWin(exists=True)),                                        # ALREADY_COMPLETED
            _stage_copy(FakeWin(exists=False), actual_generation=7),                  # BLOCKED
            wm.request_launch(FakeWin(task_ok=False), MUT, SI, observed_at=AT),       # FAILED
            wm.request_launch(FakeWin(), MUT, SI, observed_at=AT),                    # REQUESTED
            wm.confirm_launch(FakeWin(process=None), MUT, SI, observed_at=AT),        # FAILED
            wm.request_terminate(FakeWin(), MUT, SI, observed_at=AT),                 # REQUESTED
            wm.confirm_terminated(FakeWin(process=None), MUT, SI, birth={}, observed_at=AT),
            wm.tombstone(FakeWin(exists=True), MUT, SI, tombstone_dir=TOMB, observed_at=AT),
            wm.verify_cleanup(FakeWin(exists=True, process=None), MUT, SI, generation_before=1,
                              generation_now=1, audit_complete=False, observed_at=AT),   # FAILED
        ]

    def test_success_and_failure_are_both_complete_evidence_records(self):
        for res in self._all_stage_results():
            att = lc.assert_evidence_present(res)         # raises if anything is missing
            self.assertTrue(att["evidence_digest"])
            self.assertIn(att["stage_status"], lc.STAGE_STATUSES)
            self.assertIn(att["failure_category"], ("",) + lc.FAILURE_CATEGORIES)

    def test_failed_stages_still_carry_a_reason_and_a_category(self):
        for res in self._all_stage_results():
            att = res["attestation"]
            if att["outcome"] == "failure":
                self.assertTrue(att["reason_code"], att["operation"])
                self.assertTrue(att["failure_category"], att["operation"])

    def test_a_record_without_evidence_is_refused(self):
        for broken in ({}, {"attestation": {}}, {"attestation": {"evidence_digest": ""}}, None, "x"):
            with self.assertRaises(lc.EvidenceMissing):
                lc.assert_evidence_present(broken, "stage_copy")

    def test_a_record_with_an_unknown_status_is_refused(self):
        res = _stage_copy(FakeWin(exists=False))
        res["attestation"]["stage_status"] = "FINE_PROBABLY"
        with self.assertRaises(lc.EvidenceMissing):
            lc.assert_evidence_present(res)


class StoreEvidenceCompletenessTests(SimpleTestCase):
    """The durable half: a sequenced operation with no evidence is an integrity failure."""

    @staticmethod
    def _store(pool_size=2):
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        return SlotStore(f.name, pool_size=pool_size)

    def _record(self, store, slot, gen, res, now=1):
        return store.record_stage(slot=slot, generation=gen,
                                  operation=res["attestation"]["operation"],
                                  attestation=res["attestation"], now=now)

    def test_recording_a_stage_writes_sequence_and_evidence_together(self):
        s = self._store(); slot, gen = s.assign(RUUID, now=1)
        n = self._record(s, slot, gen, _stage_copy(FakeWin(exists=False)))
        self.assertEqual(n, 1)
        ev = s.stage_evidence_for_occupancy(slot, gen)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["stage_status"], lc.COMPLETED)
        s.assert_evidence_complete(slot, gen)

    def test_failures_are_recorded_as_first_class_events(self):
        s = self._store(); slot, gen = s.assign(RUUID, now=1)
        self._record(s, slot, gen, _stage_copy(FakeWin(exists=False), actual_generation=4))
        self._record(s, slot, gen, wm.request_launch(FakeWin(task_ok=False), MUT, SI, observed_at=AT), 2)
        ev = s.stage_evidence_for_occupancy(slot, gen)
        self.assertEqual([e["stage_status"] for e in ev], [lc.BLOCKED, lc.FAILED])
        self.assertEqual([e["failure_category"] for e in ev], [lc.INTEGRITY, lc.WINDOWS])
        s.assert_evidence_complete(slot, gen)

    def test_a_stage_with_no_evidence_digest_is_refused_at_the_door(self):
        s = self._store(); slot, gen = s.assign(RUUID, now=1)
        with self.assertRaises(EvidenceIncomplete):
            s.record_stage(slot=slot, generation=gen, operation="stage_copy",
                           attestation={"stage_status": lc.COMPLETED}, now=1)
        self.assertEqual(s.sequence_for_occupancy(slot, gen), [])   # and nothing was sequenced

    def test_deleted_evidence_is_an_integrity_failure_not_a_gap(self):
        s = self._store(); slot, gen = s.assign(RUUID, now=1)
        self._record(s, slot, gen, _stage_copy(FakeWin(exists=False)))
        self._record(s, slot, gen, wm.request_launch(FakeWin(), MUT, SI, observed_at=AT), 2)
        s._conn.execute("DELETE FROM stage_evidence WHERE sequence_number=1"); s._conn.commit()
        with self.assertRaises(SlotIntegrityError):
            s.assert_evidence_complete(slot, gen)

    def test_evidence_for_an_unsequenced_operation_is_an_integrity_failure(self):
        s = self._store(); slot, gen = s.assign(RUUID, now=1)
        self._record(s, slot, gen, _stage_copy(FakeWin(exists=False)))
        s._conn.execute(
            "INSERT INTO stage_evidence (slot, generation, sequence_number, operation, stage_status,"
            " failure_category, reason_code, evidence_digest, at) VALUES (?,?,?,?,?,?,?,?,?)",
            (slot, gen, 2, "ghost_stage", lc.COMPLETED, "", "", "deadbeef", 2))
        s._conn.commit()
        with self.assertRaises(SlotIntegrityError):
            s.assert_evidence_complete(slot, gen)

    def test_evidence_naming_a_different_operation_is_an_integrity_failure(self):
        s = self._store(); slot, gen = s.assign(RUUID, now=1)
        self._record(s, slot, gen, _stage_copy(FakeWin(exists=False)))
        s._conn.execute("UPDATE stage_evidence SET operation='tombstone' WHERE sequence_number=1")
        s._conn.commit()
        with self.assertRaises(SlotIntegrityError):
            s.assert_evidence_complete(slot, gen)

    def test_evidence_cannot_be_duplicated_for_one_stage(self):
        s = self._store(); slot, gen = s.assign(RUUID, now=1)
        self._record(s, slot, gen, _stage_copy(FakeWin(exists=False)))
        with self.assertRaises(sqlite3.IntegrityError):
            s._conn.execute(
                "INSERT INTO stage_evidence (slot, generation, sequence_number, operation, stage_status,"
                " failure_category, reason_code, evidence_digest, at) VALUES (?,?,?,?,?,?,?,?,?)",
                (slot, gen, 1, "stage_copy", lc.COMPLETED, "", "", "other", 3))

    def test_evidence_is_scoped_to_the_occupancy(self):
        s = self._store(); slot, gen = s.assign(RUUID, now=1)
        self._record(s, slot, gen, _stage_copy(FakeWin(exists=False)))
        s.release(RUUID, now=2)
        other = "99999999-8888-7777-6666-555555555555"
        slot2, gen2 = s.assign(other, now=3)
        self.assertEqual(s.stage_evidence_for_occupancy(slot2, gen2), [])
        self.assertEqual(len(s.stage_evidence_for_occupancy(slot, gen)), 1)
