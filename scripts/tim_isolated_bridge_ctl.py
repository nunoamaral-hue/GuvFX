#!/usr/bin/env python3
"""Isolated TradersWay-Demo winter-timezone observation lifecycle controller.

GFX-TIM-ISOLATED-WINTER-TZ-PATH-013 / ADR-0121.

Starts and stops an ISOLATED, ephemeral read path for the winter broker-timezone probe against
the dormant ``account_001`` TradersWay-Demo slot, WITHOUT ever touching the production ``:8788``
bridge, the Program-Files MT5 terminal, or any live tenant.

Standard library only (so it is testable in the dependency-free governance CI). No trading
capability. The token is never read, printed, or logged: it flows opaquely from the caller's
environment (loaded by the reviewed secrets ``.bat``) into the temp bridge child process; this
module passes the environment through to the bridge and NEVER inspects, logs, or stores a token.

Safety invariants (enforced in code, unit-tested with a fake host):
- ``:8788`` is a PROTECTED CONTROL: verified listening at start, verified unchanged at both start
  and stop; never started, stopped, repointed, or mutated.
- Only the dedicated loopback endpoint ``127.0.0.1:<FIXED_PORT>`` is used; proven loopback-only
  (never ``0.0.0.0``) after start.
- Only processes THIS controller created are ever terminated, and only after a positive identity
  re-check of the live process at that PID (defeats PID reuse). Broad process-name termination
  (``taskkill /IM ...``) is never used.
- Partial-start failures deterministically tear down only the newly created resources.
- A pre-existing ``account_001`` terminal (e.g. one the owner started to log in the demo) is
  attached to but NOT owned, so teardown never kills it.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time

# --- Fixed contract (no user-configurable values) -------------------------------------------------
ACCOUNT001_TERMINAL = r"C:\GuvFX\terminals\account_001\instance\terminal64.exe"
STAGING_DIR = r"C:\GuvFX\TIM\winter-tz-probe"
STATE_FILE = STAGING_DIR + r"\tim_isolated_state.json"
BRIDGE_SCRIPT = STAGING_DIR + r"\mt5_signal_bridge.py"
FIXED_PORT = 8977                 # loopback-only; proven free of repository allocations (8800-8899 etc.)
BIND_HOST = "127.0.0.1"
PROTECTED_PORT = 8788             # production Taurex-LIVE bridge — NEVER touched
EXPECTED_SERVER = "TradersWay-Demo"
STATE_SCHEMA = "tim-isolated-ctl/1"
TERMINAL_SETTLE_S = 12            # bounded wait for a freshly-started terminal to initialise
BRIDGE_BIND_TIMEOUT_S = 30        # bounded wait for the bridge to bind the loopback port
IDENTITY_CAPTURE_ATTEMPTS = 6     # bounded retry so a just-started process's identity is captured
IDENTITY_CAPTURE_DELAY_S = 0.2
LOOPBACK_ADDRS = ("127.0.0.1", "::1")


class CtlError(Exception):
    """Internal abort with a value-blind reason code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --- Host operations seam (real impl; a fake is injected by the tests) ----------------------------
