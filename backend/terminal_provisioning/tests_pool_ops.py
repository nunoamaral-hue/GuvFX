"""CVM-Inc-3 B3P-2 — slot-aware path resolution + pool-aware operation implementations.

Covers the two layers the mutating primitives sit under: the resolver that turns a runtime UUID into a
fixed slot path, and the operation implementations that sequence the stages, run the integrity gate and
record evidence.
"""
import json
import os
import sys
import tempfile

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "beta-agent")
if _BUNDLE not in sys.path:
    sys.path.insert(0, _BUNDLE)

import lifecycle as lc                                          # noqa: E402
from lib.mgmt_agent_core import (AgentError, BetaProvisioningAgent, EXECUTION_MODEL_SLOT_POOL,  # noqa: E402
                                 EXECUTION_MODEL_UUID_DIR)
from pool_op_impls import PoolOpImplementations, SlotResolver    # noqa: E402
from stores import (SlotIntegrityError, SlotStore, format_owner_marker, occupancy_id)   # noqa: E402

RUUID = "0f2b8f2e-7a1a-4f6e-9c1a-2b3c4d5e6f70"
#: The approved launch-task definition for slot 1 — the seven fields occupancy.TASK_IDENTITY_FIELDS pins.
APPROVED_TASK = {
    "task_name": "GuvFXBetaRuntime-1", "run_as_identity": "guvfx_b_slot1",
    "executable": r"C:\GuvFX\beta\slots\1\terminal\terminal64.exe",
    "working_directory": r"C:\GuvFX\beta\slots\1\terminal",
    "arguments": "/portable", "logon_type": 1, "run_level": 0, "enabled": True,
}
#: The approved TERMINATE definition for slot 1. install_pool.ps1 now pins BOTH task families, because the
#: terminate task is the one that reaches a process — its argument string is all that keeps
#: `Stop-Process -Force` off the operator's live terminal, which carries the same image name.
APPROVED_STOP = {
    "task_name": "GuvFXBetaRuntimeStop-1", "run_as_identity": "guvfx_b_slot1",
    "executable": "powershell.exe",
    "working_directory": "",
    "arguments": ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command \"Get-Process -Name "
                  "terminal64 -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq "
                  "'C:\\GuvFX\\beta\\slots\\1\\terminal\\terminal64.exe' } | Stop-Process -Force\""),
    "logon_type": 1, "run_level": 0, "enabled": True,
}
APPROVED_BOTH = {"GuvFXBetaRuntime-1": dict(APPROVED_TASK),
                 "GuvFXBetaRuntimeStop-1": dict(APPROVED_STOP)}
OTHER = "99999999-8888-7777-6666-555555555555"
DIGEST = "golden-digest-abc"
MANIFEST_V = "2026-07-22.13"
FILETIME = 133_000_000_000_000_000


class FakeWin:
    """Fake host. Records every write; serves marker/process/destination state the ops read back."""

    def __init__(self, *, exists=False, process=None, dest=None, task_ok=True, launches=True):
        self.calls = []
        self._exists, self._process, self._task_ok, self._launches = exists, process, task_ok, launches
        self._marker = None
        self._task_definition = dict(APPROVED_TASK)
        self._stop_definition = dict(APPROVED_STOP)
        self._dest = dest if dest is not None else {"digest": DIGEST, "executable_digest": "exe",
                                                    "portable_marker": True, "ownership_marker": True}

    # reads
    def golden_source_info(self): return {"digest": DIGEST, "manifest_version": MANIFEST_V}
    def destination_info(self, p):
        # The ownership marker is REAL state now, not a constant. A fake that always answered True is what
        # hid the defect where materialise could never satisfy its own post-check.
        return dict(self._dest, ownership_marker=self._marker is not None)
    def path_exists(self, p): return self._exists
    def real_path(self, p): return p          # provisioned slot dir, no reparse point
    def read_owner_tag(self, p): return self._marker
    def query_slot_process(self, p, identity=""): return self._process
    def same_volume(self, a, b): return True
    def task_running(self, t): return False
    def open_handles(self, p): return False   # a real host CANNOT answer this - see the trial note

    def query_task(self, t):
        if self._task_definition is None:
            return None
        # Answer PER TASK. Returning the launch definition for the terminate name too would let the
        # terminate gate pass against a definition that is not the terminate task's — the exact shape of
        # fake that has already hidden two real defects in this packet.
        base = self._stop_definition if str(t).startswith("GuvFXBetaRuntimeStop-") else self._task_definition
        if base is None:
            return None
        d = dict(base, task_name=t)
        d["portable_switch"] = "/portable" in str(d.get("arguments") or "").lower().split()
        return d

    # writes
    def copy_golden(self, p): self.calls.append(("copy_golden", p)); self._exists = True
    def write_owner_tag(self, p, raw): self.calls.append(("write_owner_tag", p)); self._marker = raw
    def move_dir(self, a, b): self.calls.append(("move_dir", a, b)); self._exists = False

    def run_task(self, t):
        self.calls.append(("run_task", t))
        if not self._task_ok:
            return False
        if t.startswith("GuvFXBetaRuntimeStop"):
            self._process = None                       # the stop task actually stops it
        elif self._launches:
            self._process = _proc()
        return True


def _proc(**over):
    d = dict(pid=13020, created_at_filetime=FILETIME,
             image=r"C:\GuvFX\beta\slots\1\terminal\terminal64.exe", image_digest="img",
             user_sid="S-1-5-21-x-1001", session_id=1)
    d.update(over)
    return d


def _store(pool_size=2):
    f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
    return SlotStore(f.name, pool_size=pool_size)


def _impls(win, store, **over):
    args = dict(golden_digest=DIGEST, golden_manifest_version=MANIFEST_V, now_fn=lambda: 100,
                approved_tasks={k: dict(v) for k, v in APPROVED_BOTH.items()},
                manifest_version=MANIFEST_V, sleep_fn=lambda _seconds: None)   # settle window, no wall time
    args.update(over)
    return PoolOpImplementations(win, store, **args)


