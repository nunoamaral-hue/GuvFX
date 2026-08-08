"""ADR-0034 / M3b-2 — the Hosted Workspace Agent's read-only observation pipeline (pure orchestration, DARK).

The agent turns an *already-running, already-broker-connected* Hosted Workspace into a trusted
``RawWorkspaceSnapshot`` and, from it, the canonical M3a ``WorkspaceObservation``. It is the ONE component
that touches the live workspace terminal, and its responsibility is deliberately narrow: **observe, never
act**. It:

- uses ONLY the M1 Guarded-Attach primitive to reach the terminal — it NEVER launches MT5, NEVER calls
  ``mt5.login()``, NEVER authenticates, and NEVER places/modifies/closes an order;
- performs NO persistence, NO telemetry, NO recovery, NO strategy pause/resume;
- derives NO lifecycle state (that is the Workspace Manager's sole responsibility — M3a);
- fails closed — every host failure becomes an OBSERVATION (all-False / explicit-unknown facts), never an
  exception to the consumer, and never a default positive;
- is deterministic where it counts — ``clock`` is injected (no hidden wall-clock), and every host
  interaction goes through an injected ``host`` boundary so the whole pipeline is unit/mutation-testable
  with a mock MT5 on any platform.

Flow (no component may bypass it):

    Hosted Workspace Agent
      -> host.locate(spec)        # is the EXPECTED terminal running (and unambiguous)?
      -> host.attach(spec)        # M1 Guarded Attach — never launch, never login; else fail closed
      -> host.read_state(spec)    # READ-ONLY: terminal_info / account_info / positions / orders / tick
      -> RawWorkspaceSnapshot     # explicit facts, no inference
      -> build_workspace_observation(...)   # M3b-1 producer (certified)
      -> WorkspaceObservation     # -> Workspace Manager (M3a)

The concrete ``host`` for the live Windows workspace is ``agent_host.Mt5WorkspaceHost``, which binds this
pipeline to the M1 primitive + a real ``mt5`` handle by injection (no MetaTrader5 import here).
"""
from dataclasses import dataclass
from typing import Mapping, Optional

from hosted_workspace.producer import (
    DEFAULT_CLOCK_TOLERANCE_SECONDS,
    RawWorkspaceSnapshot,
    _is_number,
    _is_true,
    build_workspace_observation,
)
from hosted_workspace.state_machine import WorkspaceReason


@dataclass(frozen=True)
class WorkspaceSpec:
    """The EXPECTED workspace the agent is asked to observe — its binding, not observed truth. Carries no
    secret: identity is the (non-secret) expected login/server numbers, plus the fixed terminal path."""
    workspace_id: str = ""
    expected_login: Optional[str] = None
    expected_server: Optional[str] = None
    target_path: Optional[str] = None
    target_pid: Optional[int] = None
    freshness_limit_seconds: Optional[float] = None
    tick_symbol: Optional[str] = None  # optional symbol for the (optional) read-only tick probe


@dataclass(frozen=True)
class ProcessProbe:
    """Result of locating the expected terminal process. ``duplicate`` means the target is AMBIGUOUS (more
    than one candidate) — the agent must then refuse to attach rather than risk the wrong workspace."""
    running: bool = False
    pid: Optional[int] = None
    duplicate: bool = False
    reason: str = ""


@dataclass(frozen=True)
class AttachOutcome:
    """Result of the M1 Guarded Attach. ``ok`` is True only when the guarded attach succeeded (already
    running + connected + account present); ``ipc_available`` reflects a usable IPC handle after attach."""
    attempted: bool = False
    ok: bool = False
    ipc_available: bool = False
    reason: str = ""


@dataclass(frozen=True)
class HostReadState:
    """The read-only facts gathered AFTER a successful guarded attach. ``terminal``/``account`` are the
    (non-secret) mappings the snapshot needs; position/order counts + tick presence are read to exercise
    the read-only surface and prove blast-radius, and are NOT part of the certified snapshot contract."""
    terminal: Optional[Mapping] = None   # {connected, trade_allowed}
    account: Optional[Mapping] = None    # {login, server, trade_mode}
    position_count: Optional[int] = None
    order_count: Optional[int] = None
    tick_present: Optional[bool] = None