class HostOps:
    """All host interaction funnels through here so the lifecycle logic is fully testable."""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def listeners(self):
        """Return [(addr, port, pid), ...] for TCP LISTENING sockets (netstat -ano)."""
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
        rows = []
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[0].upper() != "TCP" or "LISTENING" not in parts:
                continue
            local, pid = parts[1], parts[-1]
            addr, _, port = local.rpartition(":")
            addr = addr.strip("[]")
            try:
                rows.append((addr, int(port), int(pid)))
            except ValueError:
                continue
        return rows

    def process(self, pid):
        """Return {'exe': ..., 'cmdline': ...} for a live PID, or None. Value-blind (no token)."""
        if pid is None:
            return None
        info = self._wmic_process(pid)
        if info is None:
            info = self._ps_process(pid)
        return info

    def find_pids_by_exe(self, exe_path):
        """PIDs whose executable path equals exe_path (exact, case-insensitive)."""
        target = exe_path.lower()
        pids = []
        out = subprocess.run(
            ["wmic", "process", "get", "ProcessId,ExecutablePath", "/format:csv"],
            capture_output=True, text=True,
        )
        for line in out.stdout.splitlines():
            cols = line.split(",")
            if len(cols) < 3:
                continue
            exe, raw_pid = cols[-1].strip(), cols[-2].strip()
            if exe.lower() == target and raw_pid.isdigit():
                pids.append(int(raw_pid))
        return pids

    def start(self, cmd, env):
        """Launch a DETACHED process that outlives this controller; return its PID."""
        flags = 0
        detached = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags = detached
        proc = subprocess.Popen(
            cmd, env=env, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        return proc.pid

    def terminate(self, pid):
        """Terminate exactly ONE pid. Never a name-based broad kill."""
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/F"], capture_output=True, text=True)

    def _wmic_process(self, pid):
        out = subprocess.run(
            ["wmic", "process", "where", "processid=%d" % int(pid),
             "get", "ExecutablePath,CommandLine", "/format:list"],
            capture_output=True, text=True,
        )
        exe = cmdline = None
        for line in out.stdout.splitlines():
            if line.startswith("ExecutablePath="):
                exe = line.split("=", 1)[1].strip()
            elif line.startswith("CommandLine="):
                cmdline = line.split("=", 1)[1].strip()
        if exe or cmdline:
            return {"exe": exe or "", "cmdline": cmdline or ""}
        return None

    def _ps_process(self, pid):
        ps = ("$p=Get-CimInstance Win32_Process -Filter \"ProcessId=%d\";"
              "if($p){Write-Output ('exe=' + $p.ExecutablePath);"
              "Write-Output ('cmd=' + $p.CommandLine)}" % int(pid))
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True,
        )
        exe = cmdline = None
        for line in out.stdout.splitlines():
            if line.startswith("exe="):
                exe = line[4:].strip()
            elif line.startswith("cmd="):
                cmdline = line[4:].strip()
        if exe or cmdline:
            return {"exe": exe or "", "cmdline": cmdline or ""}
        return None


# --- Pure-ish lifecycle logic (host injected) -----------------------------------------------------
def _listener_pids(host, port):
    return [(addr, pid) for (addr, p, pid) in host.listeners() if p == port]


def _single_protected_pid(host):
    pids = {pid for (_addr, pid) in _listener_pids(host, PROTECTED_PORT)}
    if len(pids) != 1:
        return None
    return next(iter(pids))


def _port_free(host, port):
    return len(_listener_pids(host, port)) == 0


def _bridge_env(env):
    """Environment for the isolated, read-only temp bridge. Inherits the caller's env (which carries
    the opaque GUVFX_AGENT_TOKEN loaded by the secrets .bat) and overlays a hard read-only posture.
    The token is passed through untouched; it is never read or logged by this module."""
    e = dict(env)
    e["BRIDGE_BIND_HOST"] = BIND_HOST       # loopback only
    e["BRIDGE_HTTP_ONLY"] = "1"             # no job poller, no order/execution path, POST refused
    e["HTTP_SERVER_PORT"] = str(FIXED_PORT)
    e["MT5_TERMINAL_PATH"] = ACCOUNT001_TERMINAL
    e["MT5_GUARDED_ATTACH"] = "1"           # never-launch attach to the running account_001 terminal
    e["MT5_EXPECTED_SERVER"] = EXPECTED_SERVER
    e.pop("MT5_ALLOW_LIVE", None)           # never allow live
    # No backend job-poll/execution transport (belt-and-suspenders alongside BRIDGE_HTTP_ONLY).
    for k in ("GUVFX_API_URL", "MT5_API_URL", "API_BASE_URL"):
        e.pop(k, None)
    return e


def _identity(host, pid):
    info = host.process(pid)
    if not info:
        return None
    return {"exe": info.get("exe", ""), "cmdline": info.get("cmdline", "")}


