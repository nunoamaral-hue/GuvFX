"""CVM-Inc-3 B3P-2 — real Windows adapter tests (run off-host).

What CAN be proven here: the adapter satisfies the same contract as the fake; its decision logic is
correct; and off-host every method fails closed instead of inventing an answer.

What CANNOT be proven here, and is not claimed: that any Win32 call behaves as documented on
WIN-RD8VDS93DK7. That is the viability trial's job — see docs/B3P2_WINDOWS_RESEARCH_FINDINGS.md §4.
"""
import inspect
import os
import sys

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "beta-agent")
if _BUNDLE not in sys.path:
    sys.path.insert(0, _BUNDLE)

import win_slot_ops as wso          # noqa: E402
from win_ops import SlotWindowsOps, WindowsOpsError    # noqa: E402


def _adapter():
    return wso.RealSlotWindowsOps(golden_dir=r"C:\GuvFX\beta\golden", slots_root=r"C:\GuvFX\beta\slots")


class ContractConformanceTests(SimpleTestCase):
    """The fake and the real adapter must satisfy the SAME contract — that is what makes the off-host
    suite evidence about the real system rather than about a mock."""

    def test_implements_every_interface_method_with_the_same_signature(self):
        for name, declared in vars(SlotWindowsOps).items():
            if name.startswith("_") or not inspect.isfunction(declared):
                continue
            impl = getattr(wso.RealSlotWindowsOps, name, None)
            self.assertTrue(inspect.isfunction(impl), f"{name} not implemented")
            self.assertEqual(list(inspect.signature(impl).parameters)[1:],
                             list(inspect.signature(declared).parameters)[1:], name)

    def test_adds_no_mutating_method_beyond_the_four(self):
        """The interface's absences ARE the security property; the implementation must not reintroduce
        a kill, a launch, a delete or an ACL write."""
        public = {n for n, o in vars(wso.RealSlotWindowsOps).items()
                  if inspect.isfunction(o) and not n.startswith("_")}
        self.assertEqual(public - {n for n, o in vars(SlotWindowsOps).items() if inspect.isfunction(o)},
                         set())
        for forbidden in ("stop_pid", "kill", "terminate", "launch_runtime", "delete", "rmtree",
                          "set_acl", "register_task", "create_user", "make_dirs"):
            self.assertFalse(hasattr(wso.RealSlotWindowsOps, forbidden), forbidden)


