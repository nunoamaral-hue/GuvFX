"""Stream 7C - tests for the hosted executor's primitive runner (imports the deploy/hosted-executor bundle).

Covers the security-critical mapping: the exact argument vector built for each primitive (incl. the AppLocker
username->-HostedUser mismatch, injected -AccountId/-Mode, dropped keys), the password->stdin routing (never
argv), the ParseFile startup gate (RULE 9/11 with positive+negative controls), the JSON verdict convention, and
fail-closed handling of every malformed/unsafe input.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "hosted-executor")
_LIB = os.path.join(_BUNDLE, "lib")
for _p in (_BUNDLE, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import primitive_runner as pr  # noqa: E402

_WINDOWS_SCRIPTS = os.path.join(_REPO, "backend", "terminal_provisioning", "windows")


class RemoteAppNonDestructiveTests(unittest.TestCase):
    """Regression (Stream 7D Customer-Zero drift): ``Set-GuvfxRemoteApp.ps1 -Mode Ensure`` must create the SHARED
    ``TSAppAllowList`` / ``Applications`` registry containers ONLY when absent. ``New-Item -Force`` on an existing
    registry key deletes it and ALL its subkeys (recreates it empty) -- applied to a shared parent it wiped
    Customer Zero's ``terminal64`` alias the moment a second per-account alias was published. The shared parents
    must sit behind a ``Test-Path`` guard; only the LEAF alias key may be ``-Force``'d."""

    def _script(self):
        with open(os.path.join(_WINDOWS_SCRIPTS, "Set-GuvfxRemoteApp.ps1"), encoding="ascii") as fh:
            return fh.read()

    def test_shared_parents_are_guarded_not_force_recreated(self):
        import re
        s = self._script()
        self.assertIn("if (-not (Test-Path $TSROOT))", s)
        self.assertIn("if (-not (Test-Path $APPSROOT))", s)
        for var in ("$TSROOT", "$APPSROOT"):
            for m in re.finditer(r"New-Item\s+-Path\s+" + re.escape(var) + r"\s+-Force", s):
                prefix = s[max(0, m.start() - 80):m.start()]
                self.assertIn("Test-Path " + var, prefix,
                              "New-Item -Path %s -Force must sit inside a Test-Path guard (else it wipes "
                              "sibling RemoteApp aliases, incl. Customer Zero's terminal64)" % var)


class FakeProc:
    def __init__(self, stdout=b'{"ok":true}', returncode=0):
        self.stdout = stdout
        self.stderr = b""
        self.returncode = returncode


def _recording_runner(proc=None, **kw):
    """A PrimitiveRunner whose subprocess is recorded, not executed. parse_validator always passes."""
    calls = []

    def fake_run(argv, *, input_bytes, timeout_s):
        calls.append({"argv": list(argv), "input": input_bytes, "timeout": timeout_s})
        return proc if proc is not None else FakeProc()

    runner = pr.PrimitiveRunner(scripts_dir="/scripts", powershell="powershell",
                                run_subprocess=fake_run, parse_validator=lambda p: (True, "ok"), **kw)
    return runner, calls


