"""STREAM 9E - the observer's self-contained M1 Guarded-Attach primitive (never launch, never login).

This is a FAITHFUL, stdlib-only extraction of the certified guarded-attach helpers that used to be reached via
``scripts.mt5_signal_bridge`` (``_guarded_attach_enabled`` / ``evaluate_guarded_attach`` /
``_running_terminal_dirs`` / ``_terminal_process_running`` / ``guarded_initialize``). It exists for two reasons,
both required by the STREAM 9E hardening packet:

1. **Do not use the legacy bridge.** ``scripts.mt5_signal_bridge`` is the legacy signal-copy bridge (it imports
   ``requests``/``urllib3`` and stands up an HTTP server at module import). Routing the read-only observer
   through it drags the whole execution bridge into a locked-down tenant session. The observer must depend on a
   narrow, reviewed, read-only attach primitive instead.
2. **Self-contained staging.** ``run_observer.py`` is staged FLAT to ``C:\\GuvFX\\observer`` as the tenant. With
   the legacy import, ``from scripts import mt5_signal_bridge`` cannot resolve there (no ``scripts`` package on
   ``sys.path``) and the observer was dead-on-arrival for every account. This module is staged as a sibling so
   ``import observer_attach`` resolves with zero transitive dependencies beyond the host-only ``MetaTrader5``.

The behaviour is IDENTICAL to the legacy helpers (``tests_observer_attach.py`` is a parity lock that runs both
side-by-side across a matrix, so this copy can never silently diverge). Nothing here launches MT5, authenticates
(``mt5.login`` is never called; credential keys are refused), places, modifies, or closes an order.
"""
from __future__ import annotations

import logging
import ntpath
import os

logger = logging.getLogger("guvfx.hosted_workspace.observer_attach")

# P0 bounded observation: the guarded attach caps mt5.initialize(timeout=) so a busy terminal fails fast (see
# guarded_initialize). Milliseconds. Overridable via env for host tuning; default 8s (>> a healthy attach).
try:
    _ATTACH_TIMEOUT_MS = max(2000, int(os.getenv("GUVFX_OBSERVER_ATTACH_TIMEOUT_MS", "8000")))
except (TypeError, ValueError):
    _ATTACH_TIMEOUT_MS = 8000


def _guarded_attach_enabled() -> bool:
    """When set (on a persistent-workspace / observer host) the attach primitive enforces the never-launch
    invariant (target already running + connected + identity, or fail closed). Unset => exact prior
    ``mt5.initialize()`` behaviour (may launch) - matching the legacy bridge default byte-for-byte."""
    return os.getenv("MT5_GUARDED_ATTACH", "").strip().lower() in ("1", "true", "yes", "on")


def evaluate_guarded_attach(path, process_running, init_ok, terminal_connected, account_present):
    """Pure, fail-closed decision for the GUARDED (never-launch) attach - no MT5, no I/O, fully
    unit/mutation-testable. Reports the most specific failure first. Returns (ok: bool, reason: str).

    Inputs are gathered by ``guarded_initialize()`` in the ONLY safe order: ``process_running`` is probed
    BEFORE ``mt5.initialize()`` is ever called, so a not-running terminal is rejected here and never launched;
    ``init_ok``/``terminal_connected``/``account_present`` reflect the attach that followed a positive probe."""
    if not path:
        return False, "guarded_attach_no_path"
    if not process_running:
        return False, "guarded_attach_terminal_not_running"  # never launch a down terminal
    if not init_ok:
        return False, "guarded_attach_initialize_failed"
    if not terminal_connected:
        return False, "guarded_attach_not_connected"  # attached but no live broker link
    if not account_present:
        return False, "guarded_attach_no_account"
    return True, "ok"