def _get(mapping, key):
    """Safe read from a host mapping — never raises, never invents a value. Non-mapping / missing -> None."""
    if isinstance(mapping, Mapping):
        return mapping.get(key)
    return None


def _identity(value):
    """Faithfully carry an observed identity as its string form (mt5 logins are ints), or None. This is NOT
    sanitisation — the certified producer (_clean_identity) still decides blank/whitespace/None. The agent
    only normalises TYPE, never infers or defaults a value. Fail-closed: an untrusted value whose __str__
    raises degrades to None (an explicit unknown), never an exception to the consumer."""
    if value is None:
        return None
    try:
        return str(value)
    except Exception:
        return None


def _safe_str(value):
    """``str(value or "")`` fail-closed: an untrusted reason value whose ``__str__``/``__bool__`` raises
    degrades to "" (never an exception to the consumer)."""
    try:
        return str(value or "")
    except Exception:
        return ""


def _safe_clock(clock):
    """Read the injected clock once, fail-closed: a missing/raising/non-finite clock yields None so the
    snapshot's observed_at is an explicit unknown (and the producer then computes fresh=False)."""
    try:
        value = clock()
    except Exception:
        return None
    return value if _is_number(value) else None


def _fail_closed_snapshot(spec, observed_at, *, process_running=False, attach_attempted=False,
                          attach_succeeded=False, ipc_available=False, process_reason="",
                          attach_reason="", connection_reason=""):
    """A snapshot in which every UNPROVEN fact is False and every unknown identity is None — never a default
    positive. Only facts the agent actually established (e.g. process_running when attach later failed) are
    carried through; everything downstream of the failure stays fail-closed."""
    return RawWorkspaceSnapshot(
        workspace_id=str(getattr(spec, "workspace_id", "") or ""),
        expected_login=getattr(spec, "expected_login", None),
        expected_server=getattr(spec, "expected_server", None),
        target_pid=getattr(spec, "target_pid", None),
        target_path=getattr(spec, "target_path", None),
        process_running=bool(process_running),
        attach_attempted=bool(attach_attempted),
        attach_succeeded=bool(attach_succeeded),
        ipc_available=bool(ipc_available),
        terminal_connected=False,
        trade_allowed=False,
        observed_login=None,
        observed_server=None,
        observed_trade_mode=None,
        observed_at=observed_at,
        freshness_limit_seconds=getattr(spec, "freshness_limit_seconds", None),
        attach_reason=str(attach_reason or ""),
        process_reason=str(process_reason or ""),
        connection_reason=str(connection_reason or ""),
    )


def _release(host):
    """Best-effort release of the read-only attach — leave no dangling IPC handle. Never raises."""
    try:
        release = getattr(host, "release", None)
        if callable(release):
            release()
    except Exception:
        pass