def _ctx(store, runtime_uuid=RUUID, operation="MATERIALISE"):
    return SlotResolver(store, now_fn=lambda: 100).resolve(runtime_uuid=runtime_uuid,
                                                           operation=operation)


class SlotResolverTests(SimpleTestCase):
    """Step 1: the path is a function of the SLOT NUMBER — never of anything a caller supplied."""

    def test_path_is_derived_from_the_slot_not_the_uuid(self):
        s = _store()
        b = _ctx(s)
        self.assertEqual(b["slot"], 1)
        self.assertEqual(b["slot_path"], r"C:\GuvFX\beta\slots\1\terminal")
        self.assertNotIn(RUUID, b["slot_path"])
        self.assertNotIn(RUUID.replace("-", ""), b["slot_path"].lower())

    def test_task_names_are_fixed_per_slot(self):
        si = _ctx(_store())["slot_input"]
        self.assertEqual((si.launch_task, si.terminate_task),
                         ("GuvFXBetaRuntime-1", "GuvFXBetaRuntimeStop-1"))

    def test_only_materialise_may_allocate(self):
        s = _store()
        for op in ("START", "VERIFY", "STOP", "TOMBSTONE"):
            with self.assertRaises(AgentError, msg=op) as ctx:
                _ctx(s, operation=op)
            self.assertEqual(ctx.exception.reason_code, "runtime_not_assigned")
        self.assertEqual(s.occupancy(), {})              # nothing was consumed by the failed lookups

    def test_resolution_is_stable_across_operations(self):
        s = _store()
        first = _ctx(s)
        for op in ("START", "VERIFY", "STOP", "TOMBSTONE"):
            later = _ctx(s, operation=op)
            self.assertEqual((later["slot"], later["generation"], later["slot_path"]),
                             (first["slot"], first["generation"], first["slot_path"]), op)

    def test_occupancy_id_matches_the_slot_generation_pair(self):
        b = _ctx(_store())
        self.assertEqual(b["occupancy_id"], occupancy_id(b["slot"], b["generation"]))

    def test_two_runtimes_get_different_slots(self):
        s = _store()
        self.assertNotEqual(_ctx(s, RUUID)["slot"], _ctx(s, OTHER)["slot"])

    def test_pool_exhaustion_is_not_a_silent_overwrite(self):
        from stores import PoolExhausted
        s = _store(pool_size=1)
        _ctx(s, RUUID)
        with self.assertRaises(PoolExhausted):
            _ctx(s, OTHER)


class ExecutionModelTests(SimpleTestCase):
    """The model is explicit; a pool agent missing its resolver must not fall back to the old layout."""

    _COMMON = dict(keyring={}, nonce_store=None, idempotency_store=None, op_impls={},
                   agent_version="t", script_manifest={}, script_versions={},
                   resolve_real_path=lambda p: None, runtime_locks=None)

    def test_slot_pool_without_a_resolver_refuses_to_construct(self):
        with self.assertRaises(ValueError):
            BetaProvisioningAgent(execution_model=EXECUTION_MODEL_SLOT_POOL, slot_resolver=None,
                                  **self._COMMON)

    def test_unknown_execution_model_refuses_to_construct(self):
        with self.assertRaises(ValueError):
            BetaProvisioningAgent(execution_model="whatever_works", **self._COMMON)

    def test_default_model_is_the_documented_compatibility_one(self):
        agent = BetaProvisioningAgent(**self._COMMON)
        self.assertEqual(agent.execution_model, EXECUTION_MODEL_UUID_DIR)


class MaterialiseTests(SimpleTestCase):
    def test_materialise_stages_the_copy_and_writes_the_marker_within_the_stage(self):
        s, win = _store(), FakeWin(exists=False)
        out = _impls(win, s).materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        self.assertTrue(out["path_containment_verified"])
        self.assertEqual([c[0] for c in win.calls], ["copy_golden", "write_owner_tag"])
        self.assertEqual(out["slot"], 1)
        self.assertEqual(out["generation"], 1)

    def test_no_marker_survives_a_failed_stage(self):
        """A marker on a partial runtime would vouch for something that was never verified. The stage now
        writes it between the copy and the proof, so a failed proof must still leave the operation refused
        and the slot un-startable."""
        s = _store()
        win = FakeWin(exists=False, dest={"digest": DIGEST, "executable_digest": "exe",
                                          "portable_marker": None, "ownership_marker": True})
        with self.assertRaises(AgentError) as ctx:
            _impls(win, s).materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        self.assertEqual(ctx.exception.reason_code, "stage_copy_incomplete")

    def test_repeat_materialise_is_already_completed_and_copies_nothing(self):
        s, win = _store(), FakeWin(exists=False)
        impls = _impls(win, s)
        impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        win.calls.clear()
        out = impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        self.assertTrue(out["idempotent"])
        self.assertEqual(win.calls, [])

    def test_every_stage_is_recorded_with_evidence(self):
        s, win = _store(), FakeWin(exists=False)
        _impls(win, s).materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        ev = s.stage_evidence_for_occupancy(1, 1)
        self.assertEqual([e["operation"] for e in ev], ["stage_copy"])
        self.assertEqual(ev[0]["stage_status"], lc.COMPLETED)
        s.assert_evidence_complete(1, 1)

    def test_failures_are_recorded_too(self):
        s = _store()
        win = FakeWin(exists=False, dest={"digest": "WRONG", "executable_digest": "exe",
                                          "portable_marker": True, "ownership_marker": True})
        with self.assertRaises(AgentError):
            _impls(win, s).materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        ev = s.stage_evidence_for_occupancy(1, 1)
        self.assertEqual([e["stage_status"] for e in ev], [lc.FAILED])
        self.assertEqual(ev[0]["failure_category"], lc.INTEGRITY)


