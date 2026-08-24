"""Beta Readiness Stream 7C - the hosted executor's primitive runner (the injected ``run_primitive``).

This is the ONLY code that turns a dispatched primitive name + server-derived args into a real Windows action,
and it is deliberately incapable of arbitrary execution:

  * a primitive name resolves to EXACTLY ONE fixed, reviewed ``.ps1`` filename (allow-list) inside the
    configured scripts dir - never a caller path, never a free-form command;
  * every ``.ps1`` is ParseFile-gated at STARTUP (RULE 9) - the daemon refuses to serve if any fails to parse
    under the target PowerShell, and the gate asserts a POSITIVE marker (RULE 11: "exited 0" is not proof);
  * execution is a FIXED ARGUMENT VECTOR - ``powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass
    -File <fixed .ps1> <named args>`` - passed to ``subprocess`` with a list (NEVER a shell string), so there
    is no shell, no ``-Command``/``-EncodedCommand``, and no interpolation;
  * the args dict (snake_case, from ``host_agent_dispatch._build_args``) is mapped to the script's real
    ``-ParamName`` per a static table, mandatory params the dispatch omits are INJECTED (``-AccountId``,
    ``-Mode``), and unmapped keys are DROPPED - nothing from the request reaches the command line except these
    typed, server-derived scalars;
  * the Windows account PASSWORD (PROVISION_IDENTITY only) is written to the child's STDIN (first line) - never
    argv, never an environment variable, never a log - matching ``Provision-GuvfxAccount.ps1``;
  * the verdict is the script's ``ok`` (bool) AND a zero exit; output is size-capped and parsed as one compact
    JSON line; a timeout / non-zero exit / unparsable output all fail closed with a sanitised reason.

Django-free (stdlib only). ``run_subprocess`` and ``parse_validator`` are injected so the whole runner is
exercised in CI on any platform without a real PowerShell.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess  # noqa: S404 - fixed argument vector only; never a shell string (see below)

# Same channel the daemon configures (RotatingFileHandler, propagate off). Used ONLY for a sanitised
# WARNING on a non-ok verdict - never the args, paths, usernames, or secrets.
logger = logging.getLogger("guvfx.hosted-executor")

_USERNAME_RE = re.compile(r"^guvfx_u_([1-9][0-9]*)$")
_ALLOWED_SCALAR = (str, int, bool)

# A stable, secret-free reason CODE: snake_case, optionally ":<filename-like token>". A raw PowerShell
# exception message (spaces, quotes, path separators, drive letters) does NOT match. Four older CONTRACT
# scripts promote ``$_.Exception.Message`` into ``error``, which ``_parse_result`` surfaces as ``reason`` -
# so logging ``reason`` verbatim could write an arg-derived runtime path or username to the daemon log. Such
# a reason is redacted to a marker before it is logged (the structured exception_type/HResult, when present,
# are bounded by construction and still logged).
_SAFE_REASON_RE = re.compile(r"^[a-z0-9_]+(?::[A-Za-z0-9_.\-]+)?$")


def _safe_log_reason(reason) -> str:
    return reason if isinstance(reason, str) and _SAFE_REASON_RE.match(reason) else "unstructured"


class PrimitiveSpec:
    """A single primitive's execution contract. ``script=None`` marks a not-yet-implemented op (fail-closed)."""

    __slots__ = ("script", "argmap", "fixed", "inject_account_id", "stdin_arg")

    def __init__(self, *, script, argmap=None, fixed=None, inject_account_id=False, stdin_arg=None):
        self.script = script
        self.argmap = dict(argmap or {})            # snake_case arg key -> "-ParamName"
        self.fixed = dict(fixed or {})              # "-ParamName" -> fixed value (e.g. "-Mode": "Enforce")
        self.inject_account_id = bool(inject_account_id)   # inject "-AccountId <derived from username>"
        self.stdin_arg = stdin_arg                  # arg key whose value is piped to stdin (password), or None


