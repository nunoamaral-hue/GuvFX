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
import os

logger = logging.getLogger("guvfx.hosted_workspace.observer_attach")


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


def guarded_initialize(mt5, init_kwargs, *, probe=None) -> bool:
    """Attach to MT5. DARK by default - when MT5_GUARDED_ATTACH is unset this is byte-identical to
    ``mt5.initialize(**init_kwargs)`` (behaviour-preserving). When enabled, enforce the never-launch invariant
    via ``evaluate_guarded_attach()``: probe the process BEFORE initialize so a down terminal is never launched,
    require broker-connected + an account identity, else fail closed (releasing any attach we opened). NEVER
    calls ``mt5.login()``; NEVER relaunches."""
    if not _guarded_attach_enabled():
        return bool(mt5.initialize(**init_kwargs))  # legacy passthrough - unchanged

    # Attach-only: the guarded path must NEVER authenticate. mt5.initialize(login=,password=,server=) performs a
    # broker login (it re-authorises the terminal), so credential keys are FORBIDDEN here - initialize may only
    # ATTACH by path.
    if any(k in init_kwargs for k in ("login", "password", "server")):
        logger.warning("guarded_attach rejected: guarded_attach_credentials_forbidden")
        return False

    path = init_kwargs.get("path")
    probe = probe or _terminal_process_running
    # Probe FIRST - the attach below is reached only when a terminal is already running, so it can only ATTACH,
    # never launch. (A sub-millisecond probe->attach TOCTOU window remains: if the terminal exits in the gap,
    # initialize(path=) could launch it - but that worst case equals the legacy path, never worse.)
    running = bool(path) and bool(probe(path))
    init_ok = False
    connected = False
    account = None
    identity = None
    if running:
        try:
            init_ok = bool(mt5.initialize(**init_kwargs))
            if init_ok:
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
            # A raising initialize/terminal_info/account_info IS the degraded state the guard exists for - fail
            # closed. Any attach opened (init_ok True) is released by the `if not ok` shutdown below.
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
