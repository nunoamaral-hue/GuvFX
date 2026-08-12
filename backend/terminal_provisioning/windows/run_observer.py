"""STREAM 9E - the session-bound Hosted Workspace observer (runs AS guvfx_u_<id>, read-only).

Attach -> observe -> write snapshot -> exit. This is the ONLY code that runs inside the hosted user's own
Windows session and touches the live MT5 terminal, and its responsibility is deliberately narrow: OBSERVE,
NEVER ACT.

It reuses the CERTIFIED M1 Guarded Attach primitive via the self-contained ``observer_attach`` sibling
(``observer_attach.guarded_initialize`` + ``_terminal_process_running`` - a faithful, parity-locked, stdlib-only
copy of the certified helpers, DECOUPLED from the legacy ``scripts.mt5_signal_bridge`` so the observer stages as
a self-contained asset and never drags the legacy signal-copy bridge into a tenant session) so it NEVER launches
MT5, NEVER calls ``mt5.login()``, NEVER authenticates, and NEVER places / modifies / closes an order. The read
surface mirrors the certified ``agent_host`` adapter
(``terminal_info`` / ``account_info`` only). The certified producer -> observation -> decision -> persistence
runs on the BACKEND (Django) from the snapshot this writes; this harness is deliberately Django-free so it
stages as ONE reviewed, self-contained asset with no per-session framework bootstrap.

Fail-closed at every step: a down/ambiguous terminal, a refused attach, an unreadable IPC, or any exception
yields a snapshot in which every UNPROVEN fact is False / every unknown identity is None - never a default
positive. Customer Zero (account 1) is refused up front (defence in depth; the host dispatcher refuses it too).

The snapshot is published ATOMICALLY (temp -> fsync -> os.replace) so a reader (the LocalSystem trigger
primitive) can never see a partially-written file. It carries an observation id, the account id, the epoch
timestamp, the terminal path, and the approved RawWorkspaceSnapshot fields - and NO secret (no password, no
token). ASCII-only.

Usage (invoked on demand by the per-account scheduled task, AS guvfx_u_<id>):
    py "C:\\GuvFX\\observer\\run_observer.py" --account 18
"""
import argparse
import json
import ntpath
import os
import sys
import tempfile
import time
import uuid

ACCOUNTS_BASE = r"C:\GuvFX\accounts"
RESERVED_ACCOUNT_IDS = frozenset({1})   # Customer Zero (guvfx_u_1) - never observed by a hosted observer
SNAPSHOT_SCHEMA = "9e.observer.v1"


def _term_read(mt5):
    """Read terminal_info -> {connected, trade_allowed} (read-only). Any failure -> None (fail closed).
    Mirrors the certified agent_host._term_read."""
    try:
        term = mt5.terminal_info()
    except Exception:
        return None
    if term is None:
        return None
    return {"connected": bool(getattr(term, "connected", False)),
            "trade_allowed": bool(getattr(term, "trade_allowed", False))}


def _acc_read(mt5):
    """Read account_info -> {login, server, trade_mode} (read-only). Any failure -> None (fail closed).
    Mirrors the certified agent_host._acc_read."""
    try:
        acc = mt5.account_info()
    except Exception:
        return None
    if acc is None:
        return None
    return {"login": getattr(acc, "login", None), "server": getattr(acc, "server", None),
            "trade_mode": getattr(acc, "trade_mode", None)}


def _identity(value):
    """Carry an observed identity as its string form (mt5 logins are ints), or None; never raise."""
    if value is None:
        return None
    try:
        return str(value)
    except Exception:
        return None


def _blank_snapshot(account_id, term_path):
    """A snapshot in which every UNPROVEN fact is False / every unknown identity is None."""
    return {
        "schema": SNAPSHOT_SCHEMA,
        "observation_id": uuid.uuid4().hex,
        "account_id": int(account_id),
        "observed_at": time.time(),
        "target_path": term_path,
        "ok": False,
        "process_running": False,
        "attach_attempted": False,
        "attach_succeeded": False,
        "ipc_available": False,
        "terminal_connected": False,
        "trade_allowed": False,
        "observed_login": None,
        "observed_server": None,
        "observed_trade_mode": None,
        "attach_reason": "",
        "process_reason": "",
        "connection_reason": "",
    }