class FailClosedOffHostTests(SimpleTestCase):
    """Off-host every Win32-backed method must RAISE. A plausible-looking answer would be a fabricated
    fact about a machine this process cannot see."""

    def test_win32_backed_methods_raise_off_host(self):
        if os.name == "nt":
            self.skipTest("this test asserts the off-host behaviour")
        a = _adapter()
        for call in (lambda: a.same_volume("a", "b"),
                     lambda: a.query_task("GuvFXBetaRuntime-1"),
                     lambda: a.task_running("GuvFXBetaRuntime-1"),
                     lambda: a.run_task("GuvFXBetaRuntime-1"),
                     lambda: a._win32(),
                     lambda: a.copy_golden(r"C:\GuvFX\beta\slots\1\terminal"),
                     lambda: a.read_acl(r"C:\GuvFX")):
            with self.assertRaises(WindowsOpsError):
                call()

    def test_unavailability_has_its_own_reason_code(self):
        if os.name == "nt":
            self.skipTest("this test asserts the off-host behaviour")
        with self.assertRaises(wso.WindowsApiUnavailable) as ctx:
            _adapter().query_task("GuvFXBetaRuntime-1")
        self.assertEqual(ctx.exception.reason_code, "windows_api_unavailable")

    def test_open_handles_fails_closed_off_host(self):
        """WS-B: open_handles is now IMPLEMENTED (Restart Manager, host-proven). For a slot dir that EXISTS
        it must still fail closed off-host — it canonicalises (Win32) after the existence check, so it raises
        windows_api_unavailable rather than fabricating a clear/held answer. (A GONE dir legitimately returns
        clear — that path is covered by OpenHandlesOrderingTests.)"""
        import tempfile
        import shutil
        if os.name == "nt":
            self.skipTest("asserts the off-host behaviour")
        tmp = tempfile.mkdtemp()
        try:
            sub = os.path.join(tmp, "slot"); os.mkdir(sub)
            a = wso.RealSlotWindowsOps(golden_dir=tmp, slots_root=tmp)
            with self.assertRaises(WindowsOpsError) as ctx:
                a.open_handles(sub)
            self.assertEqual(ctx.exception.reason_code, "windows_api_unavailable")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RobocopyExitTests(SimpleTestCase):
    """The classic robocopy bug in both directions."""

    def test_exit_one_is_success_not_failure(self):
        # 'All files were copied successfully' — the NORMAL result of a healthy fresh copy.
        self.assertEqual(wso.classify_robocopy_exit(1), "accepted")

    def test_exit_zero_is_success(self):
        self.assertEqual(wso.classify_robocopy_exit(0), "accepted")

    def test_documented_failure_threshold_rejects(self):
        for rc in (8, 9, 15, 16, 32):
            self.assertEqual(wso.classify_robocopy_exit(rc), "failed", rc)

    def test_undocumented_middle_codes_are_rejected_not_assumed_successful(self):
        """Microsoft's '>= 8 is failure' statement is one-directional; '0-7 is success' is folklore. Into a
        destination proven ABSENT, extras (2/3/6/7) or mismatches (5/6/7) cannot legitimately occur."""
        for rc in (2, 3, 4, 5, 6, 7):
            self.assertEqual(wso.classify_robocopy_exit(rc), "failed", rc)


class SlotProcessSelectionTests(SimpleTestCase):
    SLOT = r"C:\GuvFX\beta\slots\2\terminal"

    def _c(self, image, pid=1):
        return {"pid": pid, "image": image}

    def test_none_means_not_running(self):
        self.assertIsNone(wso.select_slot_process([], self.SLOT))

    def test_single_candidate_is_returned(self):
        c = self._c(self.SLOT + r"\terminal64.exe")
        self.assertIs(wso.select_slot_process([c], self.SLOT), c)

    def test_several_candidates_resolve_to_the_runtime_executable(self):
        helper = self._c(self.SLOT + r"\metaeditor64.exe", pid=2)
        terminal = self._c(self.SLOT + r"\terminal64.exe", pid=3)
        self.assertIs(wso.select_slot_process([helper, terminal], self.SLOT), terminal)

    def test_ambiguity_raises_rather_than_picking_by_enumeration_order(self):
        a, b = self._c(self.SLOT + r"\helper1.exe", 4), self._c(self.SLOT + r"\helper2.exe", 5)
        with self.assertRaises(WindowsOpsError) as ctx:
            wso.select_slot_process([a, b], self.SLOT)
        self.assertEqual(ctx.exception.reason_code, "ambiguous_slot_process")

    def test_selection_is_case_insensitive_like_the_filesystem(self):
        c = self._c(self.SLOT.upper() + r"\TERMINAL64.EXE", pid=6)
        other = self._c(self.SLOT + r"\x.exe", pid=7)
        self.assertIs(wso.select_slot_process([other, c], self.SLOT), c)


class OpenProcessErrorTests(SimpleTestCase):
    """Denied must never be read as absent — that is how a live runtime gets reported as terminated."""

    def test_dead_pid_is_gone(self):
        self.assertEqual(wso.classify_open_process_error(87), "gone")

    def test_access_denied_is_denied_not_gone(self):
        self.assertEqual(wso.classify_open_process_error(5), "denied")
        self.assertNotEqual(wso.classify_open_process_error(5), "gone")

    def test_anything_else_is_unknown_not_gone(self):
        for code in (0, 6, 8, 1450, 299):
            self.assertEqual(wso.classify_open_process_error(code), "unknown", code)


