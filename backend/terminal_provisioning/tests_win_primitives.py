"""CVM-Inc-3 B3P-2 — READ-ONLY Windows primitive tests (stages 1-3).

Proves: immutable primitive input; minimal local attestation carrying no occupancy identity; explicit
machine-readable time; mechanically read-only behaviour (a recording fake fails the test on ANY attempted
side effect); absence distinguished from invalidity and from unavailability; and that the resolver cannot
address Nuno's production estate.
"""
import os
import sys

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "beta-agent")
if _BUNDLE not in sys.path:
    sys.path.insert(0, _BUNDLE)

import win_primitives as wp   # noqa: E402

OBSERVED_AT = "2026-07-22T09:00:00Z"
FILETIME = 133_000_000_000_000_000        # 100-ns ticks — stable, machine-readable


class RecordingFakeWin:
    """Read/write Windows adapter that RECORDS every mutating attempt instead of performing it.

    Any non-empty ``side_effects`` after a read-only primitive runs is a test failure — this is how
    "read-only" is proven mechanically rather than by reading the code.
    """

    def __init__(self, *, task=None, process=None, paths=None, acl=None,
                 task_error=None, process_error=None, path_error=None, acl_error=None):
        self.side_effects = []
        self._task, self._process = task, process
        self._paths = paths or {}
        self._acl = acl
        self._task_error, self._process_error = task_error, process_error
        self._path_error, self._acl_error = path_error, acl_error

    # ---- read surface ----
    def query_task(self, name):
        if self._task_error:
            raise self._task_error
        return self._task

    def query_slot_process(self, slot_path, identity=""):
        if self._process_error:
            raise self._process_error
        return self._process

    def path_exists(self, path):
        if self._path_error:
            raise self._path_error
        return path in self._paths

    def real_path(self, path):
        return self._paths.get(path)

    def read_acl(self, path):
        if self._acl_error:
            raise self._acl_error
        return self._acl

    # ---- write surface: recorded, never performed ----
    def make_dirs(self, path): self.side_effects.append(("make_dirs", path))
    def copy_golden(self, path): self.side_effects.append(("copy_golden", path))
    def write_owner_tag(self, path, v): self.side_effects.append(("write_owner_tag", path))
    def move_dir(self, a, b): self.side_effects.append(("move_dir", a, b))
    def stop_pid(self, pid): self.side_effects.append(("stop_pid", pid))
    def run_task(self, name): self.side_effects.append(("run_task", name))
    def end_task(self, name): self.side_effects.append(("end_task", name))
    def set_acl(self, path, acl): self.side_effects.append(("set_acl", path))
    def register_task(self, defn): self.side_effects.append(("register_task", defn))
    def open_for_write(self, path): self.side_effects.append(("open_for_write", path))


#: A well-formed service SID (S-1-5-80- namespace), as the launch grantee. The gate asserts SHAPE only.
_GRANTEE_SID = "S-1-5-80-3139157870-2983391045-3678747466-658725712-1809340420"


def _task(**over):
    # ADR-0016 launch shape: powershell.exe runs the fixed wrapper AS the slot identity (the wrapper launches
    # this slot's terminal64 and grants the service query access). portable_switch is computed by the real
    # query_task from the bare /portable token; the fake dict carries it directly.
    d = dict(task_name="GuvFXBetaRuntime-2", run_as_identity="guvfx_u_beta_2",
             run_as_sid="S-1-5-21-x-1002",
             executable="powershell.exe",
             working_directory=r"C:\GuvFX\beta\slots\2\terminal",
             arguments=(
                 '-NoProfile -NonInteractive -ExecutionPolicy Bypass '
                 '-File "C:\\GuvFX\\beta\\launcher\\slot_launch.ps1" '
                 '-TerminalPath "C:\\GuvFX\\beta\\slots\\2\\terminal\\terminal64.exe" '
                 '-WorkingDirectory "C:\\GuvFX\\beta\\slots\\2\\terminal" '
                 '-GranteeSid ' + _GRANTEE_SID + ' /portable'),
             portable_switch=True,
             logon_type="TASK_LOGON_PASSWORD", run_level="LEAST", enabled=True, last_result=0)
    d.update(over)
    return d


