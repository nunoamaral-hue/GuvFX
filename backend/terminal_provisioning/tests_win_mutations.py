"""CVM-Inc-3 B3P-2 — MUTATING Windows primitive tests (stages 4-9).

Proves: capability enforcement; stage-copy integrity on both sides with no partial copy reaching launch;
launch as two separate records (REQUESTED != started); STOP success meaning process ABSENT; tombstone as a
move; and cleanup/rollback producing evidence either way.
"""
import os
import sys

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "beta-agent")
if _BUNDLE not in sys.path:
    sys.path.insert(0, _BUNDLE)

import win_mutations as wm      # noqa: E402
import win_primitives as wp     # noqa: E402

AT = "2026-07-22T10:00:00Z"
FILETIME = 133_000_000_000_000_000
DIGEST = "golden-digest-abc"
MANIFEST_V = "2026-07-22.8"


class FakeWin:
    def __init__(self, *, exists=False, process=None, source=None, dest=None, task_ok=True,
                 same_volume=True, task_running=False, handles=False):
        self.calls = []
        self._exists, self._process = exists, process
        self._source = source if source is not None else {"digest": DIGEST,
                                                          "manifest_version": MANIFEST_V}
        self._dest = dest if dest is not None else {"digest": DIGEST, "executable_digest": "exe",
                                                    "portable_marker": True, "ownership_marker": True}
        self._task_ok, self._same_volume = task_ok, same_volume
        self._marker = None
        self._task_running, self._handles = task_running, handles

    def golden_source_info(self): return self._source
    def destination_info(self, p): return self._dest
    def path_exists(self, p): return self._exists
    def real_path(self, p): return p          # provisioned slot dir, no reparse point
    def query_slot_process(self, p, identity=""): return self._process
    def same_volume(self, a, b): return self._same_volume
    def task_running(self, t): return self._task_running
    def open_handles(self, p): return self._handles

    def copy_golden(self, p):
        self.calls.append(("copy_golden", p)); self._exists = True

    def write_owner_tag(self, p, raw):
        self.calls.append(("write_owner_tag", p)); self._marker = raw

    def run_task(self, t):
        self.calls.append(("run_task", t)); return self._task_ok

    def move_dir(self, a, b):
        self.calls.append(("move_dir", a, b)); self._exists = False


def _proc(**over):
    d = dict(pid=13020, created_at_filetime=FILETIME,
             image=r"C:\GuvFX\beta\slots\2\terminal\terminal64.exe", image_digest="img",
             user_sid="S-1-5-21-x-1002", session_id=1)
    d.update(over)
    return d


SI = None
MUT = wp.MUTATING_CONTEXT
TOMB = r"C:\GuvFX\beta\tombstones\2\a1b2c3d4"
MARKER = '{"generation": 1, "runtime_uuid": "u", "slot": 2}'    # opaque to the stage


def setUpModule():
    global SI
    SI = wp.resolve_slot_input(2)


class CapabilityEnforcementTests(SimpleTestCase):
    """Requirement 1: mutating helpers refuse a READ_ONLY context."""

    def test_every_mutating_primitive_refuses_read_only(self):
        ro = wp.READ_ONLY_CONTEXT
        win = FakeWin()
        cases = (
            ("stage_copy", lambda: wm.stage_copy(win, ro, SI, expected_source_digest=DIGEST,
                                                 expected_source_manifest_version=MANIFEST_V,
                                                 expected_generation=1, actual_generation=1,
                                                 owner_marker=MARKER)),
            ("request_launch", lambda: wm.request_launch(win, ro, SI)),
            ("confirm_launch", lambda: wm.confirm_launch(win, ro, SI)),
            ("request_terminate", lambda: wm.request_terminate(win, ro, SI)),
            ("confirm_terminated", lambda: wm.confirm_terminated(win, ro, SI, birth={})),
            ("tombstone", lambda: wm.tombstone(win, ro, SI, tombstone_dir=TOMB)),
            ("verify_cleanup", lambda: wm.verify_cleanup(win, ro, SI, generation_before=1,
                                                         generation_now=1, audit_complete=True)),
        )
        for name, call in cases:
            with self.assertRaises(wp.CapabilityViolation, msg=name) as ctx:
                call()
            self.assertEqual(ctx.exception.reason_code, "capability_violation")
        self.assertEqual(win.calls, [])          # nothing happened on the host

    def test_unknown_capability_rejected(self):
        with self.assertRaises(ValueError):
            wp.PrimitiveContext("sort_of_mutating")

    def test_read_only_primitives_run_without_mutating_capability(self):
        wp.observe_process(FakeWin(process=None), SI, observed_at=AT)   # no raise