class Win32BindingTests(SimpleTestCase):
    """Two silent 64-bit-only defects that would otherwise have surfaced only on the box."""

    def test_bindings_declare_restype_and_use_last_error(self):
        source = inspect.getsource(wso.RealSlotWindowsOps._win32)
        # Without restype=HANDLE, ctypes defaults to C int and truncates a 64-bit handle.
        self.assertIn("OpenProcess.restype = wintypes.HANDLE", source)
        # Without use_last_error=True, ctypes.get_last_error() always returns 0, so denied-vs-gone -
        # the distinction the whole design rests on - would read every failure as "unknown".
        self.assertIn('WinDLL("kernel32", use_last_error=True)', source)

    def test_no_undeclared_windll_shortcut_remains(self):
        source = open(os.path.join(_BUNDLE, "win_slot_ops.py"), encoding="utf-8").read()
        self.assertNotIn("ctypes.windll", source)      # the undeclared, truncating form


class UnattributableProcessTests(SimpleTestCase):
    """A process that cannot be attributed to a location must not be silently skipped: skipping it turns
    'I could not look' into 'nothing is running', which is the fail-open the design exists to prevent."""

    def test_the_guard_is_present_and_raises(self):
        source = inspect.getsource(wso.RealSlotWindowsOps.query_slot_process)
        self.assertIn("process_attribution_incomplete", source)
        self.assertIn("unattributable", source)

    def test_the_reason_code_is_an_observation_failure_not_an_absence(self):
        import lifecycle as lc
        self.assertEqual(lc.classify("process_attribution_incomplete"), lc.OBSERVATION)


class TreeDigestTests(SimpleTestCase):
    def test_digest_is_order_independent(self):
        a = [("a.txt", 1, "aa"), ("b/c.txt", 2, "bb")]
        self.assertEqual(wso.tree_digest(a), wso.tree_digest(list(reversed(a))))

    def test_separator_and_case_are_normalised(self):
        self.assertEqual(wso.tree_digest([("B/C.TXT", 2, "bb")]),
                         wso.tree_digest([(r"b\c.txt", 2, "bb")]))

    def test_content_change_changes_the_digest(self):
        self.assertNotEqual(wso.tree_digest([("a.txt", 1, "aa")]),
                            wso.tree_digest([("a.txt", 1, "ab")]))

    def test_size_change_changes_the_digest(self):
        self.assertNotEqual(wso.tree_digest([("a.txt", 1, "aa")]),
                            wso.tree_digest([("a.txt", 2, "aa")]))

    def test_an_extra_file_changes_the_digest(self):
        self.assertNotEqual(wso.tree_digest([("a.txt", 1, "aa")]),
                            wso.tree_digest([("a.txt", 1, "aa"), ("b.txt", 0, "cc")]))

    def test_empty_tree_is_stable(self):
        self.assertEqual(wso.tree_digest([]), wso.tree_digest([]))


class PortableSwitchTests(SimpleTestCase):
    """MetaQuotes documents no on-disk portable marker: /portable is a per-LAUNCH property, so the task
    arguments are the authoritative signal."""

    def test_detects_the_switch(self):
        self.assertTrue(wso.portable_switch_present("/portable"))
        self.assertTrue(wso.portable_switch_present("/config:x /PORTABLE"))

    def test_absent_switch(self):
        for args in ("", None, "/config:x"):
            self.assertFalse(wso.portable_switch_present(args))

    def test_does_not_match_a_substring(self):
        """'/portablex' is a different switch; a substring test would accept it."""
        self.assertFalse(wso.portable_switch_present("/portablex"))