class ArgVectorTests(unittest.TestCase):
    def _argv(self, primitive, args, proc=None):
        runner, calls = _recording_runner(proc=proc)
        result = runner.run(primitive, args)
        self.assertEqual(len(calls), 1, f"expected exactly one subprocess for {primitive}")
        return calls[0], result

    def test_provision_injects_accountid_and_routes_password_to_stdin(self):
        call, res = self._argv("provision_identity", {
            "username": "guvfx_u_14", "runtime_root": r"C:\GuvFX\accounts\14",
            "terminal_root": r"C:\GuvFX\accounts\14\terminal", "password": b"s3cr3t-pw"})
        argv = call["argv"]
        # fixed prefix
        self.assertEqual(argv[:6], ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"])
        self.assertTrue(argv[6].endswith("Provision-GuvfxAccount.ps1"))
        self.assertIn("-Username", argv)
        self.assertEqual(argv[argv.index("-Username") + 1], "guvfx_u_14")
        self.assertIn("-RuntimeRoot", argv)
        self.assertIn("-AccountId", argv)                          # injected from username
        self.assertEqual(argv[argv.index("-AccountId") + 1], "14")
        self.assertNotIn("-TerminalRoot", argv)                    # dropped (no such param)
        # PASSWORD: on stdin, first line; NEVER in argv or any log
        self.assertEqual(call["input"], b"s3cr3t-pw\n")
        self.assertNotIn("s3cr3t-pw", " ".join(argv))
        self.assertNotIn(b"s3cr3t-pw", b" ".join(a.encode() for a in argv))

    def test_applocker_maps_username_to_hosteduser_not_username(self):
        call, _ = self._argv("applocker_tenant_merge", {"username": "guvfx_u_9", "account_id": 9})
        argv = call["argv"]
        self.assertTrue(argv[6].endswith("Set-GuvfxAppLockerTenant.ps1"))
        self.assertIn("-HostedUser", argv)                          # the critical mismatch
        self.assertEqual(argv[argv.index("-HostedUser") + 1], "guvfx_u_9")
        self.assertNotIn("-Username", argv)
        self.assertIn("-Mode", argv)
        self.assertEqual(argv[argv.index("-Mode") + 1], "Merge")
        self.assertEqual(argv[argv.index("-AccountId") + 1], "9")

    def test_applocker_remove_uses_remove_mode(self):
        call, _ = self._argv("applocker_tenant_remove", {"username": "guvfx_u_9", "account_id": 9})
        self.assertEqual(call["argv"][call["argv"].index("-Mode") + 1], "Remove")

    def test_single_session_injects_enforce_mode(self):
        call, _ = self._argv("ensure_single_session", {})
        argv = call["argv"]
        self.assertTrue(argv[6].endswith("Set-GuvfxSingleSession.ps1"))
        self.assertEqual(argv[argv.index("-Mode") + 1], "Enforce")   # else it only verifies

    def test_remoteapp_ensure_drops_username_and_accountid(self):
        call, _ = self._argv("ensure_remoteapp", {
            "username": "guvfx_u_14", "terminal_root": r"C:\GuvFX\accounts\14\terminal",
            "alias": "guvfx_mt5_14", "account_id": 14})
        argv = call["argv"]
        self.assertEqual(argv[argv.index("-Mode") + 1], "Ensure")
        self.assertEqual(argv[argv.index("-Alias") + 1], "guvfx_mt5_14")
        self.assertEqual(argv[argv.index("-TerminalRoot") + 1], r"C:\GuvFX\accounts\14\terminal")
        self.assertNotIn("-Username", argv)
        self.assertNotIn("-AccountId", argv)

    def test_remoteapp_remove_uses_remove_mode(self):
        call, _ = self._argv("remove_remoteapp", {
            "username": "guvfx_u_14", "terminal_root": r"C:\GuvFX\accounts\14\terminal",
            "alias": "guvfx_mt5_14", "account_id": 14})
        self.assertEqual(call["argv"][call["argv"].index("-Mode") + 1], "Remove")

    def test_workspace_acl_apply_passes_mode_and_snapshot(self):
        call, _ = self._argv("apply_workspace_acl", {
            "username": "guvfx_u_14", "runtime_root": r"C:\GuvFX\accounts\14",
            "snapshot_path": r"C:\GuvFX\accounts\14\audit\acl_snapshot.sddl", "mode": "Apply"})
        argv = call["argv"]
        self.assertEqual(argv[argv.index("-Mode") + 1], "Apply")
        self.assertEqual(argv[argv.index("-SnapshotPath") + 1], r"C:\GuvFX\accounts\14\audit\acl_snapshot.sddl")

    def test_observer_injects_ensure_and_drops_terminal_root(self):
        call, _ = self._argv("prepare_observer", {
            "username": "guvfx_u_14", "runtime_root": r"C:\GuvFX\accounts\14",
            "terminal_root": r"C:\GuvFX\accounts\14\terminal"})
        argv = call["argv"]
        self.assertEqual(argv[argv.index("-Mode") + 1], "Ensure")
        self.assertNotIn("-TerminalRoot", argv)

    def test_non_provision_gets_empty_stdin(self):
        call, _ = self._argv("ensure_rdp_membership", {"username": "guvfx_u_14"})
        self.assertEqual(call["input"], b"")

    def test_never_uses_shell_string(self):
        # The recorded argv is always a LIST (argument vector); shell interpolation is impossible.
        call, _ = self._argv("ensure_rdp_membership", {"username": "guvfx_u_14"})
        self.assertIsInstance(call["argv"], list)


class GuardTests(unittest.TestCase):
    def test_unknown_primitive_fails_closed(self):
        runner, _ = _recording_runner()
        self.assertEqual(runner.run("rm_rf_everything", {}), {"ok": False, "reason": "unknown_primitive"})

    def test_verify_slot_unimplemented(self):
        runner, _ = _recording_runner()
        self.assertEqual(runner.run("verify_slot", {"username": "guvfx_u_2"})["reason"], "verify_slot_unimplemented")

    def test_account_id_underivable_from_bad_username(self):
        runner, _ = _recording_runner()
        self.assertEqual(runner.run("provision_identity",
                                    {"username": "administrator", "runtime_root": r"C:\x", "password": b"p"}),
                         {"ok": False, "reason": "account_id_underivable"})

    def test_dashed_value_refused(self):
        runner, _ = _recording_runner()
        self.assertEqual(runner.run("apply_autotrading_config", {"terminal_root": "-Force"}),
                         {"ok": False, "reason": "param_value_dashed"})

    def test_control_char_value_refused(self):
        runner, _ = _recording_runner()
        self.assertEqual(runner.run("apply_autotrading_config", {"terminal_root": "C:\\x\ninject"}),
                         {"ok": False, "reason": "param_value_control_char"})

    def test_non_scalar_value_refused(self):
        runner, _ = _recording_runner()
        self.assertEqual(runner.run("ensure_rdp_membership", {"username": ["guvfx_u_1"]})["reason"], "param_not_scalar")

    def test_script_path_rejects_traversal(self):
        # A fixed filename never contains a path separator; any that does is refused. Forward-slash is a
        # separator on every OS (backslash only on Windows), so use it for a cross-platform assertion.
        runner, _ = _recording_runner()
        for bad in ("../evil.ps1", "sub/evil.ps1", "/etc/passwd"):
            with self.assertRaises(pr.PrimitiveError):
                runner.script_path(bad)

    def test_args_not_dict(self):
        runner, _ = _recording_runner()
        self.assertEqual(runner.run("ensure_single_session", None), {"ok": False, "reason": "args_malformed"})


class VerdictTests(unittest.TestCase):
    def test_ok_true_and_zero_exit(self):
        runner, _ = _recording_runner(proc=FakeProc(b'{"ok":true,"rows":[1,2]}', 0))
        res = runner.run("apply_workspace_acl", {"username": "guvfx_u_2", "runtime_root": r"C:\GuvFX\accounts\2",
                                                 "snapshot_path": r"C:\GuvFX\accounts\2\audit\s.sddl", "mode": "Apply"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["rows"], [1, 2])

    def test_ok_true_but_nonzero_exit_is_not_ok(self):
        runner, _ = _recording_runner(proc=FakeProc(b'{"ok":true}', 1))
        self.assertFalse(runner.run("ensure_single_session", {})["ok"])

    def test_failure_reason_surfaced_from_reason_key(self):
        runner, _ = _recording_runner(proc=FakeProc(b'{"ok":false,"reason":"refusing: not hosted"}', 1))
        res = runner.run("apply_autotrading_config", {"terminal_root": r"C:\GuvFX\accounts\2\terminal"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "refusing: not hosted")

    def test_failure_reason_falls_back_to_error_key(self):
        runner, _ = _recording_runner(proc=FakeProc(b'{"ok":false,"error":"boom"}', 1))
        self.assertEqual(runner.run("ensure_rdp_membership", {"username": "guvfx_u_2"})["reason"], "boom")

    def test_non_json_output_fails_closed(self):
        runner, _ = _recording_runner(proc=FakeProc(b"not json at all", 0))
        self.assertEqual(runner.run("ensure_single_session", {}), {"ok": False, "reason": "primitive_bad_output"})

    def test_empty_output_fails_closed(self):
        runner, _ = _recording_runner(proc=FakeProc(b"", 0))
        self.assertEqual(runner.run("ensure_single_session", {}), {"ok": False, "reason": "primitive_no_output"})

    def test_last_json_line_is_parsed(self):
        runner, _ = _recording_runner(proc=FakeProc(b'WARNING: noise\n{"ok":true}', 0))
        self.assertTrue(runner.run("ensure_single_session", {})["ok"])

    def test_oversized_output_fails_closed(self):
        runner, _ = _recording_runner(proc=FakeProc(b'{"ok":true}' + b"x" * 100, 0))
        runner.max_output_bytes = 10
        self.assertEqual(runner.run("ensure_single_session", {})["reason"], "primitive_output_too_large")

    def test_timeout_fails_closed(self):
        import subprocess

        def timeout_run(argv, *, input_bytes, timeout_s):
            raise subprocess.TimeoutExpired(argv, timeout_s)

        runner = pr.PrimitiveRunner(scripts_dir="/scripts", run_subprocess=timeout_run,
                                    parse_validator=lambda p: (True, "ok"))
        self.assertEqual(runner.run("ensure_single_session", {}), {"ok": False, "reason": "primitive_timeout"})

    def test_launch_failure_fails_closed(self):
        def boom(argv, *, input_bytes, timeout_s):
            raise OSError("no powershell")

        runner = pr.PrimitiveRunner(scripts_dir="/scripts", run_subprocess=boom,
                                    parse_validator=lambda p: (True, "ok"))
        self.assertEqual(runner.run("ensure_single_session", {}), {"ok": False, "reason": "primitive_launch_failed"})


class ParseGateTests(unittest.TestCase):
    """RULE 9/11: the daemon must refuse to serve if any reviewed primitive fails to parse - with a positive
    control (a known-good script the validator accepts) AND a negative control (a script it rejects)."""

    def test_all_present_and_parse_ok(self):
        runner = pr.PrimitiveRunner(scripts_dir=_WINDOWS_SCRIPTS, parse_validator=lambda p: (True, "PARSE_OK"))
        runner.verify_scripts()   # positive control: real scripts exist and the validator accepts them

    def test_one_script_fails_parse_raises(self):
        bad = os.path.join(_WINDOWS_SCRIPTS, "Set-GuvfxRemoteApp.ps1")

        def validator(path):
            return (False, "PARSE_ERR") if path == bad else (True, "PARSE_OK")   # negative control

        runner = pr.PrimitiveRunner(scripts_dir=_WINDOWS_SCRIPTS, parse_validator=validator)
        with self.assertRaises(pr.PrimitiveError) as ctx:
            runner.verify_scripts()
        self.assertIn("parse_failed", ctx.exception.reason_code)

    def test_missing_script_raises(self):
        runner = pr.PrimitiveRunner(scripts_dir="/nonexistent-scripts-dir",
                                    parse_validator=lambda p: (True, "ok"))
        with self.assertRaises(pr.PrimitiveError) as ctx:
            runner.verify_scripts()
        self.assertIn("missing", ctx.exception.reason_code)

    def test_required_scripts_cover_contract(self):
        runner = pr.PrimitiveRunner(scripts_dir=_WINDOWS_SCRIPTS)
        want = {s.script for s in pr.CONTRACT.values() if s.script}
        self.assertEqual(set(runner.required_scripts()), want)

    def test_parse_command_interpolates_path_not_args(self):
        # Regression: `powershell -Command "<cmd>" <path>` does NOT populate $args from trailing args (it
        # appends them to the command), so the real ParseFile gate must EMBED the path in the command string.
        # (The earlier $args[0] form parsed nothing and made the daemon refuse to start on the host.)
        p = r"C:\GuvFX\hosted\scripts\Set-GuvfxRemoteApp.ps1"
        cmd = pr._parse_ps_command(p)
        self.assertNotIn("$args", cmd)
        self.assertIn("ParseFile('" + p + "'", cmd)         # single-quoted, interpolated
        self.assertIn("PARSE_OK", cmd)
        self.assertIn("PARSE_ERR", cmd)
        # single quotes in a path are doubled (PS escaping) so interpolation cannot break the string
        self.assertIn("a''b", pr._parse_ps_command("a'b"))


class ObserverScheduledTaskCmdletTests(unittest.TestCase):
    """AJ#3 corrective: ``Set-GuvfxObserver.ps1`` registered the observer task via ``New-ScheduledTaskSettings``
    - which is NOT a real cmdlet (the real one is ``New-ScheduledTaskSettingsSet``). The call is syntactically
    valid, so the ParseFile gate (RULE 9) accepts it, but at runtime it throws ``CommandNotFoundException``
    (HResult 0x80131501) BEFORE ``Register-ScheduledTask`` runs; the primitive returned ok:false for EVERY
    account and every hosted workspace stalled at PROVISIONING. ParseFile cannot catch a nonexistent-command
    call - a static command-resolution check must (packet Phase 4 A/B)."""

    def _observer(self):
        with open(os.path.join(_WINDOWS_SCRIPTS, "Set-GuvfxObserver.ps1"), encoding="ascii") as fh:
            return fh.read()

    def _windows_ps1(self):
        return [f for f in os.listdir(_WINDOWS_SCRIPTS) if f.endswith(".ps1")]

    def test_observer_uses_the_real_settings_cmdlet(self):
        self.assertIn("New-ScheduledTaskSettingsSet", self._observer())

    def test_no_windows_primitive_calls_the_nonexistent_settings_cmdlet(self):
        import re
        # IGNORECASE because PowerShell resolves cmdlet names case-insensitively (a re-typo as
        # `new-scheduledtasksettings` throws the same CommandNotFoundException). Comment lines are stripped so a
        # doc comment naming the wrong cmdlet is not a false positive (repo _code() convention). latin-1 is
        # total (never raises) and the cmdlet name is ASCII, so a non-ASCII byte elsewhere cannot break the scan.
        bad = re.compile(r"New-ScheduledTaskSettings(?!Set)", re.IGNORECASE)   # the name NOT followed by 'Set'
        offenders = []
        for n in self._windows_ps1():
            src = open(os.path.join(_WINDOWS_SCRIPTS, n), encoding="latin-1").read()
            code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
            if bad.search(code):
                offenders.append(n)
        self.assertEqual(offenders, [], f"nonexistent cmdlet New-ScheduledTaskSettings used in {offenders}")

    def test_observer_is_ascii_only(self):
        raw = open(os.path.join(_WINDOWS_SCRIPTS, "Set-GuvfxObserver.ps1"), "rb").read()
        self.assertEqual(sorted({b for b in raw if b > 127}), [], "Set-GuvfxObserver.ps1 has non-ASCII bytes")

    def test_observer_catch_emits_structured_diagnostics_not_bare_error(self):
        src = self._observer()
        self.assertIn('$result.reason = "observer_prepare_exception"', src)
        self.assertIn("$result.exception_type", src)
        self.assertIn("$result.exception_hresult", src)
        self.assertNotIn('$result.reason="error"', src)      # the old opaque collapse is gone

    def test_observer_takes_no_secret_so_diagnostics_cannot_leak_one(self):
        self.assertNotIn("$Password", self._observer())
        self.assertIsNone(pr.CONTRACT["prepare_observer"].stdin_arg)   # no stdin secret for this primitive


class FailureDiagnosticsTests(unittest.TestCase):
    """Diagnostic hardening (packet Phase 3, tests D/E): the runner passes the script's structured exception
    metadata through to the caller, and logs a SANITISED WARNING on any non-ok verdict - never the args."""

    def test_structured_exception_fields_pass_through_to_caller(self):
        runner, _ = _recording_runner(proc=FakeProc(
            b'{"ok":false,"reason":"observer_prepare_exception",'
            b'"exception_type":"CommandNotFoundException","exception_hresult":"0x80131501",'
            b'"exception_message":"The term X is not recognized"}', 1))
        res = runner.run("prepare_observer", {"username": "guvfx_u_2", "runtime_root": r"C:\GuvFX\accounts\2"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "observer_prepare_exception")
        self.assertEqual(res["exception_type"], "CommandNotFoundException")
        self.assertEqual(res["exception_hresult"], "0x80131501")

    def test_run_logs_warning_on_failure_without_args(self):
        runner, _ = _recording_runner(proc=FakeProc(
            b'{"ok":false,"reason":"observer_prepare_exception",'
            b'"exception_type":"CommandNotFoundException","exception_hresult":"0x80131501"}', 1))
        with self.assertLogs("guvfx.hosted-executor", level="WARNING") as cm:
            runner.run("prepare_observer", {"username": "guvfx_u_7", "runtime_root": r"C:\GuvFX\accounts\7"})
        blob = "\n".join(cm.output)
        self.assertIn("prepare_observer", blob)                 # primitive name
        self.assertIn("observer_prepare_exception", blob)       # stable reason
        self.assertIn("CommandNotFoundException", blob)         # exception type
        self.assertIn("0x80131501", blob)                       # HResult
        self.assertNotIn(r"C:\GuvFX\accounts\7", blob)          # NEVER the args / paths
        self.assertNotIn("guvfx_u_7", blob)                     # NEVER the username value

    def test_run_warning_redacts_unstructured_reason_from_legacy_scripts(self):
        # Four older CONTRACT scripts emit ``$result.error = $_.Exception.Message``; _parse_result promotes
        # that raw message into ``reason``. The WARNING log must NOT write such a message (it can carry an
        # arg-derived path / username) - it is redacted to 'unstructured'. (Adversarial-review MEDIUM.)
        runner, _ = _recording_runner(proc=FakeProc(
            b'{"ok":false,"error":"the user guvfx_u_7 was not found at path is denied"}', 1))
        with self.assertLogs("guvfx.hosted-executor", level="WARNING") as cm:
            runner.run("materialise_runtime", {"username": "guvfx_u_7", "runtime_root": r"C:\GuvFX\accounts\7"})
        blob = "\n".join(cm.output)
        self.assertIn("reason=unstructured", blob)            # the raw message is redacted, not logged
        self.assertNotIn("guvfx_u_7", blob)                   # the username inside the message never reaches the log
        self.assertNotIn("was not found", blob)

    def test_run_does_not_log_on_success(self):
        runner, _ = _recording_runner(proc=FakeProc(b'{"ok":true}', 0))
        with self.assertNoLogs("guvfx.hosted-executor", level="WARNING"):
            runner.run("ensure_single_session", {})

    def test_guard_refusals_are_also_logged_without_args(self):
        runner, _ = _recording_runner()
        with self.assertLogs("guvfx.hosted-executor", level="WARNING") as cm:
            runner.run("rm_rf_everything", {"username": "guvfx_u_9"})
        blob = "\n".join(cm.output)
        self.assertIn("unknown_primitive", blob)
        self.assertNotIn("guvfx_u_9", blob)


if __name__ == "__main__":
    unittest.main()