def _running_terminal_dirs():
    """Install directories (lowercased) of every currently-running terminal64.exe. FAIL-CLOSED: a directory
    is included ONLY when its executable path is confirmed, so a foreign / image-name-only match can never
    stand in for the target. Tries psutil (precise), then wmic ExecutablePath; any failure yields a partial/
    empty set rather than a false positive."""
    dirs = set()
    try:
        import psutil
        for proc in psutil.process_iter(["exe"]):
            try:
                exe = proc.info.get("exe") or ""
                if exe and os.path.basename(exe).lower() == "terminal64.exe":
                    dirs.add(os.path.dirname(os.path.abspath(exe)).lower())
            except Exception:
                continue
        return dirs
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(
            ["wmic", "process", "where", "name='terminal64.exe'", "get", "ExecutablePath", "/format:csv"],
            capture_output=True, text=True, timeout=10,
        )
        for line in (out.stdout or "").splitlines():
            for field in line.split(","):
                f = field.strip()
                if f.lower().endswith("terminal64.exe"):
                    dirs.add(os.path.dirname(os.path.abspath(f)).lower())
    except Exception:
        pass
    return dirs


def _terminal_process_running(path) -> bool:
    """Is a terminal64.exe already running from ``path``'s INSTALL DIRECTORY? Used ONLY to guarantee
    ``guarded_initialize()`` never launches MT5. Matches strictly by install directory (never image-name alone),
    so a foreign terminal on a multi-install host can never green-light launching a down target.
    FAIL-CLOSED: no path, an unresolvable path, or an unconfirmable process set => False."""
    if not path:
        return False
    try:
        target_dir = os.path.dirname(os.path.abspath(path)).lower()
    except Exception:
        return False
    return target_dir in _running_terminal_dirs()