class IntegrityGateTests(SimpleTestCase):
    """Before every mutating operation the database, marker, UUID, slot and generation must agree."""

    def _materialised(self):
        s, win = _store(), FakeWin(exists=False)
        impls = _impls(win, s)
        impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        return s, win, impls

    def test_a_stale_marker_from_a_previous_occupancy_is_refused(self):
        s, win, impls = self._materialised()
        win._marker = format_owner_marker(RUUID, 1, 0)          # generation 0 = previous occupant
        with self.assertRaises(SlotIntegrityError):
            impls.start(canonical_dir="", runtime_uuid=RUUID, base="",
                        context=_ctx(s, operation="START"))
        self.assertTrue(s.is_quarantined(1))                     # and the slot is quarantined, not repaired

    def test_a_marker_naming_another_runtime_is_refused(self):
        s, win, impls = self._materialised()
        win._marker = format_owner_marker(OTHER, 1, 1)
        with self.assertRaises(SlotIntegrityError):
            impls.start(canonical_dir="", runtime_uuid=RUUID, base="",
                        context=_ctx(s, operation="START"))

    def test_a_missing_marker_is_a_mismatch_not_an_implicit_free_slot(self):
        s, win, impls = self._materialised()
        win._marker = None
        with self.assertRaises(SlotIntegrityError):
            impls.start(canonical_dir="", runtime_uuid=RUUID, base="",
                        context=_ctx(s, operation="START"))

    def test_a_quarantined_slot_refuses_further_mutation(self):
        s, win, impls = self._materialised()
        s.quarantine_slot(1, "operator_test", 1)
        with self.assertRaises(SlotIntegrityError):
            impls.start(canonical_dir="", runtime_uuid=RUUID, base="",
                        context=_ctx(s, operation="START"))

    def test_missing_slot_binding_is_refused(self):
        s, win, impls = self._materialised()
        for bad in (None, {}, {"slot": 1}):
            with self.assertRaises(AgentError) as ctx:
                impls.start(canonical_dir="", runtime_uuid=RUUID, base="", context=bad)
            self.assertEqual(ctx.exception.reason_code, "slot_binding_missing")


class StartStopLifecycleTests(SimpleTestCase):
    def _ready(self, **win_kw):
        s = _store()
        win = FakeWin(exists=False, **win_kw)
        impls = _impls(win, s)
        impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        win.calls.clear()
        return s, win, impls

    def _start(self, s, impls):
        return impls.start(canonical_dir="", runtime_uuid=RUUID, base="",
                           context=_ctx(s, operation="START"))

    def test_start_triggers_the_task_then_proves_the_process(self):
        s, win, impls = self._ready()
        out = self._start(s, impls)
        self.assertEqual([c[0] for c in win.calls], ["run_task"])
        self.assertTrue(out["running"])
        self.assertEqual(out["pid"], 13020)
        self.assertTrue(out["executable_containment_verified"])
        self.assertEqual([e["operation"] for e in s.stage_evidence_for_occupancy(1, 1)][1:],
                         ["precheck_launch_task", "request_launch", "confirm_launch"])

    def test_a_trigger_that_starts_nothing_is_not_a_started_runtime(self):
        s, win, impls = self._ready(launches=False)
        with self.assertRaises(AgentError) as ctx:
            self._start(s, impls)
        self.assertEqual(ctx.exception.reason_code, "process_absent")
        ev = s.stage_evidence_for_occupancy(1, 1)
        self.assertEqual(ev[-2]["stage_status"], lc.REQUESTED)     # the trigger WAS accepted
        self.assertEqual(ev[-1]["stage_status"], lc.FAILED)        # the launch was NOT

    def test_start_never_launches_a_second_terminal(self):
        s, win, impls = self._ready()
        self._start(s, impls)
        win.calls.clear()
        out = self._start(s, impls)
        self.assertTrue(out["idempotent"])
        self.assertEqual(win.calls, [])                            # no second trigger

    def test_verify_is_read_only(self):
        s, win, impls = self._ready()
        self._start(s, impls)
        win.calls.clear()
        out = impls.verify(canonical_dir="", runtime_uuid=RUUID, base="",
                           context=_ctx(s, operation="VERIFY"))
        self.assertTrue(out["running"])
        self.assertEqual(win.calls, [])

    def test_verify_reports_not_running_without_claiming_a_stop(self):
        s, win, impls = self._ready(launches=False)
        out = impls.verify(canonical_dir="", runtime_uuid=RUUID, base="",
                           context=_ctx(s, operation="VERIFY"))
        self.assertFalse(out["running"])

    def test_stop_succeeds_only_once_the_process_is_absent(self):
        s, win, impls = self._ready()
        self._start(s, impls)
        out = impls.stop(canonical_dir="", runtime_uuid=RUUID, base="",
                         context=_ctx(s, operation="STOP"))
        self.assertFalse(out["running"])
        self.assertEqual([e["operation"] for e in s.stage_evidence_for_occupancy(1, 1)][-2:],
                         ["request_terminate", "confirm_terminated"])

    def test_a_stop_trigger_that_leaves_the_process_running_is_a_failure(self):
        s, win, impls = self._ready()
        self._start(s, impls)

        def stubborn(t):
            win.calls.append(("run_task", t)); return True        # accepted, but nothing dies
        win.run_task = stubborn
        with self.assertRaises(AgentError) as ctx:
            impls.stop(canonical_dir="", runtime_uuid=RUUID, base="",
                       context=_ctx(s, operation="STOP"))
        self.assertEqual(ctx.exception.reason_code, "process_still_running")