def _proc(**over):
    d = dict(pid=13020, created_at_filetime=FILETIME,
             image=r"C:\GuvFX\beta\slots\2\terminal\terminal64.exe", image_digest="abc123",
             user_sid="S-1-5-21-x-1002", session_id=1)
    d.update(over)
    return d


def _paths(exists=True, reparse=None):
    p = {}
    if exists:
        for path in (wp.BETA_SLOTS_ROOT, r"C:\GuvFX\beta\slots\2", r"C:\GuvFX\beta\slots\2\terminal"):
            p[path] = path
    if reparse:
        p.update(reparse)
    return p


SI = None


def setUpModule():
    global SI
    SI = wp.resolve_slot_input(2)


class ImmutableInputTests(SimpleTestCase):
    """Requirement 1: a primitive cannot mutate its input, and cannot be mutated mid-flight."""

    def test_input_is_frozen(self):
        for field, value in (("slot", 3), ("slot_path", r"C:\evil"),
                             ("launch_task", "X"), ("terminate_task", "Y")):
            with self.assertRaises(Exception, msg=field):
                setattr(SI, field, value)

    def test_upper_layer_cannot_alter_an_in_flight_view(self):
        """Defensive copy: mutating the caller's dict afterwards must not change the primitive input."""
        view = {"slot": 2, "slot_path": r"C:\GuvFX\beta\slots\2\terminal",
                "launch_task": "GuvFXBetaRuntime-2", "terminate_task": "GuvFXBetaRuntimeStop-2"}
        si = wp.SlotInput.from_scoped_view(view)
        view["slot"] = 99
        view["slot_path"] = r"C:\GuvFX\accounts\1"
        self.assertEqual(si.slot, 2)
        self.assertEqual(si.slot_path, r"C:\GuvFX\beta\slots\2\terminal")


class AttestationTests(SimpleTestCase):
    """Requirement 2: minimal local envelope, carrying no occupancy identity."""

    def test_carries_exactly_the_required_fields(self):
        a = wp.attest(slot=2, operation="observe_process", outcome=wp.PRESENT_VALID,
                      reason_code="", evidence={"pid": 1}, observed_at=OBSERVED_AT)
        self.assertEqual(set(a), set(wp.ATTESTATION_FIELDS))
        self.assertEqual(a["primitive_version"], wp.PRIMITIVE_VERSION)

    def test_never_carries_occupancy_or_tenant_identity(self):
        for res in (wp.inspect_task(RecordingFakeWin(task=_task()), SI, observed_at=OBSERVED_AT),
                    wp.observe_process(RecordingFakeWin(process=_proc()), SI, observed_at=OBSERVED_AT),
                    wp.inspect_filesystem(RecordingFakeWin(paths=_paths()), SI, observed_at=OBSERVED_AT)):
            for forbidden in wp.FORBIDDEN_ATTESTATION_FIELDS:
                self.assertNotIn(forbidden, res["attestation"], forbidden)


class TimeSourceTests(SimpleTestCase):
    """Requirement 3: stable machine-readable creation time; no locale strings; no skew inference."""

    def test_equality_on_integer_ticks(self):
        self.assertTrue(wp.creation_time_matches(FILETIME, FILETIME))
        self.assertFalse(wp.creation_time_matches(FILETIME, FILETIME + 1))

    def test_string_representations_are_refused_outright(self):
        for bad in ("2026-07-22 09:00:00", "22/07/2026 09:00", str(FILETIME)):
            self.assertFalse(wp.creation_time_matches(bad, bad))

    def test_missing_values_never_match(self):
        self.assertFalse(wp.creation_time_matches(None, FILETIME))
        self.assertFalse(wp.creation_time_matches(FILETIME, None))

    def test_process_evidence_uses_machine_readable_creation_time(self):
        res = wp.observe_process(RecordingFakeWin(process=_proc()), SI, observed_at=OBSERVED_AT)
        self.assertIsInstance(res["evidence"]["created_at_filetime"], int)

    def test_unusable_creation_time_is_invalid_not_valid(self):
        res = wp.observe_process(RecordingFakeWin(process=_proc(created_at_filetime="09:00")), SI,
                                 observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["outcome"], wp.PRESENT_INVALID)
        self.assertEqual(res["attestation"]["reason_code"], "creation_time_unusable")