def _capture_identity(host, pid):
    """Capture a process identity with a bounded retry: a just-started process can momentarily be
    invisible to the process query, and relying on an empty identity would let rollback refuse to
    terminate a live resource (orphan). Returns {exe, cmdline} or None if it never appears."""
    ident = _identity(host, pid)
    attempts = 0
    while ident is None and attempts < IDENTITY_CAPTURE_ATTEMPTS:
        host.sleep(IDENTITY_CAPTURE_DELAY_S)
        ident = _identity(host, pid)
        attempts += 1
    return ident


def _identity_matches(host, pid, recorded):
    """True only if the LIVE process at pid is still the one we recorded (defeats PID reuse)."""
    if pid is None or not recorded:
        return False
    live = _identity(host, pid)
    if live is None:
        return False
    return live.get("exe", "") == recorded.get("exe", "") and \
        live.get("cmdline", "") == recorded.get("cmdline", "")


def _find_terminal(host):
    pids = host.find_pids_by_exe(ACCOUNT001_TERMINAL)
    return pids[0] if pids else None


def _await_loopback_bind(host, pid):
    """Within a bounded window, require FIXED_PORT to be LISTENING, bound ONLY to loopback, owned by
    pid, and never present on 0.0.0.0. Returns True iff that holds."""
    deadline = host.now() + BRIDGE_BIND_TIMEOUT_S
    while host.now() <= deadline:
        entries = _listener_pids(host, FIXED_PORT)
        if entries:
            addrs = {addr for (addr, _pid) in entries}
            owners = {p for (_addr, p) in entries}
            if any(a not in LOOPBACK_ADDRS for a in addrs):
                return False  # bound to a non-loopback interface -> fail closed
            if owners == {pid}:
                return True
        host.sleep(0.25)
    return False


def _safe_terminate(host, pid, recorded_identity):
    """Terminate pid ONLY if the live process still matches recorded_identity.
    Returns True when it is safe to proceed (killed, or already gone); False to REFUSE (pid reused)."""
    if pid is None:
        return True
    if host.process(pid) is None:
        return True  # already gone - nothing to kill
    if not _identity_matches(host, pid, recorded_identity):
        return False  # a DIFFERENT process now holds this pid - refuse
    host.terminate(pid)
    return True


def _rollback(host, created):
    """Deterministically tear down only resources this start created (bridge first, then terminal).
    A newly-created resource whose identity was not captured at creation gets a last-chance capture,
    so a live process we started is never orphaned by an empty-identity refusal."""
    for key in ("bridge", "terminal"):
        r = created.get(key)
        if r is None:
            continue
        ident = r.get("identity") or _capture_identity(host, r.get("pid"))
        _safe_terminate(host, r.get("pid"), ident)


