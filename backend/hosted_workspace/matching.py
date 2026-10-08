"""hosted_workspace.matching — pure, fail-closed active-account-match decision (ADR-0033).

Backend mirror of ``scripts/mt5_signal_bridge.py::evaluate_binding`` for the Hosted Persistent MT5
ATTACH model. Given a point-in-time OBSERVATION of the user's persistent terminal (which broker
account it is CURRENTLY logged into — read host-side via attach + ``account_info``/``terminal_info``,
never a login) and the account a strategy is bound to, decide whether that terminal is safe to execute
the strategy against.

Design contract (mirrors the certified bridge gate, adapted for a user-owned multi-account terminal):

* Pure and I/O-free — NO Django, NO MetaTrader5, NO network. Unit- and mutation-testable in isolation
  (tests_matching.py). The backend cannot import MetaTrader5; the observation is produced host-side and
  reaches this function as plain scalars.
* Fail CLOSED — every missing / ambiguous field, and every mismatch, DENIES. ``ok`` is True only when
  every applicable check passes.
* The identity pin is MANDATORY (both login and server). Unlike the bridge's demo default (where the
  pin is optional), a user-owned terminal lets the customer switch the active Navigator account at any
  moment, so "which account is active" must always be pinned — an unpinned match is treated as unsafe.
* This is DELIBERATELY NOT the live order-time boundary in this increment. The authoritative gate
  before every ``order_send`` remains ``evaluate_binding`` inside the bridge process. Wiring this
  decision into ``execution/broker_gate.py`` is an Amber change deferred to a later increment pending
  ADR-0033 acceptance (it must first reconcile with the existing ``password_enc`` + ``VALIDATED`` gate
  preconditions, which the attach model does not satisfy).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

# MT5 trade_mode encoding (repeated for a standalone module): 0=DEMO, 1=CONTEST, 2=REAL.
TRADE_MODE_DEMO = 0


def _norm_server(s) -> str:
    """Normalize a broker SERVER name for comparison: strip + casefold. MT5 server names are case-INSENSITIVE
    identifiers (the terminal connects to the same server regardless of the case typed), so the identity match on
    the server must compare case-insensitively — otherwise a stored ``Tradersway-Live`` never matches the terminal's
    ``TradersWay-Live`` (observed Account-46 case mismatch, 2026-10-08). This normalizes ONLY the server field;
    ``login`` stays an EXACT string match and the DEMO/LIVE environment stays an EXACT match — neither is weakened.
    No new identity collision: a casefold-equal server is the same broker server, and login+environment still gate."""
    return (str(s) if s is not None else "").strip().casefold()


@dataclass(frozen=True)
class WorkspaceObservation:
    """A point-in-time read of the user's persistent MT5 terminal, as plain scalars a host-side attach
    probe emits as JSON. Contains NO secret: a broker LOGIN id is an identifier (like
    TradingAccount.account_number), never a credential; no password ever appears here."""
    process_running: bool = False
    ipc_available: bool = False
    connected: Optional[bool] = None
    trade_allowed: Optional[bool] = None
    login: Optional[str] = None
    server: Optional[str] = None
    trade_mode: Optional[int] = None
    observed_at: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class ExpectedAccount:
    """The account a strategy is bound to (from the DB TradingAccount). ``login`` is account_number,
    ``server`` the broker server name. ``allow_live`` gates non-demo execution and defaults False."""
    login: Optional[str] = None
    server: Optional[str] = None
    is_demo: bool = True
    allow_live: bool = False


@dataclass(frozen=True)
class MatchDecision:
    ok: bool
    reason: str


def evaluate_active_account_match(obs: WorkspaceObservation, expected: ExpectedAccount) -> MatchDecision:
    """Pure fail-closed decision: is ``obs`` (the terminal's live active account) safe to execute the
    strategy bound to ``expected``? Returns MatchDecision(ok, reason). ``ok`` is True ONLY when every
    check passes; ``reason`` is a stable, secret-free lower_snake code (never contains a login)."""
    # Workspace liveness first — distinct reasons so an operator can tell "not running" from "running
    # but no IPC" from "IPC up but broker not connected".
    if not obs.process_running:
        return MatchDecision(False, "workspace_not_running")
    if not obs.ipc_available:
        return MatchDecision(False, "workspace_ipc_unavailable")
    # Broker/terminal truth. connected / trade_allowed may be None (unknown) — anything but True denies.
    if obs.connected is not True:
        return MatchDecision(False, "terminal_not_connected")
    if obs.trade_allowed is not True:
        return MatchDecision(False, "trade_not_allowed")
    # The active-account identity must be fully observable.
    if obs.login is None:
        return MatchDecision(False, "active_login_unavailable")
    if obs.server is None:
        return MatchDecision(False, "active_server_unavailable")
    if obs.trade_mode is None:
        return MatchDecision(False, "trade_mode_unavailable")
    # Mandatory identity pin — an unpinned expected account is unsafe in a user-switchable terminal.
    if not expected.login:
        return MatchDecision(False, "expected_login_unconfigured")
    if not expected.server:
        return MatchDecision(False, "expected_server_unconfigured")
    # The active account must be EXACTLY the bound account.
    if str(obs.login) != str(expected.login):              # login EXACT (unchanged) — the tenant discriminator
        return MatchDecision(False, "active_account_login_mismatch")
    if _norm_server(obs.server) != _norm_server(expected.server):   # server CASE-INSENSITIVE (MT5 identifier semantics)
        return MatchDecision(False, "active_account_server_mismatch")
    # Demo/live classification agreement — the observed terminal's environment must EXACTLY EQUAL the
    # account's expected environment, fail-closed in BOTH directions: a DEMO account must never match a LIVE
    # terminal, and a LIVE account must never match a DEMO terminal. (The prior check was asymmetric — it only
    # caught demo-expected↔live-observed — which was safe only because the expected environment was always
    # hardcoded demo; D3 lets a LIVE account carry its real environment, so the symmetric check is required.)
    # This is an IDENTITY/ENVIRONMENT match, NOT an execution authorization: execution for a LIVE account is
    # gated separately (readiness condition 11 + the §3 authorization in D4 + the live order-time bridge
    # gate), never here. Byte-identical for a demo-expected account (the only case before D3).
    is_demo_account = (obs.trade_mode == TRADE_MODE_DEMO)
    if is_demo_account != expected.is_demo:
        return MatchDecision(False, "classification_mismatch")
    # A LIVE account may be MATCHED only when live matching is explicitly permitted for it (``allow_live``);
    # with it False a live terminal is refused, so a LIVE account stays inert unless the D3 producer opts it
    # in (flag-gated). DEMO is always permitted. This remains an identity gate, not an order authorization.
    if not is_demo_account and not expected.allow_live:
        return MatchDecision(False, "live_execution_not_authorised")
    return MatchDecision(True, "ok")


def normalize_observation(snapshot: Optional[Mapping]) -> WorkspaceObservation:
    """Build a WorkspaceObservation from a host-side attach-probe JSON snapshot. None-safe; NEVER
    raises — a malformed/absent snapshot yields a fail-closed observation whose fields are None/False."""
    if not isinstance(snapshot, Mapping):
        return WorkspaceObservation(reason="observation_unavailable")

    def _tri(key: str) -> Optional[bool]:
        v = snapshot.get(key)
        return v if isinstance(v, bool) else None

    try:
        login = snapshot.get("login")
        server = snapshot.get("server")
        tm = snapshot.get("trade_mode")
        observed_at = snapshot.get("observed_at")
        return WorkspaceObservation(
            process_running=bool(snapshot.get("process_running", False)),
            ipc_available=bool(snapshot.get("ipc_available", False)),
            connected=_tri("connected"),
            trade_allowed=_tri("trade_allowed"),
            login=(str(login) if login is not None else None),
            server=(str(server) if server is not None else None),
            trade_mode=(tm if isinstance(tm, int) and not isinstance(tm, bool) else None),
            observed_at=(str(observed_at) if observed_at is not None else None),
            reason=str(snapshot.get("reason", "")),
        )
    except Exception:
        # Categorically fail closed: any pathological value (e.g. a __str__ that raises) yields a
        # fail-closed observation — never a raise, and never a pass.
        return WorkspaceObservation(reason="observation_unavailable")