class ReadOnlyEnforcementTests(SimpleTestCase):
    """Requirement 4: mechanically read-only — ANY attempted side effect fails the test."""

    def test_no_primitive_performs_a_side_effect(self):
        for name, call in (
            ("inspect_task", lambda w: wp.inspect_task(w, SI, observed_at=OBSERVED_AT)),
            ("inspect_task_terminate", lambda w: wp.inspect_task(w, SI, which="terminate",
                                                                 observed_at=OBSERVED_AT)),
            ("observe_process", lambda w: wp.observe_process(w, SI, observed_at=OBSERVED_AT)),
            ("inspect_filesystem", lambda w: wp.inspect_filesystem(w, SI, observed_at=OBSERVED_AT)),
        ):
            win = RecordingFakeWin(task=_task(), process=_proc(), paths=_paths())
            call(win)
            self.assertEqual(win.side_effects, [], f"{name} performed a side effect")

    def test_no_side_effect_even_on_absent_or_failing_observations(self):
        for win in (RecordingFakeWin(),                                          # everything absent
                    RecordingFakeWin(task_error=PermissionError()),
                    RecordingFakeWin(process_error=OSError("wmi down")),
                    RecordingFakeWin(path_error=PermissionError())):
            wp.inspect_task(win, SI, observed_at=OBSERVED_AT)
            wp.observe_process(win, SI, observed_at=OBSERVED_AT)
            wp.inspect_filesystem(win, SI, observed_at=OBSERVED_AT)
            self.assertEqual(win.side_effects, [])


class AbsenceIsNotSuccessTests(SimpleTestCase):
    """Requirement 5: absent / invalid / unavailable / denied are five distinct states."""

    def test_missing_task_is_absent_not_invalid(self):
        res = wp.inspect_task(RecordingFakeWin(task=None), SI, observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["outcome"], wp.ABSENT)
        self.assertEqual(res["attestation"]["reason_code"], "task_absent")

    def test_task_query_failure_is_unavailable_not_absent(self):
        res = wp.inspect_task(RecordingFakeWin(task_error=OSError("rpc")), SI, observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["outcome"], wp.UNAVAILABLE)
        self.assertEqual(res["attestation"]["reason_code"], "task_observation_unavailable")

    def test_task_permission_denied_is_its_own_state(self):
        res = wp.inspect_task(RecordingFakeWin(task_error=PermissionError()), SI,
                              observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["outcome"], wp.PERMISSION_DENIED)

    def test_process_query_failure_is_not_process_absent(self):
        res = wp.observe_process(RecordingFakeWin(process_error=OSError("wmi")), SI,
                                 observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["outcome"], wp.UNAVAILABLE)
        self.assertEqual(res["attestation"]["reason_code"], "process_observation_unavailable")

    def test_absent_process_is_absent(self):
        res = wp.observe_process(RecordingFakeWin(process=None), SI, observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["reason_code"], "process_absent")

    def test_unmaterialised_slot_is_absent_not_a_containment_failure(self):
        paths = {wp.BETA_SLOTS_ROOT: wp.BETA_SLOTS_ROOT, r"C:\GuvFX\beta\slots\2": r"C:\GuvFX\beta\slots\2"}
        res = wp.inspect_filesystem(RecordingFakeWin(paths=paths), SI, observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["outcome"], wp.ABSENT)
        self.assertEqual(res["attestation"]["reason_code"], "terminal_path_absent")

    def test_all_observation_states_are_distinct(self):
        # ADR-0015 added MULTIPLE_MATCHING as a sixth distinct fail-closed state.
        self.assertEqual(len(set(wp.OBSERVATION_STATUSES)), 6)
        self.assertIn(wp.MULTIPLE_MATCHING, wp.OBSERVATION_STATUSES)
        self.assertNotEqual(wp.MULTIPLE_MATCHING, wp.UNAVAILABLE)     # never collapsed into unavailable