class StageCopyIntegrityTests(SimpleTestCase):
    """Requirement 3: integrity proven before AND after; no partial copy reaches launch."""

    def _copy(self, win, **over):
        args = dict(expected_source_digest=DIGEST, expected_source_manifest_version=MANIFEST_V,
                    expected_generation=1, actual_generation=1, owner_marker=MARKER, observed_at=AT)
        args.update(over)
        return wm.stage_copy(win, MUT, SI, **args)

    def test_happy_path_runs_all_checks(self):
        win = FakeWin(exists=False)
        res = self._copy(win)
        self.assertEqual(res["attestation"]["outcome"], "success")
        for k in wm.STAGE_PRE_CHECKS:
            self.assertTrue(res["evidence"]["pre_checks"][k], k)
        for k in wm.STAGE_POST_CHECKS:
            self.assertTrue(res["evidence"]["post_checks"][k], k)

    def test_wrong_source_digest_refuses_before_copying(self):
        win = FakeWin(exists=False, source={"digest": "WRONG", "manifest_version": MANIFEST_V})
        res = self._copy(win)
        self.assertEqual(res["attestation"]["reason_code"], "stage_copy_precheck_failed")
        self.assertEqual(win.calls, [])                       # never copied

    def test_wrong_manifest_version_refuses(self):
        win = FakeWin(exists=False, source={"digest": DIGEST, "manifest_version": "old"})
        self.assertIn("source_manifest_version_matches", self._copy(win)["evidence"]["failed"])

    def test_existing_destination_is_never_copied_over(self):
        """Whatever the verdict, an existing destination is not overwritten by a second copy.

        Since the idempotency-evidence requirement landed, the VERDICT depends on what is actually there:
        a destination proven complete is ALREADY_COMPLETED (see ``tests_lifecycle``), and one that is not
        is BLOCKED with ``destination_absent`` recorded as the failed precondition. Both refuse to copy.
        """
        incomplete = {"digest": "OTHER", "executable_digest": None,
                      "portable_marker": False, "ownership_marker": False}
        win = FakeWin(exists=True, dest=incomplete)
        self.assertIn("destination_absent", self._copy(win)["evidence"]["failed"])
        self.assertEqual(win.calls, [])
        complete = FakeWin(exists=True)
        self.assertEqual(self._copy(complete)["attestation"]["outcome"], "success")
        self.assertEqual(complete.calls, [])

    def test_generation_mismatch_refuses(self):
        win = FakeWin(exists=False)
        res = self._copy(win, actual_generation=2)
        self.assertIn("generation_matches", res["evidence"]["failed"])
        self.assertEqual(win.calls, [])

    def test_partial_copy_never_reaches_launch(self):
        """Post-check failure must be a refusal, not a 'probably fine' directory."""
        for missing in ("digest", "executable_digest", "portable_marker", "ownership_marker"):
            dest = {"digest": DIGEST, "executable_digest": "exe", "portable_marker": True,
                    "ownership_marker": True}
            dest[missing] = None if missing != "digest" else "MISMATCH"
            res = self._copy(FakeWin(exists=False, dest=dest))
            self.assertEqual(res["attestation"]["outcome"], "failure", missing)
            self.assertEqual(res["attestation"]["reason_code"], "stage_copy_incomplete")