# The authoritative primitive -> (.ps1, arg mapping) table. Cross-checked against the real ``param(...)`` blocks
# in backend/terminal_provisioning/windows/*.ps1 (Stream 7C research). Note the deliberate specifics:
#   - AppLocker maps username -> -HostedUser (NOT -Username).
#   - provision/materialise inject -AccountId (derived from the server-derived username) which _build_args omits.
#   - single-session/remoteapp/observer/applocker inject the fixed -Mode the dispatch omits.
#   - provision routes the password to stdin (never argv); terminal_root/username are dropped where absent.
CONTRACT = {
    "provision_identity": PrimitiveSpec(
        script="Provision-GuvfxAccount.ps1",
        argmap={"username": "-Username", "runtime_root": "-RuntimeRoot"},
        inject_account_id=True, stdin_arg="password"),
    "apply_workspace_acl": PrimitiveSpec(
        script="Set-GuvfxWorkspaceAcl.ps1",
        argmap={"username": "-Username", "runtime_root": "-RuntimeRoot",
                "snapshot_path": "-SnapshotPath", "mode": "-Mode"}),
    "rollback_workspace_acl": PrimitiveSpec(
        script="Set-GuvfxWorkspaceAcl.ps1",
        argmap={"username": "-Username", "runtime_root": "-RuntimeRoot",
                "snapshot_path": "-SnapshotPath", "mode": "-Mode"}),
    "materialise_runtime": PrimitiveSpec(
        script="Populate-GuvfxViewerRuntime.ps1",
        argmap={"runtime_root": "-RuntimeRoot"}, inject_account_id=True),
    "apply_autotrading_config": PrimitiveSpec(
        script="Set-GuvfxAutoTradingConfig.ps1",
        argmap={"terminal_root": "-TerminalRoot"}),
    # P0 golden-drift gate (read-only): report the runtime terminal64 ProductVersion vs the pinned golden
    # manifest build. -TerminalRoot is server-derived; the script emits a JSON verdict and mutates nothing.
    "verify_runtime_build": PrimitiveSpec(
        script="Verify-GuvfxRuntimeBuild.ps1",
        argmap={"terminal_root": "-TerminalRoot"}),
    # P0 proactive LiveUpdate containment (pre-first-launch). The .ps1 confines to guvfx_u_<id> +
    # accounts\<id>\terminal, refuses Customer Zero, ensures the tenant profile exists (CreateProfile — NO
    # interactive session, NO MT5 launch), and applies the certified Variant-A tenant-scoped deny-write on the
    # tenant's OWN roaming LiveUpdate staging (read-back verified). -AccountId is passed explicitly so the script
    # re-derives/validates identity + re-asserts the CZ refusal. It NEVER launches, logs in, or places an order.
    "apply_liveupdate_containment": PrimitiveSpec(
        script="Contain-GuvfxLiveUpdate.ps1",
        argmap={"username": "-Username", "terminal_root": "-TerminalRoot", "account_id": "-AccountId"}),
    # AJ#6.3: graceful in-session close+relaunch of THIS tenant's own terminal64 (post-login capability
    # recovery). The .ps1 confines to guvfx_u_<id> + accounts\<id>\terminal, refuses Customer Zero, and only
    # ever closes/launches the tenant's OWN terminal. -AccountId is passed explicitly (not injected) so the
    # script derives its per-account task names + re-asserts the CZ refusal.
    "relaunch_terminal": PrimitiveSpec(
        script="Relaunch-GuvfxTerminal.ps1",
        argmap={"username": "-Username", "terminal_root": "-TerminalRoot", "account_id": "-AccountId"}),
    "ensure_rdp_membership": PrimitiveSpec(
        script="Grant-GuvfxRdpAccess.ps1",
        argmap={"username": "-Username"}),
    "ensure_single_session": PrimitiveSpec(
        script="Set-GuvfxSingleSession.ps1",
        argmap={}, fixed={"-Mode": "Enforce"}),
    "ensure_remoteapp": PrimitiveSpec(
        script="Set-GuvfxRemoteApp.ps1",
        argmap={"terminal_root": "-TerminalRoot", "alias": "-Alias"}, fixed={"-Mode": "Ensure"}),
    "remove_remoteapp": PrimitiveSpec(
        script="Set-GuvfxRemoteApp.ps1",
        argmap={"terminal_root": "-TerminalRoot", "alias": "-Alias"}, fixed={"-Mode": "Remove"}),
    "prepare_observer": PrimitiveSpec(
        script="Set-GuvfxObserver.ps1",
        argmap={"username": "-Username", "runtime_root": "-RuntimeRoot"}, fixed={"-Mode": "Ensure"}),
    # 9E: trigger the account's session-bound observer task once + return its snapshot (read-only). The .ps1
    # derives the task/output path from -AccountId + -Username; no caller path/task/output is ever accepted.
    "observe_workspace": PrimitiveSpec(
        script="Invoke-GuvfxObserver.ps1",
        argmap={"username": "-Username", "runtime_root": "-RuntimeRoot", "terminal_root": "-TerminalRoot"},
        inject_account_id=True),
    "applocker_tenant_merge": PrimitiveSpec(
        script="Set-GuvfxAppLockerTenant.ps1",
        argmap={"username": "-HostedUser", "account_id": "-AccountId"}, fixed={"-Mode": "Merge"}),
    "applocker_tenant_remove": PrimitiveSpec(
        script="Set-GuvfxAppLockerTenant.ps1",
        argmap={"username": "-HostedUser", "account_id": "-AccountId"}, fixed={"-Mode": "Remove"}),
    # No reviewed read-only slot-verify .ps1 exists yet; VERIFY_SLOT fails closed rather than pretend success.
    "verify_slot": PrimitiveSpec(script=None),
    # FINAL Closed-Beta stream: activate THIS node's dedicated pin-enforcing order bridge. Reviewed .ps1 takes
    # -TerminalRoot + injected -AccountId (both server-derived); it REFUSES account 1, writes the node2 env,
    # registers+starts the bridge + a port-specific watchdog, and health-checks :8789. Emits a JSON verdict.
    "activate_order_bridge": PrimitiveSpec(
        script="Activate-GuvfxOrderBridge.ps1",
        argmap={"terminal_root": "-TerminalRoot"}, inject_account_id=True),
    # P0-B1.1 multi-tenant: activate THIS tenant's OWN pin-enforcing order bridge on its per-tenant PORT.
    # Reviewed .ps1 takes explicit -AccountId (refuses CZ) + -TerminalRoot (confined) + -Port (per-tenant
    # 8800-8899, backend-allocated + host re-validated). One private bridge process/task/watchdog per tenant;
    # port-targeted control only (never a blanket python kill).
    "activate_tenant_bridge": PrimitiveSpec(
        script="Activate-GuvfxTenantBridge.ps1",
        argmap={"terminal_root": "-TerminalRoot", "account_id": "-AccountId", "port": "-Port"}),
}

