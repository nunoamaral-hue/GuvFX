"""ADR-0034 / M3b-2 — the reference Hosted Workspace host adapter (binds the pure agent to M1 + live mt5).

``Mt5WorkspaceHost`` is the ONLY place the read-only observation pipeline meets a real MT5 handle. It is a
thin, read-only adapter that satisfies the ``agent`` host boundary (``locate`` / ``attach`` / ``read_state``
/ ``release``) by delegating to:

- the **M1 Guarded-Attach primitive** for the single attach step (injected ``guarded_initialize`` +
  ``terminal_process_running``) — never launch, never login, credential keys forbidden by M1 itself; and
- **read-only** ``mt5`` calls (``terminal_info`` / ``account_info`` / ``positions_get`` / ``orders_get`` /
  ``symbol_info_tick``) for the observed facts.

Everything is INJECTED — there is no ``import MetaTrader5`` and no ``import`` of the M1 bridge module here.
That keeps this file importable (and unit-testable with a spy ``mt5`` + fake M1) on any platform, and it is
the boundary at which the M1 branch (#305) and the M3b-1 stack (#309) are wired together on the host — a
merge-sequencing decision that belongs to the Sponsor, not to this module.

STRICT: this adapter MUST NOT call ``mt5.login()``, MUST NOT call ``mt5.initialize()`` except through the
injected M1 ``guarded_initialize`` (which never launches/authenticates), and MUST NOT place, modify, or
close any order. It reads, and it releases.
"""
from hosted_workspace.agent import AttachOutcome, HostReadState, ProcessProbe


def _term_read(mt5):
    """Read terminal_info -> {connected, trade_allowed} (read-only). Any failure -> None (fail closed)."""
    try:
        term = mt5.terminal_info()
    except Exception:
        return None
    if term is None:
        return None
    return {
        "connected": bool(getattr(term, "connected", False)),
        "trade_allowed": bool(getattr(term, "trade_allowed", False)),
    }


def _acc_read(mt5):
    """Read account_info -> {login, server, trade_mode} (read-only). Any failure -> None (fail closed)."""
    try:
        acc = mt5.account_info()
    except Exception:
        return None
    if acc is None:
        return None
    return {
        "login": getattr(acc, "login", None),
        "server": getattr(acc, "server", None),
        "trade_mode": getattr(acc, "trade_mode", None),
    }


def _safe_len(fn):
    """Length of a read-only collection call (positions_get/orders_get), or None on any failure."""
    try:
        result = fn()
    except Exception:
        return None
    try:
        return len(result) if result is not None else 0
    except Exception:
        return None


def _tick_present(mt5, symbol):
    """Whether a read-only tick is available for ``symbol``. None symbol -> not probed (None)."""
    if not symbol:
        return None
    try:
        tick = mt5.symbol_info_tick(symbol)
    except Exception:
        return False
    return tick is not None


class Mt5WorkspaceHost:
    """Read-only host adapter. Construct with a live ``mt5`` module/handle and the M1 primitives, injected:

        host = Mt5WorkspaceHost(
            mt5,
            guarded_initialize=bridge.guarded_initialize,          # M1 (#305) — never launch/login
            terminal_process_running=bridge._terminal_process_running,
        )

    It performs the guarded attach via M1 and then reads broker truth read-only. It never authenticates,
    never launches, never trades.
    """

    def __init__(self, mt5, *, guarded_initialize, terminal_process_running):
        self._mt5 = mt5
        self._guarded_initialize = guarded_initialize
        self._terminal_process_running = terminal_process_running

    def locate(self, spec):
        """Is the EXPECTED terminal (by its fixed install path) already running? Fail-closed: no path or an
        unconfirmable process set -> not running. Duplicate detection is a host-cert responsibility proven
        out-of-band (M1's dir set cannot count duplicates); the agent still refuses an ambiguous target."""
        path = getattr(spec, "target_path", None)
        if not path:
            return ProcessProbe(running=False, reason="no_path")
        try:
            running = bool(self._terminal_process_running(path))
        except Exception:
            return ProcessProbe(running=False, reason="probe_error")
        return ProcessProbe(running=running, reason="" if running else "terminal_not_running")

    def attach(self, spec):
        """Attach via the M1 Guarded-Attach primitive ONLY. Passes just ``path`` (no login/password/server),
        so M1 can only ATTACH, never authenticate; M1 itself refuses credential keys and never launches."""
        path = getattr(spec, "target_path", None)
        try:
            ok = bool(self._guarded_initialize(self._mt5, {"path": path}))
        except Exception:
            return AttachOutcome(attempted=True, ok=False, ipc_available=False, reason="attach_error")
        return AttachOutcome(attempted=True, ok=ok, ipc_available=ok,
                             reason="ok" if ok else "guarded_attach_refused")

    def read_state(self, spec):
        """READ-ONLY broker truth after a successful attach. Never mutates; a failing read degrades to None
        / a fail-closed count rather than raising."""
        return HostReadState(
            terminal=_term_read(self._mt5),
            account=_acc_read(self._mt5),
            position_count=_safe_len(self._mt5.positions_get),
            order_count=_safe_len(self._mt5.orders_get),
            tick_present=_tick_present(self._mt5, getattr(spec, "tick_symbol", None)),
        )

    def release(self):
        """Release the read-only attach — leave no dangling IPC handle. Best-effort; never raises."""
        try:
            self._mt5.shutdown()
        except Exception:
            pass
