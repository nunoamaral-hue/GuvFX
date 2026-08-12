"""hosted_workspace.live_observe — STREAM 9E: the REAL production ``observe_fn`` (replaces ``_dark_observe_fn``).

Turns an eligible Hosted Workspace into a live ``WorkspaceObservation`` by asking the host — over the SIGNED
executor's read-only ``OBSERVE_WORKSPACE`` op — to run that account's session-bound observer and return a
``RawWorkspaceSnapshot``, then feeding it through the EXISTING certified producer (``build_workspace_observation``).
It adds NO state and NO second state machine: it is a transport + a fail-closed mapping only. The Workspace
Manager (M3a) + single writer (M3c consumer) remain the sole deciders/persisters.

Fail-closed at EVERY step — flag off, ineligible state, no account, executor unresolved (host-executor flag/
config off), transport error, host ``ok:false``, or malformed snapshot ⇒ returns ``None`` (the runner ingests
nothing → the workspace's freshness lapses → readiness holds). Observation is CAPABILITY only: nothing here
arms execution, logs in, provisions, confirms an account, or mutates the host.

The expected identity (login/server) is read from the WORKSPACE's own ``trading_account`` (server-owned) and
fed to the certified producer — NEVER taken from the host result — so a compromised/rogue host reply can never
assert its own identity and force ``account_match`` (the producer compares observed-vs-expected).
"""
from __future__ import annotations

import logging

from hosted_workspace.flags import hosted_mt5_observation_enabled
from hosted_workspace.producer import (
    DEFAULT_CLOCK_TOLERANCE_SECONDS,
    RawWorkspaceSnapshot,
    build_workspace_observation,
)
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from hosted_workspace.state_machine import WorkspaceReason

logger = logging.getLogger("guvfx.hosted_workspace")

# States where a fresh observation can drive progression or maintain health. PROVISIONING is excluded (no
# logged-in terminal to observe yet); SUSPENDED/RETIRED are terminal (an observation cannot progress them).
# Short-circuiting here means the host observer is NEVER triggered for a workspace that cannot use the result —
# PHASE 7 "avoid wasteful polling", enforced at the transport (before any host contact), not in the driver.
_OBSERVABLE_STATES = frozenset({
    S.WAITING_FOR_LOGIN.value, S.CONNECTED.value, S.EXECUTION_READY.value,
    S.EXECUTING.value, S.DISCONNECTED.value, S.RECOVERING.value,
})

# The observable fields the host result may fill. expected_login/expected_server are NOT here — they come from
# the workspace, never the wire.
_TRUE = (True, False)


def _bool_or_none(v):
    return v if isinstance(v, bool) else None


def _str_or_none(v):
    if v is None:
        return None
    try:
        return str(v)
    except Exception:  # noqa: BLE001
        return None


def _int_or_none(v):
    return v if (isinstance(v, int) and not isinstance(v, bool)) else None


def _num_or_none(v):
    return v if (isinstance(v, (int, float)) and not isinstance(v, bool)) else None


def _account_of(workspace):
    return getattr(workspace, "trading_account", None)


def build_observation_from_host(workspace, result):
    """Map the host's sanitised ``OBSERVE_WORKSPACE`` result (a ``RawWorkspaceSnapshot`` as a dict) into a
    certified ``WorkspaceObservation``. Fail-closed: a non-dict / ``ok:false`` / no-account result ⇒ ``None``.
    ``expected_*`` are the WORKSPACE's server-owned identity (never the host's), so the certified producer does
    the observed-vs-expected comparison — the host can never assert a match it isn't entitled to."""
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    acct = _account_of(workspace)
    if acct is None:
        return None
    server = getattr(acct, "broker_server", None)
    snapshot = RawWorkspaceSnapshot(
        workspace_id=str(getattr(workspace, "workspace_uuid", "") or getattr(workspace, "id", "") or ""),
        expected_login=_str_or_none(getattr(acct, "account_number", None)),
        expected_server=_str_or_none(getattr(server, "server_name", None)),
        target_pid=None,
        target_path=None,
        process_running=_bool_or_none(result.get("process_running")),
        attach_attempted=_bool_or_none(result.get("attach_attempted")),
        attach_succeeded=_bool_or_none(result.get("attach_succeeded")),
        ipc_available=_bool_or_none(result.get("ipc_available")),
        terminal_connected=_bool_or_none(result.get("terminal_connected")),
        trade_allowed=_bool_or_none(result.get("trade_allowed")),
        observed_login=_str_or_none(result.get("observed_login")),
        observed_server=_str_or_none(result.get("observed_server")),
        observed_trade_mode=_int_or_none(result.get("observed_trade_mode")),
        observed_at=_num_or_none(result.get("observed_at")),
        freshness_limit_seconds=None,
        attach_reason=_str_or_none(result.get("attach_reason")) or "",
        process_reason=_str_or_none(result.get("process_reason")) or "",
        connection_reason=_str_or_none(result.get("connection_reason")) or "",
    )
    now = snapshot.observed_at if isinstance(snapshot.observed_at, (int, float)) else 0.0
    prev = str(getattr(workspace, "canonical_state", "") or "")
    return build_workspace_observation(
        snapshot, now=now, previous_state=prev, previous_reason=WorkspaceReason.NONE,
        clock_tolerance_seconds=DEFAULT_CLOCK_TOLERANCE_SECONDS)


def _node_rdp_host(workspace) -> str:
    node = getattr(workspace, "execution_node", None)
    return _str_or_none(getattr(node, "rdp_host", None)) or ""


def live_observe_fn(workspace):
    """The production ``observe_fn``: ``workspace -> WorkspaceObservation | None``. Fail-closed. Gated on
    ``HOSTED_MT5_OBSERVATION_ENABLED``; the signed executor is separately gated (host-executor flag + keyring/
    base_url), so with the observation flag on but the executor unconfigured this still contacts no host."""
    if not hosted_mt5_observation_enabled():
        return None
    if str(getattr(workspace, "canonical_state", "") or "") not in _OBSERVABLE_STATES:
        return None
    acct = _account_of(workspace)
    acct_id = getattr(acct, "id", None)
    if acct is None or acct_id is None:
        return None
    try:
        from hosted_workspace.host_executor import resolve_signed_host_executor
        executor = resolve_signed_host_executor(account_id=int(acct_id), rdp_host=_node_rdp_host(workspace))
    except Exception:  # noqa: BLE001 — any resolver failure is ambiguous → fail closed
        return None
    if executor is None:
        return None
    try:
        result = executor.observe()
    except Exception:  # noqa: BLE001 — transport already fails closed, but never raise into the driver
        logger.warning("live_observe: transport error account=%s", acct_id)
        return None
    return build_observation_from_host(workspace, result)