# The PowerShell one-liner used by the default ParseFile gate. Builds the AST WITHOUT executing (RULE 9) and
# prints an explicit positive/negative marker so a bare exit-0 is never mistaken for a clean parse (RULE 11).
# The script path is INTERPOLATED (single-quoted, quotes doubled) into the command rather than passed as a
# trailing argument, because ``powershell -Command "<cmd>" <path>`` does NOT populate $args from trailing args
# (it appends them to the command). The path is a fixed reviewed .ps1 already confined to the scripts dir by
# ``script_path`` before this runs, so interpolation introduces no injection surface.
def _parse_ps_command(script_path: str) -> str:
    ps_path = "'" + str(script_path).replace("'", "''") + "'"
    return ("$e=$null;$t=$null;"
            "[void][System.Management.Automation.Language.Parser]::ParseFile(" + ps_path + ",[ref]$t,[ref]$e);"
            "if($e -and $e.Count -gt 0){Write-Output 'PARSE_ERR';exit 1}else{Write-Output 'PARSE_OK';exit 0}")


class PrimitiveError(Exception):
    """A refusal to even attempt a primitive (unknown/unimplemented/malformed args). Sanitised reason code."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _default_run_subprocess(argv, *, input_bytes, timeout_s):
    """Execute a fixed argument vector. NEVER a shell string (``shell=False`` is the default and explicit)."""
    return subprocess.run(  # noqa: S603 - argv is a fixed list of reviewed script + typed server-derived args
        argv, input=input_bytes, capture_output=True, timeout=timeout_s, shell=False, check=False)


def _default_parse_validator(powershell, script_path):
    """Real ParseFile gate: returns (ok, detail). ok only when the explicit ``PARSE_OK`` marker is printed."""
    argv = [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command", _parse_ps_command(script_path)]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=60, shell=False, check=False)  # noqa: S603
    except Exception as exc:  # noqa: BLE001
        return False, f"parse_invoke_failed:{type(exc).__name__}"
    out = (proc.stdout or b"").decode("utf-8", "replace")
    return ("PARSE_OK" in out and proc.returncode == 0), out.strip()[:120]


class PrimitiveRunner:
    """Resolves + executes reviewed provisioning primitives. Construct with the daemon config; pass ``.run`` to
    ``host_agent_dispatch.dispatch`` as ``run_primitive``."""

    def __init__(self, *, scripts_dir, powershell="powershell", timeout_s=600.0, max_output_bytes=65536,
                 run_subprocess=None, parse_validator=None):
        self.scripts_dir = os.path.abspath(scripts_dir)
        self.powershell = powershell
        self.timeout_s = float(timeout_s)
        self.max_output_bytes = int(max_output_bytes)
        self._run = run_subprocess or _default_run_subprocess
        self._parse = parse_validator or (lambda p: _default_parse_validator(self.powershell, p))

    # ── startup gates ─────────────────────────────────────────────────────────────────────────────────────
    def script_path(self, filename: str) -> str:
        """Resolve a fixed script filename inside the scripts dir, refusing anything that escapes it."""
        if not filename or os.path.basename(filename) != filename:
            raise PrimitiveError("primitive_script_name_invalid")   # no path components in a fixed constant
        path = os.path.abspath(os.path.join(self.scripts_dir, filename))
        if os.path.commonpath([path, self.scripts_dir]) != self.scripts_dir:
            raise PrimitiveError("primitive_script_escapes_dir")
        return path

    def required_scripts(self) -> list[str]:
        seen, out = set(), []
        for spec in CONTRACT.values():
            if spec.script and spec.script not in seen:
                seen.add(spec.script)
                out.append(spec.script)
        return sorted(out)

    def verify_scripts(self) -> None:
        """STARTUP gate (RULE 9): every mapped .ps1 must exist AND ParseFile-validate. Raise on ANY failure -
        the daemon must not serve if a primitive cannot be parsed by the target PowerShell."""
        for filename in self.required_scripts():
            path = self.script_path(filename)
            if not os.path.isfile(path):
                raise PrimitiveError(f"primitive_script_missing:{filename}")
            ok, detail = self._parse(path)
            if not ok:
                raise PrimitiveError(f"primitive_script_parse_failed:{filename}:{detail}")

    # ── per-request execution ─────────────────────────────────────────────────────────────────────────────
    def _account_id_from_username(self, args: dict) -> str:
        m = _USERNAME_RE.match(str(args.get("username") or ""))
        if not m:
            raise PrimitiveError("account_id_underivable")
        return m.group(1)

    def _build_argv(self, spec: PrimitiveSpec, args: dict) -> list:
        """Assemble the fixed argument vector. Raises PrimitiveError on any unsafe/non-scalar value."""
        flags: list[str] = []

        def _emit(param: str, value):
            if isinstance(value, bool):
                sval = "1" if value else "0"
            elif isinstance(value, int):
                sval = str(value)
            elif isinstance(value, str):
                sval = value
            else:
                raise PrimitiveError("param_not_scalar")
            # A value that begins with '-' could be re-interpreted by PowerShell as another named parameter.
            # Server-derived paths/usernames/aliases/modes never do; refuse defensively if one ever does.
            if sval.startswith("-"):
                raise PrimitiveError("param_value_dashed")
            if "\x00" in sval or "\r" in sval or "\n" in sval:
                raise PrimitiveError("param_value_control_char")
            flags.append(param)
            flags.append(sval)

        for param, value in spec.fixed.items():
            _emit(param, value)
        for arg_key, param in spec.argmap.items():
            if arg_key == spec.stdin_arg:
                continue                      # secret goes to stdin, never argv
            if arg_key not in args:
                continue                      # optional / not supplied for this op
            value = args[arg_key]
            if not isinstance(value, _ALLOWED_SCALAR):
                raise PrimitiveError("param_not_scalar")
            _emit(param, value)
        if spec.inject_account_id:
            _emit("-AccountId", self._account_id_from_username(args))
        return flags

    def _stdin_bytes(self, spec: PrimitiveSpec, args: dict) -> bytes:
        """The child's stdin. For PROVISION_IDENTITY: the plaintext password (from envelope_open) as one line;
        the value is used and never returned/logged. Every other primitive gets an empty, closed stdin so a
        script that unexpectedly reads stdin sees EOF immediately rather than hanging the worker."""
        if not spec.stdin_arg:
            return b""
        value = args.get(spec.stdin_arg)
        if isinstance(value, bytes):
            return value + b"\n"
        if isinstance(value, str) and value:
            return value.encode("utf-8") + b"\n"
        raise PrimitiveError("stdin_value_absent")

    def _parse_result(self, proc) -> dict:
        raw = proc.stdout or b""
        if len(raw) > self.max_output_bytes:
            return {"ok": False, "reason": "primitive_output_too_large"}
        text = raw.decode("utf-8", "replace").strip()
        line = ""
        for candidate in reversed(text.splitlines()):     # last non-empty line = the compact JSON verdict
            if candidate.strip():
                line = candidate.strip()
                break
        if not line:
            return {"ok": False, "reason": "primitive_no_output"}
        try:
            parsed = json.loads(line)
        except (ValueError, TypeError):
            return {"ok": False, "reason": "primitive_bad_output"}
        if not isinstance(parsed, dict):
            return {"ok": False, "reason": "primitive_bad_output"}
        # The verdict is the script's own ``ok`` AND a zero exit. Failure detail is ``reason`` (Stream 5/6
        # scripts) or ``error`` (older TX-1 scripts); surface whichever is present, without a secret.
        script_ok = bool(parsed.get("ok")) and proc.returncode == 0
        parsed["ok"] = script_ok
        if not script_ok and not parsed.get("reason"):
            parsed["reason"] = str(parsed.get("error") or "primitive_failed")
        return parsed

    def run(self, primitive: str, args: dict) -> dict:
        """The injected ``run_primitive(primitive, args) -> {"ok": bool, ...}``. Never raises into dispatch -
        every refusal/failure is a sanitised ``{"ok": False, "reason": ...}`` dict.

        A non-ok verdict is logged at WARNING with ONLY the sanitised diagnostic fields - the primitive name,
        the stable reason code, and any exception type/HResult the script itself surfaced. The args dict is
        NEVER passed to the logger, so no path, username, or secret can reach the daemon log (the observability
        gap that hid the AJ#3 ``New-ScheduledTaskSettings`` typo behind a bare ``reason="error"``)."""
        result = self._run_inner(primitive, args)
        if not (isinstance(result, dict) and result.get("ok")):
            reason = result.get("reason") if isinstance(result, dict) else None
            exc_type = result.get("exception_type") if isinstance(result, dict) else None
            exc_hr = result.get("exception_hresult") if isinstance(result, dict) else None
            logger.warning("primitive failed primitive=%s reason=%s exception_type=%s exception_hresult=%s",
                           str(primitive), _safe_log_reason(reason), exc_type, exc_hr)
        return result

    def _run_inner(self, primitive: str, args: dict) -> dict:
        try:
            spec = CONTRACT.get(str(primitive))
            if spec is None:
                return {"ok": False, "reason": "unknown_primitive"}
            if spec.script is None:
                return {"ok": False, "reason": f"{primitive}_unimplemented"}
            if not isinstance(args, dict):
                return {"ok": False, "reason": "args_malformed"}
            path = self.script_path(spec.script)
            argv = [self.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", path] + self._build_argv(spec, args)
            stdin_bytes = self._stdin_bytes(spec, args)
            try:
                proc = self._run(argv, input_bytes=stdin_bytes, timeout_s=self.timeout_s)
            except subprocess.TimeoutExpired:
                return {"ok": False, "reason": "primitive_timeout"}
            except Exception:  # noqa: BLE001 - any launch failure is ambiguous -> fail closed, sanitised
                return {"ok": False, "reason": "primitive_launch_failed"}
            finally:
                stdin_bytes = b""      # drop the plaintext password reference promptly
            return self._parse_result(proc)
        except PrimitiveError as exc:
            return {"ok": False, "reason": exc.reason_code}