def observe(account_id, *, mt5=None, bridge=None):
    """Produce the read-only snapshot for ``account_id``. ``mt5`` / ``bridge`` are injected for tests; on the
    host they are imported lazily (Windows-only). Never raises. Never launches, logs in, or trades."""
    runtime_root = ntpath.join(ACCOUNTS_BASE, str(int(account_id)))
    term_path = ntpath.join(runtime_root, "terminal", "terminal64.exe")
    snap = _blank_snapshot(account_id, term_path)

    if mt5 is None:
        try:
            import MetaTrader5 as mt5  # noqa: N813 - host-only, Windows
        except Exception:
            snap["process_reason"] = "mt5_unavailable"
            return snap
    if bridge is None:
        # Self-contained M1 Guarded Attach - never launch/login. Resolve in BOTH contexts: the backend package
        # (Django tests) and the flat host layout (run_observer.py staged next to observer_attach.py as tenant).
        try:
            from terminal_provisioning.windows import observer_attach as bridge
        except Exception:
            try:
                import observer_attach as bridge   # flat host context (sibling of this file)
            except Exception:
                snap["attach_reason"] = "attach_helper_unavailable"
                return snap

    # 1. Locate: is the EXPECTED terminal (by its fixed per-account path) already running? Never launch.
    try:
        running = bool(bridge._terminal_process_running(term_path))
    except Exception:
        snap["process_reason"] = "probe_error"
        return snap
    snap["process_running"] = running
    if not running:
        snap["process_reason"] = "terminal_not_running"
        return snap

    # 2. Guarded Attach (M1) - the ONLY way in. It attaches to the already-running terminal by path; it never
    #    launches and never authenticates (no login/password/server passed).
    snap["attach_attempted"] = True
    try:
        ok = bool(bridge.guarded_initialize(mt5, {"path": term_path}))
    except Exception:
        snap["attach_reason"] = "attach_error"
        return snap
    if not ok:
        snap["attach_reason"] = "guarded_attach_refused"
        return snap
    snap["attach_succeeded"] = True
    snap["ipc_available"] = True

    # 3. READ-ONLY broker truth, then release. A failing read degrades to an 'attached but unreadable'
    #    observation (attach/ipc kept; broker-truth facts stay False), never an exception.
    try:
        term = _term_read(mt5)
        acc = _acc_read(mt5)
        if term is not None:
            snap["terminal_connected"] = bool(term.get("connected"))
            snap["trade_allowed"] = bool(term.get("trade_allowed"))
        if acc is not None:
            snap["observed_login"] = _identity(acc.get("login"))
            snap["observed_server"] = _identity(acc.get("server"))
            tm = acc.get("trade_mode")
            snap["observed_trade_mode"] = tm if (isinstance(tm, int) and not isinstance(tm, bool)) else None
        snap["ok"] = True
    except Exception:
        snap["connection_reason"] = "read_error"
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass
    return snap


def result_path(account_id) -> str:
    """The fixed, server-derived per-account result path. Written by guvfx_u_<id>, read by LocalSystem."""
    return ntpath.join(ACCOUNTS_BASE, str(int(account_id)), "_obs", "observation.json")


def atomic_write(path, data) -> None:
    """Publish atomically: write a temp file in the SAME dir, fsync, then os.replace onto the final name so a
    reader never observes a partially-written snapshot."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(json.dumps(data, separators=(",", ":"), sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Session-bound Hosted Workspace observer (read-only).")
    parser.add_argument("--account", type=int, required=True)
    args = parser.parse_args(argv)
    account_id = args.account
    if account_id <= 0 or account_id in RESERVED_ACCOUNT_IDS:
        # Never observe / never create result paths for Customer Zero or an invalid id.
        sys.stdout.write(json.dumps({"ok": False, "reason": "reserved_or_invalid_account"}))
        return 2
    snap = observe(account_id)
    atomic_write(result_path(account_id), snap)
    sys.stdout.write(json.dumps({"ok": bool(snap.get("ok")), "account_id": account_id,
                                 "observation_id": snap.get("observation_id")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