class TaskInspectionContractTests(SimpleTestCase):
    """Acceptance criteria for stage 1."""

    def test_valid_task_returns_every_required_field(self):
        res = wp.inspect_task(RecordingFakeWin(task=_task()), SI, observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["outcome"], wp.PRESENT_VALID)
        for f in ("task_name", "definition_digest", "run_as_identity", "run_as_sid", "executable",
                  "working_directory", "arguments", "portable_switch", "logon_type", "run_level",
                  "enabled", "last_result"):
            self.assertIn(f, res["evidence"], f)

    def test_administrator_run_as_is_invalid(self):
        res = wp.inspect_task(RecordingFakeWin(task=_task(run_as_identity="Administrator")), SI,
                              observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["reason_code"], "forbidden_run_as_identity")

    def test_launch_executable_that_is_not_powershell_is_invalid(self):
        # ADR-0016: the launch task runs powershell.exe against the fixed wrapper. Any other executable
        # (here cmd.exe) means the task no longer runs the reviewed launcher -> launch_executable_unexpected.
        res = wp.inspect_task(
            RecordingFakeWin(task=_task(executable=r"C:\Windows\System32\cmd.exe")), SI,
            observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["reason_code"], "launch_executable_unexpected")

    def test_launch_args_not_naming_the_fixed_wrapper_are_invalid(self):
        # The args must invoke the FIXED wrapper via -File. An args string that runs a different script (or
        # inline code) is refused before the launch can grant anything.
        res = wp.inspect_task(
            RecordingFakeWin(task=_task(arguments='-File "C:\\evil\\other.ps1" -GranteeSid '
                                        + _GRANTEE_SID + ' /portable', portable_switch=True)),
            SI, observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["reason_code"], "launch_wrapper_unscoped")

    def _launch_args(self, terminal, grantee=_GRANTEE_SID, portable=" /portable"):
        return ('-NoProfile -NonInteractive -ExecutionPolicy Bypass '
                '-File "C:\\GuvFX\\beta\\launcher\\slot_launch.ps1" '
                '-TerminalPath "%s" '
                '-WorkingDirectory "C:\\GuvFX\\beta\\slots\\2\\terminal" '
                '-GranteeSid %s%s' % (terminal, grantee, portable))

    def test_launch_targeting_another_slots_terminal_is_scope_unbounded(self):
        # Prefix-safe: slot 2's gate must reject args naming slot 20's terminal64 (or any non-slot-2 path).
        args = self._launch_args(r"C:\GuvFX\beta\slots\20\terminal\terminal64.exe")
        res = wp.inspect_task(RecordingFakeWin(task=_task(arguments=args, portable_switch=True)), SI,
                              observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["reason_code"], "launch_scope_unbounded")

    def test_launch_without_a_service_sid_grantee_is_refused(self):
        # No S-1-5-80- grantee token -> the launch would not grant. Assert SHAPE only (a broad SID like the
        # Everyone S-1-1-0 is not a service SID and is rejected).
        args = self._launch_args(r"C:\GuvFX\beta\slots\2\terminal\terminal64.exe", grantee="S-1-1-0")
        res = wp.inspect_task(RecordingFakeWin(task=_task(arguments=args, portable_switch=True)), SI,
                              observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["reason_code"], "launch_grantee_missing")

    def test_launch_that_is_not_portable_is_refused(self):
        args = self._launch_args(r"C:\GuvFX\beta\slots\2\terminal\terminal64.exe", portable="")
        res = wp.inspect_task(RecordingFakeWin(task=_task(arguments=args, portable_switch=False)), SI,
                              observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["reason_code"], "launch_not_portable")

    def test_launch_carrying_an_inline_command_is_refused(self):
        # -File is the only permitted code path; an inline -Command / -EncodedCommand is refused even if the
        # rest of the args look valid. powershell.exe resolves prefix abbreviations (-com, -en) and the -ec
        # alias, so ALL of these must be caught.
        for switch in ("-Command", "-command", "-c", "-com", "-comm",
                       "-EncodedCommand", "-enc", "-en", "-e", "-ec"):
            args = self._launch_args(r"C:\GuvFX\beta\slots\2\terminal\terminal64.exe") + ' ' + switch + ' "x"'
            res = wp.inspect_task(RecordingFakeWin(task=_task(arguments=args, portable_switch=True)), SI,
                                  observed_at=OBSERVED_AT)
            self.assertEqual(res["attestation"]["reason_code"], "launch_inline_command", switch)

    def test_execution_policy_and_valid_switches_are_not_mistaken_for_inline_commands(self):
        # -ExecutionPolicy (and its -ex abbreviations) must NEVER be caught by the inline-command guard: it is
        # part of every legitimate wrapper invocation. A fully valid task must pass.
        self.assertFalse(wp._is_inline_command_switch("-ExecutionPolicy"))
        self.assertFalse(wp._is_inline_command_switch("-ex"))
        self.assertFalse(wp._is_inline_command_switch("-exec"))
        self.assertFalse(wp._is_inline_command_switch("-File"))
        args = self._launch_args(r"C:\GuvFX\beta\slots\2\terminal\terminal64.exe")
        res = wp.inspect_task(RecordingFakeWin(task=_task(arguments=args, portable_switch=True)), SI,
                              observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["outcome"], wp.PRESENT_VALID)

    def test_a_fully_valid_launch_wrapper_task_passes(self):
        args = self._launch_args(r"C:\GuvFX\beta\slots\2\terminal\terminal64.exe")
        res = wp.inspect_task(RecordingFakeWin(task=_task(arguments=args, portable_switch=True)), SI,
                              observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["outcome"], wp.PRESENT_VALID)

    def test_incomplete_definition_is_present_invalid(self):
        res = wp.inspect_task(RecordingFakeWin(task=_task(logon_type=None)), SI, observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["outcome"], wp.PRESENT_INVALID)