def _write_state(state):
    os.makedirs(STAGING_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="ascii") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def start_observation(host, env=None):
    env = os.environ.copy() if env is None else dict(env)

    protected_pid = _single_protected_pid(host)
    if protected_pid is None:
        return _fail("protected_8788_not_single_listener")
    if not _port_free(host, FIXED_PORT):
        return _fail("fixed_port_in_use")
    if os.path.exists(STATE_FILE):
        return _fail("isolated_observation_already_active_or_stale_state")

    created = {"terminal": None, "bridge": None}
    try:
        # Terminal: start the dormant slot, or attach to a pre-existing one WITHOUT owning it.
        existing = _find_terminal(host)
        if existing is not None:
            term_pid, term_created = existing, False
        else:
            term_pid = host.start([ACCOUNT001_TERMINAL, "/portable"], env)
            term_created = True
            host.sleep(TERMINAL_SETTLE_S)
        term_identity = _capture_identity(host, term_pid)
        if term_identity is None or term_identity.get("exe", "").lower() != ACCOUNT001_TERMINAL.lower():
            raise CtlError("terminal_identity_unconfirmed")
        if term_created:
            created["terminal"] = {"pid": term_pid, "identity": term_identity}

        # Isolated read-only bridge (loopback, http-only, guarded-attach, demo-only). A per-start
        # nonce in argv makes the bridge's identity UNIQUE, so even an identically-invoked bridge
        # can never be mistaken for ours during teardown (hardens PID-reuse defence).
        run_nonce = secrets.token_hex(8)
        bridge_pid = host.start([sys.executable or "python", BRIDGE_SCRIPT, "--tim-run-id", run_nonce],
                                _bridge_env(env))
        bridge_identity = _capture_identity(host, bridge_pid)
        created["bridge"] = {"pid": bridge_pid, "identity": bridge_identity or {}}

        if not _await_loopback_bind(host, bridge_pid):
            raise CtlError("loopback_bind_unproven")
        if bridge_identity is None:
            raise CtlError("bridge_identity_unconfirmed")
        if _single_protected_pid(host) != protected_pid:
            raise CtlError("protected_8788_changed")

        state = {
            "schema": STATE_SCHEMA,
            "fixed_port": FIXED_PORT,
            "bind_host": BIND_HOST,
            "protected_port": PROTECTED_PORT,
            "protected_pid": protected_pid,
            "bridge": {"pid": bridge_pid, "identity": bridge_identity, "created_by_us": True,
                       "run_nonce": run_nonce},
            "terminal": {"pid": term_pid, "identity": term_identity,
                         "created_by_us": term_created},
            "started_at_utc": host.now(),
        }
        _write_state(state)
        return _ok("isolated_bridge_started", {
            "fixed_port": FIXED_PORT, "bind_host": BIND_HOST, "loopback_only": True,
            "protected_8788_unchanged": True, "terminal_created_by_us": term_created,
        })
    except CtlError as exc:
        _rollback(host, created)
        return _fail(exc.reason)
    except Exception as exc:  # noqa: BLE001 - fail closed on anything, value-blind
        _rollback(host, created)
        return _fail("start_error_%s" % type(exc).__name__)


def stop_observation(host):
    if not os.path.exists(STATE_FILE):
        return _fail("no_state_file")
    try:
        with open(STATE_FILE, encoding="ascii") as fh:
            state = json.load(fh)
    except (ValueError, OSError):
        return _fail("malformed_state_file")
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
        return _fail("unrecognized_state_schema")

    protected_pid = state.get("protected_pid")
    bridge = state.get("bridge") or {}
    terminal = state.get("terminal") or {}

    # Bridge is always ours: terminate with a live-identity re-check; refuse on PID reuse.
    if not _safe_terminate(host, bridge.get("pid"), bridge.get("identity")):
        return _fail("bridge_identity_mismatch_refused")
    # Terminal only if WE created it; never kill a terminal the owner started.
    if terminal.get("created_by_us"):
        if not _safe_terminate(host, terminal.get("pid"), terminal.get("identity")):
            return _fail("terminal_identity_mismatch_refused")

    if not _port_free(host, FIXED_PORT):
        return _fail("temp_listener_still_present")
    if _single_protected_pid(host) != protected_pid:
        return _fail("protected_8788_changed")

    try:
        os.remove(STATE_FILE)
    except OSError:
        return _fail("state_file_removal_failed")
    return _ok("isolated_bridge_stopped", {
        "temp_listener_gone": True, "protected_8788_unchanged": True,
    })


# --- Value-blind results --------------------------------------------------------------------------
def _ok(status, extra=None):
    out = {"ok": True, "status": status, "fixed_port": FIXED_PORT, "bind_host": BIND_HOST,
           "protected_port": PROTECTED_PORT}
    if extra:
        out.update(extra)
    return out


def _fail(reason):
    return {"ok": False, "status": "refused", "reason_code": reason,
            "fixed_port": FIXED_PORT, "protected_port": PROTECTED_PORT}


def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="tim_isolated_bridge_ctl",
        description="Start/stop the isolated TradersWay-Demo winter-timezone observation bridge "
                    "(loopback-only, read-only, account_001; :8788 never touched).")
    p.add_argument("action", choices=("start", "stop"))
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    host = HostOps()
    result = start_observation(host) if args.action == "start" else stop_observation(host)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