class TombstoneLifecycleTests(SimpleTestCase):
    def _stopped(self):
        s, win = _store(), FakeWin(exists=False)
        impls = _impls(win, s)
        impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        impls.start(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s, operation="START"))
        impls.stop(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s, operation="STOP"))
        win.calls.clear()
        return s, win, impls

    def test_tombstone_moves_into_this_slots_occupancy_directory(self):
        s, win, impls = self._stopped()
        out = impls.tombstone(canonical_dir="", runtime_uuid=RUUID, base="",
                              context=_ctx(s, operation="TOMBSTONE"))
        self.assertTrue(out["tombstoned"])
        moves = [c for c in win.calls if c[0] == "move_dir"]
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][2], rf"C:\GuvFX\beta\tombstones\1\{occupancy_id(1, 1)}")

    def test_cleanup_runs_after_the_move_and_before_release(self):
        s, win, impls = self._stopped()
        impls.tombstone(canonical_dir="", runtime_uuid=RUUID, base="",
                        context=_ctx(s, operation="TOMBSTONE"))
        ops = [e["operation"] for e in s.stage_evidence_for_occupancy(1, 1)]
        self.assertEqual(ops[-3:], ["precheck_cleanup", "tombstone", "verify_cleanup"])
        self.assertEqual(s.generation_of(1), 1)          # release has NOT happened here
        s.assert_evidence_complete(1, 1)

    def test_a_surviving_process_blocks_the_move_itself(self):
        """The refusal must come BEFORE the irreversible move, not after it."""
        s, win, impls = self._stopped()
        win._process = _proc()                            # something is running in the slot again
        with self.assertRaises(AgentError) as ctx:
            impls.tombstone(canonical_dir="", runtime_uuid=RUUID, base="",
                            context=_ctx(s, operation="TOMBSTONE"))
        self.assertEqual(ctx.exception.reason_code, "cleanup_precheck_failed")
        self.assertEqual([c for c in win.calls if c[0] == "move_dir"], [])   # nothing was moved

    def test_the_whole_lifecycle_reconciles(self):
        s, win, impls = self._stopped()
        impls.tombstone(canonical_dir="", runtime_uuid=RUUID, base="",
                        context=_ctx(s, operation="TOMBSTONE"))
        ops = [e["operation"] for e in s.stage_evidence_for_occupancy(1, 1)]
        self.assertEqual(ops, ["stage_copy", "precheck_launch_task", "request_launch", "confirm_launch",
                               "precheck_terminate_task", "request_terminate", "confirm_terminated",
                               "precheck_cleanup",
                               "tombstone", "verify_cleanup"])
        s.assert_sequence_valid(1, 1)
        s.assert_evidence_complete(1, 1)
        for e in s.stage_evidence_for_occupancy(1, 1):
            self.assertIn(e["stage_status"], (lc.COMPLETED, lc.ALREADY_COMPLETED, lc.REQUESTED))


class ResponseBoundaryTests(SimpleTestCase):
    """The path never crosses the management channel, however deep the lifecycle goes."""

    def test_no_operation_returns_a_filesystem_path(self):
        s, win = _store(), FakeWin(exists=False)
        impls = _impls(win, s)
        outs = [impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))]
        outs.append(impls.start(canonical_dir="", runtime_uuid=RUUID, base="",
                                context=_ctx(s, operation="START")))
        outs.append(impls.verify(canonical_dir="", runtime_uuid=RUUID, base="",
                                 context=_ctx(s, operation="VERIFY")))
        outs.append(impls.stop(canonical_dir="", runtime_uuid=RUUID, base="",
                               context=_ctx(s, operation="STOP")))
        outs.append(impls.tombstone(canonical_dir="", runtime_uuid=RUUID, base="",
                                    context=_ctx(s, operation="TOMBSTONE")))
        for out in outs:
            self.assertNotIn("canonical_path", out)
            for value in out.values():
                self.assertNotIn(r"C:\GuvFX", str(value))
            self.assertTrue(out["canonical_path_digest"])       # the attestation, not the layout


class SettleWindowTests(SimpleTestCase):
    """A scheduler trigger is asynchronous, so the lifecycle re-observes before concluding."""

    def _ready(self, **kw):
        s, win = _store(), FakeWin(exists=False, **kw)
        impls = _impls(win, s)
        impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        win.calls.clear()
        return s, win, impls

    def test_a_slow_launch_still_completes(self):
        """The process appears on the third observation, not the first."""
        s, win, impls = self._ready(launches=False)
        observations = {"n": 0}
        original = win.query_slot_process

        def slow(path, identity=""):
            observations["n"] += 1
            return _proc() if observations["n"] >= 3 else original(path, identity)
        win.query_slot_process = slow
        out = impls.start(canonical_dir="", runtime_uuid=RUUID, base="",
                          context=_ctx(s, operation="START"))
        self.assertTrue(out["running"])
        self.assertGreaterEqual(observations["n"], 3)

    def test_a_launch_that_never_appears_still_fails(self):
        s, win, impls = self._ready(launches=False)
        with self.assertRaises(AgentError) as ctx:
            impls.start(canonical_dir="", runtime_uuid=RUUID, base="",
                        context=_ctx(s, operation="START"))
        self.assertEqual(ctx.exception.reason_code, "process_absent")


class UnobservableStartTests(SimpleTestCase):
    """START must not trigger on a slot it could not observe: a running-but-unreadable runtime would get a
    SECOND terminal, after which two processes match the slot executable and every later operation on that
    slot raises ambiguous_slot_process for ever."""

    def test_an_unobservable_slot_is_never_launched(self):
        s, win = _store(), FakeWin(exists=False)
        impls = _impls(win, s)
        impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        win.calls.clear()

        def blind(path, identity=""):
            raise OSError("enumeration unavailable")
        win.query_slot_process = blind
        with self.assertRaises(AgentError) as ctx:
            impls.start(canonical_dir="", runtime_uuid=RUUID, base="",
                        context=_ctx(s, operation="START"))
        self.assertEqual(ctx.exception.reason_code, "process_observation_unavailable")
        self.assertNotIn("run_task", [c[0] for c in win.calls])