class ProcessObservationContractTests(SimpleTestCase):
    """Acceptance criteria for stage 2."""

    def test_returns_every_required_field(self):
        res = wp.observe_process(RecordingFakeWin(process=_proc()), SI, observed_at=OBSERVED_AT)
        for f in ("pid", "created_at_filetime", "image", "image_digest",
                  "image_containment_verified", "user_sid", "session_id"):
            self.assertIn(f, res["evidence"], f)
        self.assertTrue(res["evidence"]["image_containment_verified"])

    def test_image_outside_the_slot_fails_containment(self):
        res = wp.observe_process(
            RecordingFakeWin(process=_proc(image=r"C:\Program Files\IS6 Technologies MT5 Terminal\terminal64.exe")),
            SI, observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["reason_code"], "image_outside_slot")
        self.assertFalse(res["evidence"]["image_containment_verified"])


class FilesystemInspectionContractTests(SimpleTestCase):
    """Acceptance criteria for stage 3."""

    def test_reports_every_component_and_containment(self):
        res = wp.inspect_filesystem(RecordingFakeWin(paths=_paths(), acl="ACL"), SI,
                                    observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["outcome"], wp.PRESENT_VALID)
        self.assertEqual(len(res["evidence"]["components"]), 3)
        self.assertTrue(res["evidence"]["slot_root_exists"])
        self.assertTrue(res["evidence"]["terminal_path_exists"])
        self.assertTrue(res["evidence"]["containment_verified"])

    def test_reparse_point_on_any_component_is_invalid(self):
        paths = _paths()
        paths[r"C:\GuvFX\beta\slots\2"] = r"C:\GuvFX\beta\slots\2"          # ancestor…
        paths[r"C:\GuvFX\beta\slots\2\terminal"] = r"C:\GuvFX\beta\slots\9\terminal"   # …leaf redirected
        res = wp.inspect_filesystem(RecordingFakeWin(paths=paths), SI, observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["outcome"], wp.PRESENT_INVALID)
        self.assertEqual(res["attestation"]["reason_code"], "reparse_point_present")

    def test_reparse_escaping_the_namespace_is_reported_distinctly(self):
        paths = _paths()
        paths[r"C:\GuvFX\beta\slots\2\terminal"] = r"C:\GuvFX\accounts\1"
        res = wp.inspect_filesystem(RecordingFakeWin(paths=paths), SI, observed_at=OBSERVED_AT)
        self.assertEqual(res["attestation"]["reason_code"], "reparse_escapes_namespace")

    def test_unreadable_acl_is_reported_not_fatal(self):
        res = wp.inspect_filesystem(RecordingFakeWin(paths=_paths(), acl_error=OSError()), SI,
                                    observed_at=OBSERVED_AT)
        self.assertTrue(all(c["acl_observation_failed"] for c in res["evidence"]["components"]))

    def test_evidence_contains_no_raw_paths(self):
        """Bounded + sanitised: components are identified by digest, not by reproducing the layout."""
        import json as _json
        res = wp.inspect_filesystem(RecordingFakeWin(paths=_paths()), SI, observed_at=OBSERVED_AT)
        self.assertNotIn("C:\\\\GuvFX", _json.dumps(res["evidence"]))