def build_agent_snapshot(host, spec, *, clock):
    """Observe the workspace and return a fully-populated ``RawWorkspaceSnapshot``. Pure orchestration over
    an injected ``host`` boundary; fail-closed at every step; never raises to the caller; never launches,
    logs in, or mutates. ``observed_at`` is stamped ONCE from the injected clock at the start of observation.
    """
    observed_at = _safe_clock(clock)

    # 1. Locate the EXPECTED terminal. Never attach to a down or AMBIGUOUS target. The probe object is an
    #    INJECTED/untrusted value, so its attribute reads are guarded: a raising locate OR a raising probe
    #    attribute (property / __bool__ / __str__) degrades to a fail-closed, not-running/ambiguous probe.
    try:
        probe = host.locate(spec)
        process_running = _is_true(getattr(probe, "running", False))
        # Fail-closed ambiguity gate: ONLY an explicit ``duplicate is False`` clears it. A truthy non-bool,
        # None, or a missing attribute is treated as ambiguous (refuse) — the safety gate never fails open.
        ambiguous = getattr(probe, "duplicate", True) is not False
        probe_reason = _safe_str(getattr(probe, "reason", ""))
        probe_pid = getattr(probe, "pid", None)
    except Exception:
        process_running, ambiguous, probe_reason, probe_pid = False, True, "locate_error", None
    if (not process_running) or ambiguous:
        reason = probe_reason or ("duplicate_terminal_ambiguous" if ambiguous else "terminal_not_running")
        return _fail_closed_snapshot(spec, observed_at, process_running=process_running,
                                     process_reason=reason)

    # 2 + 3. Guarded Attach (M1) — the ONLY way in — then READ-ONLY reads. From the moment ``host.attach``
    #    is invoked, the attach is released EXACTLY ONCE via the single ``finally`` (no dangling live IPC
    #    handle can survive to be inherited by a later observation), regardless of where a subsequent
    #    exception lands. The attach RESULT object is INJECTED/untrusted, so its attribute reads are guarded
    #    too (the outer ``except`` below), symmetrically with the read-state reads (the inner ``except``).
    #    Never retry, never launch, never recover.
    ipc_available = False
    try:
        attach = host.attach(spec)
        attach_attempted = _is_true(getattr(attach, "attempted", False))
        attach_ok = _is_true(getattr(attach, "ok", False))
        ipc_available = attach_ok and _is_true(getattr(attach, "ipc_available", False))
        attach_reason = _safe_str(getattr(attach, "reason", ""))
        if not attach_ok:
            snapshot = _fail_closed_snapshot(spec, observed_at, process_running=True,
                                             attach_attempted=attach_attempted,
                                             attach_reason=attach_reason or "guarded_attach_refused")
        else:
            # Attach genuinely succeeded. Reads + mapping of untrusted host values are fail-closed: a raising
            # read / .get / __str__ degrades to an 'attached but unreadable' observation (attach + ipc kept
            # for the Manager; broker-truth facts False), never an exception to the consumer.
            try:
                state = host.read_state(spec)
                terminal = getattr(state, "terminal", None)
                account = getattr(state, "account", None)
                snapshot = RawWorkspaceSnapshot(
                    workspace_id=str(getattr(spec, "workspace_id", "") or ""),
                    expected_login=getattr(spec, "expected_login", None),
                    expected_server=getattr(spec, "expected_server", None),
                    target_pid=probe_pid,
                    target_path=getattr(spec, "target_path", None),
                    process_running=True,
                    attach_attempted=True,
                    attach_succeeded=True,
                    ipc_available=ipc_available,
                    terminal_connected=_is_true(_get(terminal, "connected")),
                    trade_allowed=_is_true(_get(terminal, "trade_allowed")),
                    observed_login=_identity(_get(account, "login")),
                    observed_server=_identity(_get(account, "server")),
                    observed_trade_mode=_get(account, "trade_mode"),  # producer rejects bool; None -> deny
                    observed_at=observed_at,
                    freshness_limit_seconds=getattr(spec, "freshness_limit_seconds", None),
                    attach_reason=attach_reason,
                    process_reason=probe_reason,
                    connection_reason="",
                )
            except Exception:
                snapshot = _fail_closed_snapshot(spec, observed_at, process_running=True,
                                                 attach_attempted=True, attach_succeeded=True,
                                                 ipc_available=ipc_available, connection_reason="read_error")
    except Exception:
        # host.attach or the attach-outcome extraction raised — the attach cannot be trusted: fail closed
        # fully (nothing positive survives), and the finally still releases any handle that was opened.
        snapshot = _fail_closed_snapshot(spec, observed_at, process_running=True, attach_attempted=True,
                                         attach_succeeded=False, ipc_available=False,
                                         attach_reason="attach_error")
    finally:
        _release(host)  # read-only observer: release the attach exactly once, leave no dangling handle
    return snapshot


def observe_workspace(host, spec, *, clock, previous_state,
                      previous_reason=WorkspaceReason.NONE,
                      clock_tolerance_seconds=DEFAULT_CLOCK_TOLERANCE_SECONDS):
    """The agent's single public entry-point: observe the workspace and return the canonical
    ``WorkspaceObservation`` — and NOTHING more (no action, no persistence, no telemetry, no state
    derivation). ``now`` for freshness is the snapshot's own observation instant (a missing timestamp
    yields fresh=False in the certified producer)."""
    snapshot = build_agent_snapshot(host, spec, clock=clock)
    now = snapshot.observed_at if _is_number(snapshot.observed_at) else 0.0
    return build_workspace_observation(
        snapshot, now=now, previous_state=previous_state, previous_reason=previous_reason,
        clock_tolerance_seconds=clock_tolerance_seconds)