class UnreadableMarkerTests(SimpleTestCase):
    """An unreadable marker is an observation failure. Recording it as an ownership mismatch would
    quarantine a perfectly healthy running slot on a transient permission error."""

    def test_unreadable_marker_refuses_without_quarantining(self):
        s, win = _store(), FakeWin(exists=False)
        impls = _impls(win, s)
        impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))

        def denied(path):
            raise PermissionError("denied")
        win.read_owner_tag = denied
        with self.assertRaises(AgentError) as ctx:
            impls.start(canonical_dir="", runtime_uuid=RUUID, base="",
                        context=_ctx(s, operation="START"))
        self.assertEqual(ctx.exception.reason_code, "owner_marker_unreadable")
        self.assertFalse(s.is_quarantined(1))


class TombstoneRetryTests(SimpleTestCase):
    """A TOMBSTONE that moved the directory and then failed at cleanup is retriable. The ordinary gate
    would read the (correctly) absent marker as corruption and quarantine the slot permanently."""

    def _torn_down(self):
        s, win = _store(), FakeWin(exists=False)
        impls = _impls(win, s)
        impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        impls.start(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s, operation="START"))
        impls.stop(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s, operation="STOP"))
        win.open_handles = lambda p: (_ for _ in ()).throw(OSError("unsupported"))
        with self.assertRaises(AgentError):
            impls.tombstone(canonical_dir="", runtime_uuid=RUUID, base="",
                            context=_ctx(s, operation="TOMBSTONE"))
        return s, win, impls

    def test_the_first_attempt_refused_BEFORE_moving_anything(self):
        """With open_handles unanswerable, the precheck blocks and the runtime directory stays put -
        a doomed teardown now costs nothing instead of leaving the tree under the tombstone root."""
        s, win, impls = self._torn_down()
        self.assertTrue(win.path_exists(""))              # still there
        self.assertFalse(s.is_quarantined(1))

    def test_a_retry_resumes_instead_of_quarantining(self):
        s, win, impls = self._torn_down()
        win.open_handles = lambda p: False                # the blocking condition clears
        out = impls.tombstone(canonical_dir="", runtime_uuid=RUUID, base="",
                              context=_ctx(s, operation="TOMBSTONE"))
        self.assertTrue(out["tombstoned"])
        self.assertFalse(s.is_quarantined(1))

    def test_a_retry_for_a_different_runtime_is_still_refused(self):
        s, win, impls = self._torn_down()
        win.move_dir("", "")                              # simulate the move having happened
        s.assign(OTHER, now=5)
        with self.assertRaises(SlotIntegrityError):
            impls.tombstone(canonical_dir="", runtime_uuid=OTHER, base="",
                            context={"slot": 1, "generation": 1, "slot_input": _ctx(s)["slot_input"],
                                     "slot_path": "", "slots_root": ""})


class ReleaseTests(SimpleTestCase):
    """TOMBSTONE reports the release gap rather than implying the slot is free."""

    def _tombstoned(self):
        s, win = _store(), FakeWin(exists=False)
        impls = _impls(win, s)
        impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        impls.start(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s, operation="START"))
        impls.stop(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s, operation="STOP"))
        out = impls.tombstone(canonical_dir="", runtime_uuid=RUUID, base="",
                              context=_ctx(s, operation="TOMBSTONE"))
        return s, impls, out

    def test_tombstone_states_the_release_is_pending(self):
        s, impls, out = self._tombstoned()
        self.assertFalse(out["released"])
        self.assertTrue(out["release_pending"])
        self.assertEqual(s.generation_of(1), 1)           # generation has NOT advanced

    def test_release_advances_the_generation_once(self):
        s, impls, _out = self._tombstoned()
        impls.release(runtime_uuid=RUUID, slot=1, generation=1,
                      no_ambiguous_provisioning_job=True, no_mutation_lock_held=True)
        self.assertEqual(s.generation_of(1), 2)
        self.assertIsNone(s.lookup(RUUID))

    def test_release_refuses_when_a_proof_the_layer_cannot_observe_is_false(self):
        from stores import ReleaseProofMissing
        s, impls, _out = self._tombstoned()
        with self.assertRaises(ReleaseProofMissing):
            impls.release(runtime_uuid=RUUID, slot=1, generation=1,
                          no_ambiguous_provisioning_job=True, no_mutation_lock_held=False)
        self.assertEqual(s.generation_of(1), 1)