class ProductionExclusionTests(SimpleTestCase):
    """Requirement 6: the resolver cannot address Nuno's production estate."""

    def test_resolver_derives_only_from_the_beta_slot_namespace(self):
        for slot in (1, 2, 5, 99):
            si = wp.resolve_slot_input(slot)
            self.assertTrue(si.slot_path.startswith(wp.BETA_SLOTS_ROOT))
            self.assertTrue(si.launch_task.startswith("GuvFXBetaRuntime-"))

    def test_production_paths_are_refused(self):
        for path in (r"C:\GuvFX\accounts\1\terminal",
                     r"C:\Program Files\IS6 Technologies MT5 Terminal",
                     r"C:\GuvFX\terminals\1",
                     r"C:\GuvFX\mt5_signal_bridge.py",
                     r"C:\GuvFX\beta\slots\..\..\accounts"):
            with self.assertRaises(wp.UnauthorisedNamespace, msg=path):
                wp.SlotInput.from_scoped_view(
                    {"slot": 1, "slot_path": path, "launch_task": "GuvFXBetaRuntime-1",
                     "terminate_task": "GuvFXBetaRuntimeStop-1"})

    def test_production_task_names_are_refused(self):
        for task in ("GuvFX_Autostart", "GuvFX_SignalBridge", "GuvFX_BridgeWatchdog",
                     "GuvFX_LaunchMT5", "GFX_LaunchIS6"):
            with self.assertRaises(wp.UnauthorisedNamespace, msg=task):
                wp.SlotInput.from_scoped_view(
                    {"slot": 1, "slot_path": r"C:\GuvFX\beta\slots\1\terminal",
                     "launch_task": task, "terminate_task": "GuvFXBetaRuntimeStop-1"})

    def test_bridge_port_cannot_appear_in_a_slot_path(self):
        with self.assertRaises(wp.UnauthorisedNamespace):
            wp.SlotInput.from_scoped_view(
                {"slot": 1, "slot_path": r"C:\GuvFX\beta\slots\8788\terminal",
                 "launch_task": "GuvFXBetaRuntime-1", "terminate_task": "GuvFXBetaRuntimeStop-1"})

    def test_administrator_identity_is_refused_by_task_inspection(self):
        for identity in ("Administrator", "SYSTEM", "guvfx-rdp"):
            res = wp.inspect_task(RecordingFakeWin(task=_task(run_as_identity=identity)), SI,
                                  observed_at=OBSERVED_AT)
            self.assertEqual(res["attestation"]["reason_code"], "forbidden_run_as_identity", identity)

    def test_no_primitive_accepts_a_session_target(self):
        """Sessions are never an input: a primitive observes a session, it cannot request one."""
        import inspect
        for fn in (wp.inspect_task, wp.observe_process, wp.inspect_filesystem):
            self.assertNotIn("session", inspect.signature(fn).parameters)

    def test_slot_zero_or_negative_refused(self):
        for bad in (0, -1):
            with self.assertRaises(wp.UnauthorisedNamespace):
                wp.resolve_slot_input(bad)