class LaunchAttestationTests(SimpleTestCase):
    """Requirement 4: REQUESTED and OBSERVED are separate; a trigger is not a started MT5."""

    def test_requested_record_reports_no_process_facts(self):
        res = wm.request_launch(FakeWin(), MUT, SI, observed_at=AT)
        self.assertEqual(res["evidence"]["phase"], "REQUESTED")
        self.assertTrue(res["evidence"]["trigger_accepted"])
        for leaked in ("pid", "birth", "created_at_filetime"):
            self.assertNotIn(leaked, res["evidence"], leaked)

    def test_trigger_accepted_but_no_process_does_not_complete_launch(self):
        """THE case: task ran, MT5 never appeared."""
        win = FakeWin(process=None)
        self.assertEqual(wm.request_launch(win, MUT, SI, observed_at=AT)["attestation"]["outcome"],
                         "success")
        res = wm.confirm_launch(win, MUT, SI, observed_at=AT)
        self.assertEqual(res["attestation"]["outcome"], "failure")
        self.assertEqual(res["attestation"]["reason_code"], "process_absent")

    def test_observed_record_carries_birth_evidence(self):
        res = wm.confirm_launch(FakeWin(process=_proc()), MUT, SI, observed_at=AT)
        self.assertEqual(res["attestation"]["outcome"], "success")
        b = res["evidence"]["birth"]
        for f in ("pid", "created_at_filetime", "image_digest",
                  "executable_containment_verified", "user_sid", "session_id", "slot"):
            self.assertIn(f, b, f)

    def test_rejected_trigger_is_a_failure(self):
        res = wm.request_launch(FakeWin(task_ok=False), MUT, SI, observed_at=AT)
        self.assertEqual(res["attestation"]["reason_code"], "launch_trigger_rejected")

    def test_uncontained_image_does_not_complete_launch(self):
        win = FakeWin(process=_proc(image=r"C:\Program Files\IS6 Technologies MT5 Terminal\terminal64.exe"))
        res = wm.confirm_launch(win, MUT, SI, observed_at=AT)
        self.assertEqual(res["attestation"]["outcome"], "failure")


class TerminationSemanticsTests(SimpleTestCase):
    """Requirement 5: STOP success means the process is ABSENT."""

    BIRTH = {"pid": 13020, "created_at_filetime": FILETIME, "image_digest": "img",
             "executable_containment_verified": True, "user_sid": "S-1-5-21-x-1002",
             "session_id": 1, "slot": 2}

    def test_trigger_success_is_not_termination_success(self):
        win = FakeWin(process=_proc())
        self.assertEqual(wm.request_terminate(win, MUT, SI, observed_at=AT)["attestation"]["outcome"],
                         "success")
        res = wm.confirm_terminated(win, MUT, SI, birth=self.BIRTH, observed_at=AT)
        self.assertEqual(res["attestation"]["outcome"], "failure")
        self.assertEqual(res["attestation"]["reason_code"], "process_still_running")
        self.assertFalse(res["evidence"]["process_absent"])

    def test_absent_process_is_success(self):
        res = wm.confirm_terminated(FakeWin(process=None), MUT, SI, birth=self.BIRTH, observed_at=AT)
        self.assertEqual(res["attestation"]["outcome"], "success")
        self.assertTrue(res["evidence"]["process_absent"])

    def test_different_process_in_slot_is_escalated_not_success(self):
        win = FakeWin(process=_proc(created_at_filetime=FILETIME + 999, pid=13020))
        res = wm.confirm_terminated(win, MUT, SI, birth=self.BIRTH, observed_at=AT)
        self.assertEqual(res["attestation"]["reason_code"], "unexpected_process_in_slot")

    def test_unobservable_process_never_claims_termination(self):
        class Blind(FakeWin):
            def query_slot_process(self, p, identity=""): raise OSError("wmi down")
        res = wm.confirm_terminated(Blind(), MUT, SI, birth=self.BIRTH, observed_at=AT)
        self.assertEqual(res["attestation"]["outcome"], "failure")
        self.assertEqual(res["attestation"]["reason_code"], "process_observation_unavailable")


class TombstoneTests(SimpleTestCase):
    def test_moves_never_deletes(self):
        win = FakeWin(exists=True)
        res = wm.tombstone(win, MUT, SI, tombstone_dir=TOMB, observed_at=AT)
        self.assertEqual(res["attestation"]["outcome"], "success")
        self.assertEqual([c[0] for c in win.calls], ["move_dir"])

    def test_cross_volume_refused(self):
        """An authorised path can still be a different volume — a mount point under the tombstone root."""
        win = FakeWin(exists=True, same_volume=False)
        res = wm.tombstone(win, MUT, SI, tombstone_dir=TOMB, observed_at=AT)
        self.assertEqual(res["attestation"]["reason_code"], "cross_volume_move_refused")
        self.assertEqual(win.calls, [])

    def test_idempotent_when_already_absent(self):
        res = wm.tombstone(FakeWin(exists=False), MUT, SI, tombstone_dir=TOMB, observed_at=AT)
        self.assertTrue(res["evidence"]["idempotent"])