class ExecutionModelWiringTests(SimpleTestCase):
    """The agent must actually BUILD the pool model when configured for it — and refuse rather than
    silently revert when the settings it needs are absent."""

    @staticmethod
    def _approved_file():
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        f.write(json.dumps({"GuvFXBetaRuntime-1": APPROVED_TASK})); f.close()
        return f.name

    ENV = {
        "BETA_AGENT_BIND_HOST": "100.79.101.19",
        "BETA_AGENT_BIND_PORT": "8791",
        "BETA_AGENT_KEYRING": '{"k1": "0123456789abcdef0123456789abcdef"}',
        "BETA_AGENT_KEY_ID": "k1",
    }

    def _cfg(self, **over):
        import config as agent_config
        env = dict(self.ENV)
        env.update(over)
        return agent_config.load_config(env)

    def test_default_config_is_the_compatibility_model(self):
        from lib.mgmt_agent_core import EXECUTION_MODEL_UUID_DIR as UUID_DIR
        self.assertEqual(self._cfg()["execution_model"], UUID_DIR)

    def test_slot_pool_without_a_pool_size_refuses(self):
        from config import ConfigError
        with self.assertRaises(ConfigError):
            self._cfg(BETA_AGENT_EXECUTION_MODEL="slot_pool", BETA_AGENT_GOLDEN_DIGEST="d")

    def test_slot_pool_without_a_golden_manifest_version_refuses(self):
        """An empty version would make the stage-copy pre-check compare '' == '' and pass."""
        from config import ConfigError
        with self.assertRaises(ConfigError):
            self._cfg(BETA_AGENT_EXECUTION_MODEL="slot_pool", BETA_AGENT_SLOT_POOL_SIZE="4",
                      BETA_AGENT_GOLDEN_DIGEST="d")

    def test_a_relocated_slots_root_refuses_at_startup(self):
        """The knob was honoured for the containment base but ignored by path derivation, so any other
        value made every operation fail path_escape at runtime instead of at startup."""
        from config import ConfigError
        with self.assertRaises(ConfigError):
            self._cfg(BETA_AGENT_EXECUTION_MODEL="slot_pool", BETA_AGENT_SLOT_POOL_SIZE="4",
                      BETA_AGENT_GOLDEN_DIGEST="d", BETA_AGENT_GOLDEN_MANIFEST_VERSION="v",
                      BETA_AGENT_SLOTS_ROOT=r"D:\GuvFX\beta\slots")

    def test_a_settle_window_longer_than_the_drain_budget_refuses(self):
        """A settle window past the drain budget guarantees a service stop force-kills a mutation."""
        from config import ConfigError
        with self.assertRaises(ConfigError):
            self._cfg(BETA_AGENT_EXECUTION_MODEL="slot_pool", BETA_AGENT_SLOT_POOL_SIZE="4",
                      BETA_AGENT_GOLDEN_DIGEST="d", BETA_AGENT_GOLDEN_MANIFEST_VERSION="v",
                      BETA_AGENT_DRAIN_TIMEOUT_S="20")

    def test_the_built_implementation_has_a_real_clock(self):
        """Omitted, now_fn falls back to lambda: 0 and every durable timestamp is written as 0."""
        import agent as agent_mod
        cfg = self._cfg(BETA_AGENT_EXECUTION_MODEL="slot_pool", BETA_AGENT_SLOT_POOL_SIZE="4",
                        BETA_AGENT_GOLDEN_DIGEST="d", BETA_AGENT_GOLDEN_MANIFEST_VERSION="v",
                        BETA_AGENT_APPROVED_TASKS=self._approved_file(),
                        BETA_AGENT_DRAIN_TIMEOUT_S="45")
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        built = agent_mod.build_agent(cfg, win=FakeWin(), store=_FakeStore(),
                                      slot_store_override=SlotStore(f.name, pool_size=4))
        self.assertGreater(built.op_impls["MATERIALISE"].__self__.now_fn(), 0)

    def test_slot_pool_without_a_golden_digest_refuses(self):
        """An unset digest would make the stage-copy integrity check compare against the empty string."""
        from config import ConfigError
        with self.assertRaises(ConfigError):
            self._cfg(BETA_AGENT_EXECUTION_MODEL="slot_pool", BETA_AGENT_SLOT_POOL_SIZE="4")

    def test_unknown_execution_model_refuses(self):
        from config import ConfigError
        with self.assertRaises(ConfigError):
            self._cfg(BETA_AGENT_EXECUTION_MODEL="whatever")

    def test_pool_mode_builds_the_pool_implementations_and_resolver(self):
        import agent as agent_mod
        cfg = self._cfg(BETA_AGENT_EXECUTION_MODEL="slot_pool", BETA_AGENT_SLOT_POOL_SIZE="4",
                        BETA_AGENT_GOLDEN_DIGEST="golden-digest-abc",
                        BETA_AGENT_GOLDEN_MANIFEST_VERSION="2026-07-22.24",
                        BETA_AGENT_APPROVED_TASKS=self._approved_file(),
                        BETA_AGENT_DRAIN_TIMEOUT_S="45")
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        built = agent_mod.build_agent(cfg, win=FakeWin(), store=_FakeStore(),
                                      slot_store_override=SlotStore(f.name, pool_size=4))
        self.assertEqual(built.execution_model, "slot_pool")
        self.assertIsInstance(built.slot_resolver, SlotResolver)
        self.assertIsInstance(built.op_impls["MATERIALISE"].__self__, PoolOpImplementations)

    def test_compatibility_mode_builds_neither(self):
        import agent as agent_mod
        from op_impls import OpImplementations
        built = agent_mod.build_agent(self._cfg(), win=FakeWin(), store=_FakeStore())
        self.assertIsNone(built.slot_resolver)
        self.assertIsInstance(built.op_impls["MATERIALISE"].__self__, OpImplementations)


class _FakeStore:
    def burn(self, nonce, expiry): return True
    def get(self, job_id, op): return None
    def put(self, job_id, op, record): pass


class ReleasePendingReachesTheBackendTests(SimpleTestCase):
    """The release boundary is only a boundary if the backend can see it. Stripped by the sanitiser, the
    backend saw an unqualified TOMBSTONE success and would believe the pool had capacity it does not."""

    def test_the_allowlist_carries_the_release_state(self):
        from lib.mgmt_agent_core import _RESPONSE_ALLOWLIST
        self.assertIn("released", _RESPONSE_ALLOWLIST)
        self.assertIn("release_pending", _RESPONSE_ALLOWLIST)

    def test_the_full_path_still_never_crosses(self):
        from lib.mgmt_agent_core import _RESPONSE_ALLOWLIST
        self.assertNotIn("canonical_path", _RESPONSE_ALLOWLIST)
        self.assertNotIn("tombstone_dir", _RESPONSE_ALLOWLIST)


class VerifyObservabilityTests(SimpleTestCase):
    """VERIFY is the operation a worker calls to reconcile after an ambiguous STOP. Reporting
    running=False for an observation that FAILED would let the backend conclude a live terminal is
    stopped and proceed to tombstone it."""

    def _ready(self):
        s, win = _store(), FakeWin(exists=False)
        impls = _impls(win, s)
        impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        return s, win, impls

    def test_unobservable_is_not_reported_as_not_running(self):
        s, win, impls = self._ready()

        def blind(path, identity=""):
            raise OSError("enumeration unavailable")
        win.query_slot_process = blind
        with self.assertRaises(AgentError) as ctx:
            impls.verify(canonical_dir="", runtime_uuid=RUUID, base="",
                         context=_ctx(s, operation="VERIFY"))
        self.assertEqual(ctx.exception.reason_code, "process_observation_unavailable")

    def test_a_proven_absence_is_still_reported_as_not_running(self):
        s, win, impls = self._ready()
        out = impls.verify(canonical_dir="", runtime_uuid=RUUID, base="",
                           context=_ctx(s, operation="VERIFY"))
        self.assertFalse(out["running"])