def _bare_terminal_pids(path):
    """PIDs of terminal64.exe running from ``path``'s INSTALL DIRECTORY *without* ``/portable`` on their command
    line. This is the exact signature of a terminal that ``mt5.initialize(path=)`` launched itself (its own
    auto-launch omits ``/portable`` -> the non-portable data dir hits the containment DENY ACL). A ``/portable``
    terminal - the real per-account runtime, or any other tenant's - is never in this set and so can never be
    neutralised.

    Returns a SET of PIDs when it enumerated (possibly empty = 'ran, found none'), or ``None`` when it could not
    enumerate at ALL (neither psutil nor wmic usable). The caller treats ``None`` as 'cannot guarantee
    never-launch' and FAILS CLOSED (does not attach), so a host missing both tools can never let the gate silently
    no-op into launching a bare terminal. psutil is preferred (precise: exe dir + cmdline + owner); the wmic
    fallback (PID + CommandLine) mirrors the sibling ``_running_terminal_dirs`` so the guarantee holds on a host
    without psutil (psutil is not a declared observer dependency)."""
    if not path:
        return set()
    try:
        # Windows paths, always: on the host os.path IS ntpath; using ntpath explicitly keeps this correct there AND
        # unambiguous when exercised off-Windows (os.path=posixpath would treat "C:\..." as a relative path).
        target_dir = ntpath.dirname(ntpath.abspath(path)).lower()
    except Exception:
        return set()

    # --- Preferred: psutil (precise) ---
    try:
        import psutil
    except Exception:
        psutil = None
    if psutil is not None:
        try:
            try:
                me = psutil.Process().username()
            except Exception:
                me = None
            result = set()
            for proc in psutil.process_iter(["pid", "exe", "cmdline", "username"]):
                try:
                    exe = proc.info.get("exe") or ""
                    if ntpath.basename(exe).lower() != "terminal64.exe":
                        continue
                    if ntpath.dirname(ntpath.abspath(exe)).lower() != target_dir:
                        continue
                    if me is not None and (proc.info.get("username") or "") != me:
                        continue  # only our OWN processes (belt-and-suspenders; we can only kill ours anyway)
                    cl = proc.info.get("cmdline")
                    if not cl:
                        # empty/unreadable command line: we CANNOT confirm this is a bare (non-/portable) instance,
                        # so we must NOT classify it as one - fail closed in the KILL direction, never risk
                        # neutralising the account's real /portable terminal on a cmdline-read failure.
                        continue
                    cmd = " ".join(cl).lower()
                    if "/portable" not in cmd:
                        result.add(int(proc.info["pid"]))
                except Exception:
                    continue
            return result
        except Exception:
            pass  # psutil present but unusable -> try wmic

    # --- Fallback: wmic (host has no psutil) -> ProcessId + CommandLine (/format:list is comma-safe) ---
    try:
        import subprocess
        out = subprocess.run(
            ["wmic", "process", "where", "name='terminal64.exe'", "get", "ProcessId,CommandLine", "/format:list"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0 and not (out.stdout or "").strip():
            return None  # wmic did not run -> cannot enumerate at all
        result = set()
        block = {}
        for raw in (out.stdout or "").splitlines() + [""]:
            line = raw.strip()
            if not line:
                cl = (block.get("CommandLine") or "").lower()
                pid = (block.get("ProcessId") or "").strip()
                if pid and target_dir in cl and "/portable" not in cl:
                    try:
                        result.add(int(pid))
                    except Exception:
                        pass
                block = {}
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                block[k] = v
        return result
    except Exception:
        return None  # neither psutil nor wmic usable -> indeterminate (caller fails closed)


def _terminate_pid(pid) -> bool:
    """Terminate one PID we OWN and VERIFY it is gone. psutil first, then taskkill. Returns True only when the
    process is confirmed no longer running (an honest result: a failed kill must not report success, or the log +
    the never-CREATE guarantee would overstate what happened). Never raises."""
    try:
        import psutil
        try:
            p = psutil.Process(int(pid))
        except psutil.NoSuchProcess:
            return True   # already gone
        try:
            p.kill()
            try:
                p.wait(timeout=3)
            except Exception:
                pass
            return not p.is_running()
        except psutil.NoSuchProcess:
            return True
        except Exception:
            pass  # psutil could not kill (e.g. AccessDenied) -> try taskkill
    except Exception:
        pass
    try:
        import subprocess
        r = subprocess.run(["taskkill", "/PID", str(int(pid)), "/F"], capture_output=True, timeout=10)
        return r.returncode == 0   # taskkill returns 0 only when the process was terminated (or already absent)
    except Exception:
        return False


def guarded_initialize(mt5, init_kwargs, *, probe=None, list_bare_pids=None, terminate=None, force=False) -> bool:
    """Attach to MT5. DARK by default - when neither ``force`` nor MT5_GUARDED_ATTACH is set this is byte-identical
    to ``mt5.initialize(**init_kwargs)`` (behaviour-preserving for the legacy/production bridge). When enabled
    (``force=True`` from the observer, or the env on a persistent-workspace bridge) enforce the never-launch
    invariant via ``evaluate_guarded_attach()``: probe the process BEFORE initialize so a down terminal is never
    launched, require broker-connected + an account identity, else fail closed (releasing any attach we opened).
    NEVER calls ``mt5.login()``; NEVER relaunches. (``force`` lets the observer enforce the guard WITHOUT mutating
    the process-global environment, so it cannot leak the guard into any other code running in the same process.)

    ATTACHABILITY GATE (the never-CREATE guarantee): ``mt5.initialize(path=)`` is dual-mode - it ATTACHES to a
    running terminal, but if it cannot establish IPC (e.g. the ``/portable`` terminal is running but still in
    first-run: compiling MQL5 / downloading the broker catalogue) it LAUNCHES a fresh terminal itself, and its
    own launch omits ``/portable`` -> that bare instance writes to the non-portable data dir (blocked by the
    containment DENY ACL -> the "couldn't create data directory" WebView error) AND makes a duplicate terminal
    that fails observation closed. A process probe alone cannot prevent this (the target IS running, just not
    yet attachable). So we snapshot the bare-terminal PIDs at the target dir, call initialize, and if a NEW bare
    terminal appeared DURING our own call we treat the target as not-attachable: release the attach, neutralise
    EXACTLY the bare instance(s) THIS call spawned (never a ``/portable`` terminal, never a pre-existing stray),
    and fail closed. If the bare-terminal set cannot be enumerated at all (neither psutil nor wmic), we CANNOT
    guarantee never-launch and so fail closed WITHOUT attaching. Net effect: the observer never leaves behind a
    terminal it launched. (A terminal MT5 forks on its own AFTER we return - e.g. a LiveUpdate self-relaunch - is
    outside this single call's window; it is caught by the LocalSystem duplicate_terminal fail-closed guard and
    surfaced by the frozen-observation counter, not by this function.)"""
    if not (force or _guarded_attach_enabled()):
        return bool(mt5.initialize(**init_kwargs))  # legacy passthrough - unchanged

    # Attach-only: the guarded path must NEVER authenticate. mt5.initialize(login=,password=,server=) performs a
    # broker login (it re-authorises the terminal), so credential keys are FORBIDDEN here - initialize may only
    # ATTACH by path.
    if any(k in init_kwargs for k in ("login", "password", "server")):
        logger.warning("guarded_attach rejected: guarded_attach_credentials_forbidden")
        return False

    path = init_kwargs.get("path")
    probe = probe or _terminal_process_running
    list_bare = list_bare_pids or _bare_terminal_pids
    terminate = terminate or _terminate_pid
    # Probe FIRST - a down terminal is rejected here and never launched.
    running = bool(path) and bool(probe(path))
    init_ok = False
    connected = False
    account = None
    identity = None
    if running:
        # Snapshot the bare (non-/portable) terminals at the target dir BEFORE we attach, so we can detect a
        # terminal that our own initialize launches (see the ATTACHABILITY GATE note above). None => the host has
        # neither psutil nor wmic, so we cannot detect/clean a launch -> fail closed WITHOUT attaching.
        bare_before = list_bare(path)
        if bare_before is None:
            logger.warning("guarded_attach rejected: guarded_attach_enum_unavailable")
            return False
        bare_before = set(bare_before)
        try:
            # P0 bounded observation: cap the attach so a BUSY first-run terminal (compiling MQL5 / syncing the
            # broker symbol catalogue) cannot consume the whole host observer wait and starve the cycle. The MT5
            # python default is 60s; an already-running healthy terminal attaches in well under this. A caller that
            # already set an explicit timeout is respected. Fail-fast here surfaces as observation_timeout upstream
            # (a hold, never a launch, never a state mutation).
            attach_kwargs = dict(init_kwargs)
            attach_kwargs.setdefault("timeout", _ATTACH_TIMEOUT_MS)
            init_ok = bool(mt5.initialize(**attach_kwargs))
        except Exception:
            init_ok = False

        # Never-CREATE guarantee: did initialize LAUNCH a bare terminal instead of attaching? If the post-attach
        # enumeration is indeterminate (None) we cannot verify -> release any attach and fail closed.
        bare_after = list_bare(path)
        if bare_after is None:
            if init_ok:
                try:
                    mt5.shutdown()
                except Exception:
                    pass
            logger.warning("guarded_attach rejected: guarded_attach_postcheck_unavailable")
            return False
        spawned = set(bare_after) - bare_before
        if spawned:
            # The target was not actually attachable - neutralise exactly what we spawned and fail closed (a hold).
            try:
                mt5.shutdown()  # release any attach we opened to the just-launched instance
            except Exception:
                pass
            killed = []
            for pid in spawned:
                if terminate(pid):
                    killed.append(pid)
            logger.warning("guarded_attach rejected: guarded_attach_would_launch spawned=%s killed=%s"
                           % (sorted(spawned), sorted(killed)))
            return False

        if init_ok:
            try:
                term = mt5.terminal_info()
                connected = bool(term.connected) if term is not None else False
                account = mt5.account_info()
                if account is not None:
                    login = getattr(account, "login", None)
                    identity = {
                        "login_masked": ("****" + str(login)[-4:]) if login is not None else None,
                        "server": getattr(account, "server", None),
                        "trade_mode": getattr(account, "trade_mode", None),
                    }
            except Exception:
                # A raising terminal_info/account_info IS the degraded state the guard exists for - fail closed.
                # Any attach opened (init_ok True) is released by the `if not ok` shutdown below.
                connected = False
                account = None

    ok, reason = evaluate_guarded_attach(path, running, init_ok, connected, account is not None)
    if not ok:
        if init_ok:
            try:
                mt5.shutdown()  # release the attach we opened; leave no dangling connection
            except Exception:
                pass
        logger.warning(f"guarded_attach rejected: {reason}")
        return False
    logger.info(f"guarded_attach ok: {identity}")  # masked identity read + recorded; never login
    return True