class CleanupRollbackTests(SimpleTestCase):
    """Requirement 6: cleanup proves the slot is clean and emits evidence either way."""

    def _cleanup(self, win, **over):
        args = dict(generation_before=1, generation_now=1, audit_complete=True, observed_at=AT)
        args.update(over)
        return wm.verify_cleanup(win, MUT, SI, **args)

    def test_clean_slot_passes_all_proofs(self):
        res = self._cleanup(FakeWin(exists=False, process=None))
        self.assertEqual(res["attestation"]["outcome"], "success")
        for k in wm.CLEANUP_PROOFS:
            self.assertTrue(res["evidence"]["proofs"][k], k)

    def test_each_proof_blocks_individually(self):
        cases = (
            ("slot_directory_empty", FakeWin(exists=True, process=None), {}),
            ("no_runtime_process", FakeWin(exists=False, process=_proc()), {}),
            ("no_task_running", FakeWin(exists=False, process=None, task_running=True), {}),
            ("no_runtime_handles", FakeWin(exists=False, process=None, handles=True), {}),
            ("audit_complete", FakeWin(exists=False, process=None), {"audit_complete": False}),
            ("generation_unchanged", FakeWin(exists=False, process=None), {"generation_now": 2}),
        )
        for proof, win, over in cases:
            res = self._cleanup(win, **over)
            self.assertEqual(res["attestation"]["outcome"], "failure", proof)
            self.assertIn(proof, res["evidence"]["missing"], proof)

    def test_generation_must_not_advance_before_release(self):
        """Cleanup runs BEFORE release, so an advanced generation means out-of-order release."""
        res = self._cleanup(FakeWin(exists=False, process=None), generation_now=2)
        self.assertIn("generation_unchanged", res["evidence"]["missing"])

    def test_failure_still_produces_evidence(self):
        res = self._cleanup(FakeWin(exists=True, process=_proc()))
        self.assertEqual(res["attestation"]["reason_code"], "cleanup_incomplete")
        self.assertIn("proofs", res["evidence"])
        self.assertTrue(res["attestation"]["evidence_digest"])


class TombstoneDestinationContainmentTests(SimpleTestCase):
    """The tombstone destination is the one caller-supplied path in the mutating set — it is validated."""

    def test_production_destination_refused(self):
        for bad in (r"C:\GuvFX\accounts\1", r"C:\GuvFX\terminals\x",
                    r"C:\Program Files\IS6 Technologies MT5 Terminal",
                    r"C:\GuvFX\beta\tombstones\2\..\..\..\accounts",
                    r"C:\Windows\System32", r"C:\GuvFX\beta\slots\2"):
            win = FakeWin(exists=True)
            with self.assertRaises(wp.UnauthorisedNamespace, msg=bad):
                wm.tombstone(win, MUT, SI, tombstone_dir=bad, observed_at=AT)
            self.assertEqual(win.calls, [], bad)                 # refused before any host call

    def test_another_slots_tombstone_root_refused(self):
        with self.assertRaises(wp.UnauthorisedNamespace):
            wm.tombstone(FakeWin(exists=True), MUT, SI,
                         tombstone_dir=r"C:\GuvFX\beta\tombstones\3\x", observed_at=AT)

    def test_empty_destination_refused(self):
        with self.assertRaises(wp.UnauthorisedNamespace):
            wm.tombstone(FakeWin(exists=True), MUT, SI, tombstone_dir="", observed_at=AT)

    def test_validation_runs_even_when_slot_already_absent(self):
        """An idempotent no-op must not be a way to smuggle an unvalidated path past the guard."""
        with self.assertRaises(wp.UnauthorisedNamespace):
            wm.tombstone(FakeWin(exists=False), MUT, SI,
                         tombstone_dir=r"C:\GuvFX\accounts\1", observed_at=AT)


class StageCopyContainmentIsNotTautologicalTests(SimpleTestCase):
    """``destination_beneath_slot`` must be asserted against the fixed root, not the destination's parent."""

    def test_reparse_point_on_slot_directory_refuses_copy(self):
        class Reparse(FakeWin):
            def real_path(self, p): return r"C:\GuvFX\accounts\1"
        win = Reparse(exists=False)
        res = wm.stage_copy(win, MUT, SI, expected_source_digest=DIGEST,
                            expected_source_manifest_version=MANIFEST_V,
                            expected_generation=1, actual_generation=1, owner_marker=MARKER,
                            observed_at=AT)
        self.assertIn("destination_not_reparse", res["evidence"]["failed"])
        self.assertEqual(win.calls, [])

    def test_reparse_point_inside_the_beta_root_is_allowed(self):
        class Reparse(FakeWin):
            def real_path(self, p): return r"C:\GuvFX\beta\slots\2"
        res = wm.stage_copy(Reparse(exists=False), MUT, SI, expected_source_digest=DIGEST,
                            expected_source_manifest_version=MANIFEST_V,
                            expected_generation=1, actual_generation=1, owner_marker=MARKER,
                            observed_at=AT)
        self.assertEqual(res["attestation"]["outcome"], "success")