class LaunchTaskVerificationGateTests(SimpleTestCase):
    """F3, promoted to an implementation gate: a launch may not proceed unless task-definition
    verification has executed successfully FOR THIS OCCUPANCY.

    Having inspect_task and assert_task_matches_approved available was not sufficient - nothing called
    them, so the agent would have triggered a task without ever asserting what that task now does.
    """

    def _ready(self, **kw):
        s, win = _store(), FakeWin(exists=False, **kw)
        impls = _impls(win, s)
        impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        win.calls.clear()
        return s, win, impls

    def _start(self, s, impls):
        return impls.start(canonical_dir="", runtime_uuid=RUUID, base="",
                           context=_ctx(s, operation="START"))

    def test_verification_is_recorded_as_a_stage_before_the_trigger(self):
        s, win, impls = self._ready()
        self._start(s, impls)
        ops = [e["operation"] for e in s.stage_evidence_for_occupancy(1, 1)]
        self.assertEqual(ops[1:4], ["precheck_launch_task", "request_launch", "confirm_launch"])

    def test_a_task_repointed_outside_the_slot_never_launches(self):
        """Containment fires before the digest comparison — a stronger guarantee, asserted as such."""
        s, win, impls = self._ready()
        win._task_definition = dict(APPROVED_TASK, executable=r"C:\Windows\System32\cmd.exe")
        with self.assertRaises(AgentError) as ctx:
            self._start(s, impls)
        self.assertEqual(ctx.exception.reason_code, "executable_outside_slot")
        self.assertEqual([c for c in win.calls if c[0] == "run_task"], [])

    def test_drift_within_the_slot_still_blocks_the_launch(self):
        """The digest path: a change the containment guard cannot see, e.g. the working directory."""
        s, win, impls = self._ready()
        win._task_definition = dict(APPROVED_TASK, working_directory=r"C:\GuvFX\beta\slots\1")
        with self.assertRaises(AgentError) as ctx:
            self._start(s, impls)
        self.assertEqual(ctx.exception.reason_code, "task_definition_drift")
        self.assertEqual([c for c in win.calls if c[0] == "run_task"], [])

    def test_a_changed_principal_blocks_the_launch(self):
        s, win, impls = self._ready()
        win._task_definition = dict(APPROVED_TASK, run_as_identity="Administrator")
        with self.assertRaises(AgentError):
            self._start(s, impls)
        self.assertEqual([c for c in win.calls if c[0] == "run_task"], [])

    def test_a_disabled_task_blocks_the_launch(self):
        s, win, impls = self._ready()
        win._task_definition = dict(APPROVED_TASK, enabled=False)
        with self.assertRaises(AgentError):
            self._start(s, impls)

    def test_an_absent_task_blocks_the_launch(self):
        s, win, impls = self._ready()
        win._task_definition = None
        with self.assertRaises(AgentError) as ctx:
            self._start(s, impls)
        self.assertEqual(ctx.exception.reason_code, "task_absent")

    def test_a_slot_with_no_approved_definition_can_never_launch(self):
        s, win = _store(), FakeWin(exists=False)
        impls = _impls(win, s, approved_tasks={})
        impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        win.calls.clear()
        with self.assertRaises(AgentError) as ctx:
            self._start(s, impls)
        self.assertEqual(ctx.exception.reason_code, "approved_task_definition_missing")
        self.assertEqual(win.calls, [])

    def test_the_agent_never_repairs_a_drifted_task(self):
        s, win, impls = self._ready()
        win._task_definition = dict(APPROVED_TASK, run_level=1)
        with self.assertRaises(AgentError):
            self._start(s, impls)
        for forbidden in ("register_task", "set_acl", "enable_task"):
            self.assertEqual([c for c in win.calls if c[0] == forbidden], [], forbidden)


class ApprovedTasksConfigTests(SimpleTestCase):
    """The approval file is loaded at startup and fails closed three distinct ways."""

    ENV = dict(ExecutionModelWiringTests.ENV, BETA_AGENT_EXECUTION_MODEL="slot_pool",
               BETA_AGENT_SLOT_POOL_SIZE="4", BETA_AGENT_GOLDEN_DIGEST="d",
               BETA_AGENT_GOLDEN_MANIFEST_VERSION="v", BETA_AGENT_DRAIN_TIMEOUT_S="45")

    def _cfg(self, **over):
        import config as agent_config
        env = dict(self.ENV); env.update(over)
        return agent_config.load_config(env)

    def _file(self, content):
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        f.write(content); f.close()
        return f.name

    def test_missing_path_refuses(self):
        from config import ConfigError
        with self.assertRaises(ConfigError):
            self._cfg()

    def test_unreadable_file_refuses(self):
        from config import ConfigError
        with self.assertRaises(ConfigError):
            self._cfg(BETA_AGENT_APPROVED_TASKS="/nonexistent/approved_tasks.json")

    def test_malformed_json_refuses(self):
        from config import ConfigError
        with self.assertRaises(ConfigError):
            self._cfg(BETA_AGENT_APPROVED_TASKS=self._file("{not json"))

    def test_a_definition_missing_a_pinned_field_refuses(self):
        """An approval that omits a field does not actually approve anything about it."""
        from config import ConfigError
        partial = dict(APPROVED_TASK); partial.pop("run_as_identity")
        with self.assertRaises(ConfigError) as ctx:
            self._cfg(BETA_AGENT_APPROVED_TASKS=self._file(json.dumps({"GuvFXBetaRuntime-1": partial})))
        self.assertIn("run_as_identity", str(ctx.exception))

    def test_a_valid_file_loads_with_a_digest(self):
        cfg = self._cfg(BETA_AGENT_APPROVED_TASKS=self._file(
            json.dumps({"GuvFXBetaRuntime-1": APPROVED_TASK})))
        self.assertEqual(cfg["approved_tasks"]["GuvFXBetaRuntime-1"]["run_as_identity"], "guvfx_b_slot1")
        self.assertTrue(cfg["approved_tasks_digest"])      # a change to the approvals is visible

    def test_empty_approvals_refuse(self):
        from config import ConfigError
        with self.assertRaises(ConfigError):
            self._cfg(BETA_AGENT_APPROVED_TASKS=self._file("{}"))