class PathComparisonTests(SimpleTestCase):
    def test_containment_is_boundary_safe(self):
        root = r"C:\GuvFX\beta\slots\1"
        self.assertTrue(wso.is_beneath_path(root + r"\terminal", root))
        self.assertTrue(wso.is_beneath_path(root, root))
        self.assertFalse(wso.is_beneath_path(r"C:\GuvFX\beta\slots\10\terminal", root))

    def test_separator_and_case_insensitive(self):
        self.assertTrue(wso.paths_equal("C:/GuvFX/Beta", r"c:\guvfx\beta"))
        self.assertTrue(wso.paths_equal(r"C:\GuvFX\beta\\", r"C:\GuvFX\beta"))


class NoForbiddenDependencyTests(SimpleTestCase):
    @staticmethod
    def _imports(module):
        import ast
        tree = ast.parse(open(os.path.join(_BUNDLE, module), encoding="utf-8").read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        return names

    def test_shutil_is_never_imported_anywhere_in_the_bundle(self):
        """shutil.move catches every OSError from os.rename and falls back to copytree+rmtree, turning a
        tombstone into the copy-and-delete the design forbids. Checked across the WHOLE bundle, because the
        legacy B2 adapter had exactly this defect until the research surfaced it."""
        for module in sorted(f for f in os.listdir(_BUNDLE) if f.endswith(".py")):
            self.assertNotIn("shutil", self._imports(module), module)

    def test_psutil_is_never_imported(self):
        """psutil's create_time() is a float of seconds since 1970; 58% of FILETIME ticks do not
        round-trip through it, so it cannot carry process-birth identity."""
        self.assertNotIn("psutil", self._imports("win_slot_ops.py"))

    def test_move_uses_os_rename_which_cannot_degrade_to_copy(self):
        source = inspect.getsource(wso.RealSlotWindowsOps.move_dir)
        self.assertIn("os.rename", source)


class ProcessAttributionScopeTests(SimpleTestCase):
    """The unattributable guard must be scoped to processes that could BE the slot's runtime.

    Unscoped, one protected process anywhere on the host (a security product, PID 4) would make "this slot
    is empty" permanently unprovable, so STOP and TOMBSTONE could never succeed. Scoped too loosely and a
    live runtime gets reported absent. The name set from the slot tree is what separates the two.
    """

    def test_absent_slot_directory_means_no_process_without_touching_win32(self):
        class NoSlot(wso.RealSlotWindowsOps):
            def path_exists(self, path): return False
        adapter = NoSlot(golden_dir="g", slots_root="s")
        self.assertIsNone(adapter.query_slot_process(r"C:\GuvFX\beta\slots\1\terminal"))

    def test_the_scope_is_the_slot_identity_not_the_executable_name(self):
        """A materialised slot is a copy of MT5, so it contains terminal64.exe - the SAME name as the
        operator's production terminal. Scoping by name cannot separate the two; scoping by the slot's
        fixed identity SID puts the operator's estate out of scope by construction."""
        source = inspect.getsource(wso.RealSlotWindowsOps.query_slot_process)
        self.assertIn("_identity_sid", source)
        self.assertIn("sid != expected", source)
        self.assertNotIn("slot_names", source)

    def test_an_unresolvable_identity_raises_rather_than_matching_nothing(self):
        """An empty scope would match no process and report every slot empty."""
        with self.assertRaises(WindowsOpsError) as ctx:
            _adapter()._identity_sid("")
        self.assertEqual(ctx.exception.reason_code, "runtime_identity_required")

    def test_every_non_gone_state_counts_within_the_identity_scope(self):
        """Only 'denied' used to count, silently dropping 'unknown' - the reachable winerrors the suite
        itself lists (0, 6, 8, 299, 1450) all classify as unknown."""
        source = inspect.getsource(wso.RealSlotWindowsOps.query_slot_process)
        self.assertIn('state != "gone"', source)

    def test_open_process_returns_its_verdict_instead_of_stashing_it(self):
        """One adapter instance is shared by concurrent requests, so an instance attribute used as an
        out-parameter can be overwritten between write and read."""
        self.assertFalse(hasattr(wso.RealSlotWindowsOps, "_last_open_state"))
        self.assertIn("return None, classify_open_process_error",
                      inspect.getsource(wso.RealSlotWindowsOps._open_process))

    def test_denial_translation_exists_for_the_com_surface(self):
        """Stages branch on PermissionError specifically; without translation an ACL misconfiguration is
        filed as a retryable host fault and three reason codes are unreachable."""
        self.assertIn("PermissionError", inspect.getsource(wso.translate_denial))
        for method in (wso.RealSlotWindowsOps._folder, wso.RealSlotWindowsOps._registered_task,
                       wso.RealSlotWindowsOps.run_task):
            self.assertIn("translate_denial", inspect.getsource(method), method.__name__)

    def test_robocopy_failure_uses_a_classified_fixed_reason_code(self):
        """An interpolated reason code cannot be classified and is invisible to the AST test that proves
        every reason code maps to a category."""
        import lifecycle as lc
        source = inspect.getsource(wso.RealSlotWindowsOps.copy_golden)
        self.assertIn('WindowsOpsError("golden_copy_failed")', source)
        self.assertTrue(lc.is_classified("golden_copy_failed"))

    def test_the_tree_digest_root_must_be_readable(self):
        """os.walk's default onerror swallows every scandir error, so an unreadable root digested to
        sha256(b'') and an unreadable subtree was silently omitted while the digest still 'matched'."""
        source = inspect.getsource(wso.RealSlotWindowsOps._tree_digest)
        self.assertIn("os.lstat(root)", source)
        self.assertIn("onerror=_reraise", source)


# ── WS-A / WS-B follow-up: process-observation + open-handle DECISION logic ──────────────────────────────
# The Win32 primitives are host-proven during APPLY. Here the logic ON TOP of them is exercised with fakes
# that model the REAL primitive contracts (``_open_process`` -> (handle, "ok"|"denied"|"gone"); the
# enumerator yields (pid, name, sid) incl. a null-SID system row), so a test cannot pass against a weaker
# behaviour than the box exposes.
import win_primitives as wp   # noqa: E402

_SLOT1_SID = "S-1-5-21-2216203845-1747098376-1637942580-1004"   # guvfx_b_slot1 (host-observed)
_SLOT2_SID = "S-1-5-21-2216203845-1747098376-1637942580-1005"
_ADMIN_SID = "S-1-5-21-2216203845-1747098376-1637942580-500"
_SLOT1_DIR = r"C:\GuvFX\beta\slots\1\terminal"


class _FakeK32:
    def CloseHandle(self, h):
        return True


class _FakeSlotOps(wso.RealSlotWindowsOps):
    """RealSlotWindowsOps with only the leaf Win32 primitives faked; the scoping / containment / select /
    fail-closed logic of query_slot_process runs unchanged."""

    def __init__(self, procs, *, slot_sid=_SLOT1_SID):
        super().__init__(golden_dir=r"C:\GuvFX\golden\newMT5", slots_root=r"C:\GuvFX\beta\slots")
        self._procs = dict(procs)
        self._slot_sid = slot_sid

    def path_exists(self, path):
        return True

    def _win32(self):
        return {"k32": _FakeK32()}

    def _long_path(self, path):
        return path

    def _identity_sid(self, runtime_identity):
        return self._slot_sid

    def _enum_processes_with_owner(self):
        for pid, d in self._procs.items():
            yield int(pid), d.get("name", "terminal64.exe"), d["sid"]

    def _open_process(self, api, pid):
        st = self._procs[pid]["open"]
        return (pid, "ok") if st == "ok" else (None, st)

    def _image_path(self, api, handle):
        return self._procs[handle].get("image")

    def _creation_filetime(self, api, handle):
        return self._procs[handle].get("filetime", 133000000000000000)

    def _session_id(self, api, pid):
        return self._procs[pid].get("session", 0)

    def _file_digest(self, image):
        return "digest-stub"


def _slot_proc(pid, sid=_SLOT1_SID, image=_SLOT1_DIR + r"\terminal64.exe", session=0, **kw):
    d = {"sid": sid, "name": "terminal64.exe", "open": "ok", "image": image, "session": session}
    d.update(kw)
    return (pid, d)


class ProcessObservationTests(SimpleTestCase):
    """WS-A required scenarios against query_slot_process (the fixed WTSEnumerateProcesses path)."""

    def q(self, procs, slot_sid=_SLOT1_SID):
        return _FakeSlotOps(procs, slot_sid=slot_sid).query_slot_process(_SLOT1_DIR, "guvfx_b_slot1")

    def test_no_matching_process_returns_absent(self):
        procs = dict([_slot_proc(4336, sid=_ADMIN_SID,
                      image=r"C:\Program Files\IS6 Technologies MT5 Terminal\terminal64.exe")])
        procs[0] = {"sid": "", "name": "System Idle", "open": "gone"}      # null-SID system row is skipped
        self.assertIsNone(self.q(procs))

    def test_one_correct_slot_process_is_identified(self):
        r = self.q(dict([_slot_proc(8200)]))
        self.assertEqual(8200, r["pid"])
        self.assertEqual(_SLOT1_SID, r["user_sid"])
        self.assertEqual(0, r["session_id"])                               # Session 0 process observable
        self.assertEqual(133000000000000000, r["created_at_filetime"])     # start-time evidence carried

    def test_production_terminal_same_name_is_excluded(self):
        procs = dict([_slot_proc(8200), _slot_proc(4336, sid=_ADMIN_SID, session=3,
                      image=r"C:\Program Files\IS6 Technologies MT5 Terminal\terminal64.exe")])
        self.assertEqual(8200, self.q(procs)["pid"])

    def test_another_slots_process_is_excluded(self):
        self.assertIsNone(self.q(dict([_slot_proc(9000, sid=_SLOT2_SID,
                          image=r"C:\GuvFX\beta\slots\2\terminal\terminal64.exe")])))

    def test_wrong_owner_is_excluded(self):
        self.assertIsNone(self.q(dict([_slot_proc(8200, sid=_ADMIN_SID)])))

    def test_wrong_path_same_owner_is_excluded(self):
        self.assertIsNone(self.q(dict([_slot_proc(8200, image=r"C:\GuvFX\beta\slots\1\evil\terminal64.exe")])))

    def test_slot10_prefix_does_not_match_slot1(self):
        self.assertIsNone(self.q(dict([_slot_proc(8200,
                          image=r"C:\GuvFX\beta\slots\10\terminal\terminal64.exe")])))

    def test_permission_failure_is_unavailable_not_absent(self):
        with self.assertRaises(WindowsOpsError) as cm:
            self.q(dict([_slot_proc(8200, open="denied")]))
        self.assertEqual("process_attribution_incomplete", cm.exception.reason_code)

    def test_gone_process_is_benign_absent(self):
        self.assertIsNone(self.q(dict([_slot_proc(8200, open="gone")])))

    def test_unreadable_image_owned_by_us_is_unavailable(self):
        with self.assertRaises(WindowsOpsError):
            self.q(dict([_slot_proc(8200, image=None)]))

    def test_multiple_ambiguous_matches_fail_closed(self):
        procs = dict([_slot_proc(8200, image=_SLOT1_DIR + r"\MQL5\a.exe"),
                      _slot_proc(8201, image=_SLOT1_DIR + r"\MQL5\b.exe")])
        with self.assertRaises(WindowsOpsError):
            self.q(procs)

    def test_canonical_terminal_wins_over_child(self):
        procs = dict([_slot_proc(8200, image=_SLOT1_DIR + r"\terminal64.exe"),
                      _slot_proc(4948, image=_SLOT1_DIR + r"\MetaEditor64.exe")])
        self.assertEqual(8200, self.q(procs)["pid"])

    def test_observe_process_wrapper_absent_and_valid(self):
        si = wp.resolve_slot_input(1)
        absent = wp.observe_process(_FakeSlotOps({}), si)
        self.assertEqual("process_absent", absent["attestation"]["reason_code"])
        present = wp.observe_process(_FakeSlotOps(dict([_slot_proc(8200)])), si)
        self.assertEqual("", present["attestation"]["reason_code"])
        self.assertEqual(8200, present["evidence"]["pid"])

    def test_non_integer_creation_time_is_rejected(self):
        si = wp.resolve_slot_input(1)
        bad = wp.observe_process(_FakeSlotOps(dict([_slot_proc(8200, filetime=None)])), si)
        self.assertEqual("creation_time_unusable", bad["attestation"]["reason_code"])


class OpenHandlesLogicTests(SimpleTestCase):
    """WS-B fail-closed guards that run BEFORE the host-only Restart Manager call."""

    def _ops(self, *, exists=True, files=None):
        class OH(_FakeSlotOps):
            def path_exists(self, path):
                return exists
            def _enumerate_slot_files(self, canonical_root):
                if files is None:
                    raise AssertionError("enumeration must not be reached in this test")
                return list(files)
        return OH({})

    def test_path_outside_slots_root_fails_closed(self):
        with self.assertRaises(WindowsOpsError) as cm:
            self._ops(files=[]).open_handles(r"C:\Windows\System32")
        self.assertEqual("open_handles_path_outside_slots_root", cm.exception.reason_code)

    def test_production_tree_is_never_inspected(self):
        with self.assertRaises(WindowsOpsError) as cm:
            self._ops(files=[]).open_handles(r"C:\Program Files\IS6 Technologies MT5 Terminal")
        self.assertEqual("open_handles_path_outside_slots_root", cm.exception.reason_code)

    def test_missing_slot_dir_is_clear(self):
        self.assertFalse(self._ops(exists=False, files=[]).open_handles(_SLOT1_DIR))

    def test_empty_tree_cannot_prove_clear_fails_closed(self):
        with self.assertRaises(WindowsOpsError) as cm:
            self._ops(exists=True, files=[]).open_handles(_SLOT1_DIR)
        self.assertEqual("handle_observation_unavailable", cm.exception.reason_code)

    def test_with_files_control_reaches_host_only_rm_and_is_unavailable_off_host(self):
        ops = self._ops(exists=True, files=[_SLOT1_DIR + r"\terminal64.exe"])
        with self.assertRaises(wso.WindowsApiUnavailable):
            ops.open_handles(_SLOT1_DIR)


import shutil as _shutil    # noqa: E402
import tempfile as _tempfile   # noqa: E402


class EnumerateSlotFilesRealTests(SimpleTestCase):
    """Exercise the REAL _enumerate_slot_files (its reparse rejection) against a real temp tree — the fake
    overrides used elsewhere hid it (adversarial review WS-A/B #6/#7). Only _long_path is faked (identity)
    so the walk runs off-host; the reparse guard itself runs unchanged."""

    def setUp(self):
        self.tmp = _tempfile.mkdtemp()
        self.ops = _FakeSlotOps({})    # _long_path is identity in the fake

    def tearDown(self):
        _shutil.rmtree(self.tmp, ignore_errors=True)

    def test_regular_files_are_enumerated(self):
        f1 = os.path.join(self.tmp, "terminal64.exe"); open(f1, "w").close()
        sub = os.path.join(self.tmp, "MQL5"); os.mkdir(sub)
        f2 = os.path.join(sub, "x.ex5"); open(f2, "w").close()
        self.assertEqual(sorted(self.ops._enumerate_slot_files(self.tmp)), sorted([f1, f2]))

    def test_child_symlink_is_rejected_fail_closed(self):
        open(os.path.join(self.tmp, "a.txt"), "w").close()
        try:
            os.symlink(os.path.dirname(self.tmp), os.path.join(self.tmp, "escape"))
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted here")
        with self.assertRaises(WindowsOpsError) as cm:
            self.ops._enumerate_slot_files(self.tmp)
        self.assertEqual(cm.exception.reason_code, "reparse_point_in_tree")

    def test_root_symlink_is_rejected(self):
        real = os.path.join(self.tmp, "real"); os.mkdir(real)
        link = os.path.join(self.tmp, "rootlink")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted here")
        with self.assertRaises(WindowsOpsError) as cm:
            self.ops._enumerate_slot_files(link)
        self.assertEqual(cm.exception.reason_code, "reparse_point_in_tree")


class OpenHandlesOrderingTests(SimpleTestCase):
    """open_handles must test existence BEFORE canonicalising, or verify_cleanup's post-move call raises
    (adversarial review #1/#3). Modelled with a _long_path that fails like GetLongPathNameW on a gone path."""

    class _Ops(_FakeSlotOps):
        def __init__(self, exists):
            super().__init__({})
            self._exists = exists
        def path_exists(self, path):
            return self._exists
        def _long_path(self, path):
            raise wso.WindowsOpsError("path_normalisation_failed")   # GetLongPathNameW on a missing path
        def _enumerate_slot_files(self, c):
            return []

    def test_missing_path_returns_clear_without_canonicalising(self):
        # exists=False -> returns False BEFORE _long_path is ever called (which would have raised)
        self.assertFalse(self._Ops(exists=False).open_handles(r"C:\GuvFX\beta\slots\1\terminal"))

    def test_present_but_uncanonicalisable_still_fails_closed(self):
        with self.assertRaises(WindowsOpsError) as cm:
            self._Ops(exists=True).open_handles(r"C:\GuvFX\beta\slots\1\terminal")
        self.assertEqual(cm.exception.reason_code, "path_normalisation_failed")


class WsAbSourceInvariantTests(SimpleTestCase):
    """Source-level pins for the primitives the fakes cannot exercise (review #5/#8/#9): the exact WTS call,
    the RM fail-closed branches, the existence-before-canon order, and the reparse-dereferencing scope guard."""

    def _src(self):
        return open(os.path.join(_BUNDLE, "win_slot_ops.py"), encoding="utf-8").read()

    def test_wts_enumerate_uses_host_proven_args(self):
        # HOST-PROVEN (positive+negative control 2026-07-24): (…,1,0) returns valid rows on the target
        # pywin32; (…,0,1) raises error 87. Pin the working call and forbid the unavailable Ex variant.
        s = self._src()
        self.assertIn("win32ts.WTSEnumerateProcesses(win32ts.WTS_CURRENT_SERVER_HANDLE, 1, 0)", s)
        self.assertNotIn("WTSEnumerateProcessesEx(", s)
        self.assertNotIn("WTSEnumerateProcesses(win32ts.WTS_CURRENT_SERVER_HANDLE, 0, 1)", s)

    def test_open_handles_rm_failures_raise_never_clear(self):
        s = self._src()
        self.assertIn("return needed.value > 0", s)               # result is a real count, not a hard-coded bool
        self.assertGreaterEqual(
            s.count('raise WindowsOpsError("handle_observation_unavailable") from RuntimeError'), 3)

    def test_open_handles_checks_existence_before_canonicalising(self):
        s = self._src()
        head = s[s.index("def open_handles(self, path: str)"):][:3000]   # the method body fits this window
        self.assertLess(head.index("if not self.path_exists(path):"),
                        head.index("canonical = self._long_path(path)"))

    def test_open_handles_scope_guard_dereferences_reparse(self):
        s = self._src()
        self.assertIn("os.path.realpath(path)", s)                # junction at the slot root resolves out of scope
        self.assertIn("os.path.realpath(self.slots_root)", s)