class CleanupCoversBothTasksTests(SimpleTestCase):
    def test_terminate_task_still_running_blocks_cleanup(self):
        class OnlyTerminate(FakeWin):
            def task_running(self, t): return t.endswith("Stop-2")
        res = wm.verify_cleanup(OnlyTerminate(exists=False, process=None), MUT, SI,
                                generation_before=1, generation_now=1, audit_complete=True,
                                observed_at=AT)
        self.assertEqual(res["attestation"]["outcome"], "failure")
        self.assertIn("no_task_running", res["evidence"]["missing"])


class MarkerIsWrittenByTheStageTests(SimpleTestCase):
    """The ownership marker is one of stage_copy's own post-checks, so the stage must write it.

    Before this, materialise() wrote the marker AFTER stage_copy returned - which meant a fresh
    MATERIALISE could never satisfy ownership_marker_present and always ended stage_copy_incomplete,
    leaving a fully populated marker-less slot that every later integrity gate quarantined.
    """

    def test_marker_is_written_between_copy_and_proof(self):
        win = FakeWin(exists=False)
        wm.stage_copy(win, MUT, SI, expected_source_digest=DIGEST,
                      expected_source_manifest_version=MANIFEST_V, expected_generation=1,
                      actual_generation=1, owner_marker=MARKER, observed_at=AT)
        self.assertEqual([c[0] for c in win.calls], ["copy_golden", "write_owner_tag"])

    def test_no_marker_is_written_when_a_precondition_blocks(self):
        win = FakeWin(exists=False)
        wm.stage_copy(win, MUT, SI, expected_source_digest=DIGEST,
                      expected_source_manifest_version=MANIFEST_V, expected_generation=1,
                      actual_generation=9, owner_marker=MARKER, observed_at=AT)
        self.assertEqual(win.calls, [])

    def test_a_failing_write_is_a_recorded_stage_failure_not_an_escape(self):
        class Unwritable(FakeWin):
            def write_owner_tag(self, p, raw):
                raise PermissionError("denied")
        res = wm.stage_copy(Unwritable(exists=False), MUT, SI, expected_source_digest=DIGEST,
                            expected_source_manifest_version=MANIFEST_V, expected_generation=1,
                            actual_generation=1, owner_marker=MARKER, observed_at=AT)
        self.assertEqual(res["attestation"]["reason_code"], "stage_copy_permission_denied")


class EmptyBirthTerminationTests(SimpleTestCase):
    """An unobservable pre-stop observation yields an empty birth record. It must not reach the identity
    comparison: that raised KeyError, escaped the stage entirely, and left a REQUESTED row with no
    confirmation row while the runtime kept trading."""

    def test_empty_birth_with_a_live_process_is_still_running_not_a_crash(self):
        res = wm.confirm_terminated(FakeWin(process=_proc()), MUT, SI, birth={}, observed_at=AT)
        self.assertEqual(res["attestation"]["reason_code"], "process_still_running")
        self.assertTrue(res["evidence"]["birth_evidence_missing"])

    def test_empty_birth_with_no_process_is_still_a_clean_stop(self):
        res = wm.confirm_terminated(FakeWin(process=None), MUT, SI, birth={}, observed_at=AT)
        self.assertEqual(res["attestation"]["outcome"], "success")


class TombstoneAdapterFailureTests(SimpleTestCase):
    """An adapter raising inside tombstone must become a recorded stage failure, never an escape."""

    def test_move_failure_is_recorded(self):
        class Stuck(FakeWin):
            def move_dir(self, a, b):
                raise PermissionError("sharing violation")
        res = wm.tombstone(Stuck(exists=True), MUT, SI, tombstone_dir=TOMB, observed_at=AT)
        self.assertEqual(res["attestation"]["reason_code"], "tombstone_move_permission_denied")

    def test_precheck_failure_is_blocked_not_raised(self):
        class Blind(FakeWin):
            def same_volume(self, a, b):
                raise OSError("volume identity unavailable")
        res = wm.tombstone(Blind(exists=True), MUT, SI, tombstone_dir=TOMB, observed_at=AT)
        self.assertEqual(res["attestation"]["reason_code"], "tombstone_precheck_unavailable")