class PortableSwitchGateTests(SimpleTestCase):
    """Portable mode is a per-LAUNCH command-line property, so `arguments` is part of task identity.

    A task edited from /portable to nothing keeps per-instance state in the identity's %APPDATA% - OUTSIDE
    the slot, where tombstoning cannot reach it - to be inherited by the next occupancy. That is exactly the
    leak install_pool.ps1 refuses in the golden image, arriving by a different door.
    """

    def _ready(self):
        s, win = _store(), FakeWin(exists=False)
        impls = _impls(win, s)
        impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        win.calls.clear()
        return s, win, impls

    def test_a_task_that_lost_portable_blocks_the_launch(self):
        s, win, impls = self._ready()
        win._task_definition = dict(APPROVED_TASK, arguments="")
        with self.assertRaises(AgentError) as ctx:
            impls.start(canonical_dir="", runtime_uuid=RUUID, base="",
                        context=_ctx(s, operation="START"))
        self.assertEqual(ctx.exception.reason_code, "task_definition_drift")
        self.assertEqual([c for c in win.calls if c[0] == "run_task"], [])

    def test_arguments_are_part_of_task_identity(self):
        from occupancy import TASK_IDENTITY_FIELDS
        self.assertIn("arguments", TASK_IDENTITY_FIELDS)

    def test_the_gate_records_the_observed_portable_switch(self):
        """It was computed by the adapter and dropped by inspect_task, so every gate reported it null -
        indistinguishable from 'not portable'."""
        s, win, impls = self._ready()
        impls.start(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s, operation="START"))
        import win_primitives as wp
        obs = wp.inspect_task(win, wp.resolve_slot_input(1), which="launch", observed_at=100)
        self.assertTrue(obs["evidence"]["portable_switch"])
        self.assertEqual(obs["evidence"]["arguments"], "/portable")


class TerminateGateTests(SimpleTestCase):
    """Negative coverage for the terminate gate.

    Mutation-tested during review: deleting the whole containment branch, or dropping the scope check
    alone, left the suite green. A gate whose removal no test notices is not a gate.
    """

    def _stopped_impls(self, **win_kw):
        """A materialised, started, now-absent runtime — the state STOP is called from."""
        s = _store()
        win = FakeWin(exists=False, **win_kw)
        impls = _impls(win, s)
        impls.materialise(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s))
        impls.start(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s, operation="START"))
        win._process = None            # the terminate trigger will find it gone
        win.calls.clear()
        return s, win, impls

    def test_a_terminate_task_without_an_approval_refuses_before_triggering(self):
        s, win, impls = self._stopped_impls()
        impls.approved_tasks.pop("GuvFXBetaRuntimeStop-1")
        with self.assertRaises(AgentError) as ctx:
            impls.stop(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s, operation="STOP"))
        self.assertEqual(ctx.exception.reason_code, "approved_task_definition_missing")
        self.assertNotIn(("run_task", "GuvFXBetaRuntimeStop-1"),
                         [(c[0], c[1]) for c in win.calls if c[0] == "run_task"])

    def test_a_terminate_task_whose_arguments_lost_the_path_filter_is_refused(self):
        """The exact edit an operator would make to 'fix' a failing STOP: drop the Where-Object clause."""
        s, win, impls = self._stopped_impls()
        win._stop_definition = dict(
            APPROVED_STOP,
            arguments='-NoProfile -Command "Get-Process -Name terminal64 | Stop-Process -Force"')
        with self.assertRaises(AgentError) as ctx:
            impls.stop(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s, operation="STOP"))
        self.assertEqual(ctx.exception.reason_code, "terminate_scope_unbounded")

    def test_a_terminate_task_pointed_at_another_executable_is_refused(self):
        s, win, impls = self._stopped_impls()
        win._stop_definition = dict(APPROVED_STOP, executable=r"C:\Windows\System32\taskkill.exe")
        with self.assertRaises(AgentError) as ctx:
            impls.stop(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s, operation="STOP"))
        self.assertEqual(ctx.exception.reason_code, "terminate_executable_unexpected")

    def test_a_terminate_task_scoped_to_a_DIFFERENT_slot_is_refused(self):
        """slots\\1 is a substring of slots\\10, so containment must be tested at a path boundary."""
        s, win, impls = self._stopped_impls()
        win._stop_definition = dict(
            APPROVED_STOP,
            arguments=APPROVED_STOP["arguments"].replace(r"slots\1\terminal", r"slots\10\terminal"))
        with self.assertRaises(AgentError) as ctx:
            impls.stop(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s, operation="STOP"))
        self.assertEqual(ctx.exception.reason_code, "terminate_scope_unbounded")

    def test_a_drifted_terminate_definition_is_refused(self):
        s, win, impls = self._stopped_impls()
        win._stop_definition = dict(APPROVED_STOP, run_as_identity="Administrator")
        with self.assertRaises(AgentError) as ctx:
            impls.stop(canonical_dir="", runtime_uuid=RUUID, base="", context=_ctx(s, operation="STOP"))
        self.assertIn(ctx.exception.reason_code, ("task_definition_drift", "forbidden_run_as_identity"))
