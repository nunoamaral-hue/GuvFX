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

    def test_open_handles_fails_closed_everywhere(self):
        """Not an off-host limitation: there is NO supported way to prove this on Windows either."""
        with self.assertRaises(WindowsOpsError) as ctx:
            _adapter().open_handles(r"C:\GuvFX\beta\slots\1\terminal")
        self.assertEqual(ctx.exception.reason_code, "handle_enumeration_unsupported")


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

    def test_the_scope_is_derived_from_the_slot_tree(self):
        source = inspect.getsource(wso.RealSlotWindowsOps.query_slot_process)
        self.assertIn("slot_names", source)
        self.assertIn("name.lower() in slot_names", source)

    def test_every_non_gone_state_counts_when_the_name_matches(self):
        """Previously only 'denied' counted, silently dropping 'unknown' - the reachable winerrors the
        suite itself lists (0, 6, 8, 299, 1450) all classify as unknown."""
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
