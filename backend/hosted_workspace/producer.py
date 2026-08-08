"""ADR-0034 / M3b-1 — the Workspace Observation Producer (pure, DARK).

The trusted boundary between raw host/MT5/attach facts and the pure M3a Workspace Manager. It converts a
``RawWorkspaceSnapshot`` into the canonical ``manager.WorkspaceObservation`` and NOTHING ELSE. It:
- describes reality only — it NEVER derives canonical lifecycle state (that is the Workspace Manager's sole
  responsibility);
- performs NO actions — no Windows/MT5/attach/persistence/telemetry/execution/tasks/Guacamole;
- is pure and deterministic — `now` is supplied (no wall-clock);
- fails closed — unknown/malformed raw data never becomes a positive fact;
- is secret-free — the output carries no credential; the raw login exists only transiently inside the
  certified identity comparison and is never emitted here.

Data flow:  Guarded-Attach / host facts  ->  RawWorkspaceSnapshot  ->  build_workspace_observation(...)
            ->  manager.WorkspaceObservation  ->  Workspace Manager (M3a).
"""
import math
from dataclasses import dataclass
from typing import Optional

from hosted_workspace.manager import WorkspaceObservation
from hosted_workspace.matching import (
    ExpectedAccount,
    WorkspaceObservation as MatchObservation,
    evaluate_active_account_match,
)
from hosted_workspace.state_machine import WorkspaceReason

DEFAULT_CLOCK_TOLERANCE_SECONDS = 5.0


@dataclass(frozen=True)
class RawWorkspaceSnapshot:
    """Untrusted raw Hosted Workspace runtime facts. NEVER carries a broker password, ``accounts.dat``
    contents, token, secret, or keyring material — identity is limited to the (non-secret) login/server
    numbers required for the account-match comparison."""
    workspace_id: str = ""
    # Expected identity (from the workspace/account binding supplied to the producer).
    expected_login: Optional[str] = None
    expected_server: Optional[str] = None
    # Process
    target_pid: Optional[int] = None
    target_path: Optional[str] = None
    process_running: Optional[bool] = None
    # Attach (produced by the M1 Guarded-Attach primitive in the future M3b-2 host layer)
    attach_attempted: Optional[bool] = None
    attach_succeeded: Optional[bool] = None
    ipc_available: Optional[bool] = None
    # Terminal
    terminal_connected: Optional[bool] = None
    trade_allowed: Optional[bool] = None
    # Active account (observed)
    observed_login: Optional[str] = None
    observed_server: Optional[str] = None
    observed_trade_mode: Optional[int] = None
    # Freshness
    observed_at: Optional[float] = None
    freshness_limit_seconds: Optional[float] = None
    # Optional diagnostics (non-secret, not emitted into the observation)
    attach_reason: str = ""
    process_reason: str = ""
    connection_reason: str = ""


def _is_true(value):
    """Strict fail-closed truth: ONLY the literal ``True`` is true. ``None`` (unknown) or any other value
    (including truthy non-bools like ``1`` or ``"true"``) is False — uncertainty never becomes a positive."""
    return value is True


def _is_number(value):
    """A usable real number: an int or float, NOT a bool, and FINITE. NaN/inf are floats, so without the
    isfinite guard a NaN timestamp/limit would defeat every ordered freshness check (all NaN comparisons are
    False) and fail OPEN — adversarial finding (HIGH). Rejecting non-finite closes that at the type gate."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clean_identity(value):
    """A genuine non-blank identity, stripped, else None. A None/blank/whitespace-only pin is 'unavailable'
    so the certified matcher denies (an unpinned/blank identity must never be an exact match) — finding."""
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _clean_trade_mode(value):
    """A genuine integer trade_mode, else None (matcher denies via trade_mode_unavailable). Rejects bool —
    otherwise ``False == TRADE_MODE_DEMO(0)`` would classify a malformed bool as a DEMO account — finding."""
    return value if (isinstance(value, int) and not isinstance(value, bool)) else None


def _compute_freshness(observed_at, now, limit, tolerance):
    """Deterministic freshness from a SUPPLIED ``now`` (no wall-clock access). Fail-closed on: a missing or
    malformed timestamp, a missing/non-positive limit, staleness (``age > limit``), or a future observation
    beyond the permitted clock tolerance (``age < -tolerance``). The boundary ``age == limit`` is FRESH."""
    if not (_is_number(observed_at) and _is_number(now) and _is_number(limit)):
        return False
    if limit <= 0:
        return False
    # A non-finite/garbage tolerance must never DISABLE the future-observation guard: a NaN/inf tolerance
    # would make `age < -abs(tolerance)` silently False for any age, so default to zero tolerance
    # (fail-closed) rather than trusting the operand. (`tolerance` is config, not snapshot data — this is
    # defense-in-depth, not an attacker-reachable path.)
    tol = abs(tolerance) if _is_number(tolerance) else 0.0
    age = now - observed_at
    if age < -tol:
        return False
    if age > limit:
        return False
    return True


def _account_match(snapshot):
    """Isolate the certified identity match (login + server + demo/live classification) by giving
    ``evaluate_active_account_match`` a readiness-neutral observation — so only the identity checks decide.
    Fail-closed: missing/partial/unknown identity or a demo/live mismatch yields False."""
    observed = MatchObservation(
        process_running=True, ipc_available=True, connected=True, trade_allowed=True,
        login=_clean_identity(snapshot.observed_login), server=_clean_identity(snapshot.observed_server),
        trade_mode=_clean_trade_mode(snapshot.observed_trade_mode))
    expected = ExpectedAccount(
        login=_clean_identity(snapshot.expected_login), server=_clean_identity(snapshot.expected_server),
        is_demo=True, allow_live=False)
    return bool(evaluate_active_account_match(observed, expected).ok)


def build_workspace_observation(snapshot, *, now, previous_state,
                                previous_reason=WorkspaceReason.NONE,
                                clock_tolerance_seconds=DEFAULT_CLOCK_TOLERANCE_SECONDS):
    """Convert raw facts into the canonical M3a ``WorkspaceObservation``. Pure, fail-closed, secret-free, and
    NEVER derives lifecycle state. ``now`` and ``previous_state`` are supplied (the producer neither reads a
    clock nor derives the previous state — it carries it through for the Manager)."""
    try:
        process_running = _is_true(snapshot.process_running)
        # IPC is usable only when the guarded attach SUCCEEDED and IPC is reported available.
        ipc_available = _is_true(snapshot.attach_succeeded) and _is_true(snapshot.ipc_available)
        connected = _is_true(snapshot.terminal_connected)
        trade_allowed = _is_true(snapshot.trade_allowed)
        account_match = _account_match(snapshot)
        fresh = _compute_freshness(
            snapshot.observed_at, now, snapshot.freshness_limit_seconds, clock_tolerance_seconds)
        observed_at = snapshot.observed_at if _is_number(snapshot.observed_at) else None
    except Exception:
        # No exception may fall through into a permissive observation — collapse to fully fail-closed.
        process_running = ipc_available = connected = trade_allowed = account_match = fresh = False
        observed_at = None

    return WorkspaceObservation(
        process_running=process_running,
        ipc_available=ipc_available,
        connected=connected,
        account_match=account_match,
        trade_allowed=trade_allowed,
        fresh=fresh,
        previous_state=str(previous_state),
        previous_reason=str(previous_reason),
        observed_at=observed_at,
    )
