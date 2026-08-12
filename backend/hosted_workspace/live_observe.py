"""hosted_workspace.live_observe - STREAM 9E: the REAL production ``observe_fn`` (replaces ``_dark_observe_fn``).

Turns an eligible Hosted Workspace into a live ``WorkspaceObservation`` by asking the host - over the SIGNED
executor's read-only ``OBSERVE_WORKSPACE`` op - to run that account's session-bound observer and return BOTH a
tenant-attested ``RawWorkspaceSnapshot`` AND an independent LocalSystem corroboration block, then feeding the
combined, agreed evidence through the EXISTING certified producer (``build_workspace_observation``). It adds NO
state and NO second state machine: it is a transport + a fail-closed mapping + a two-source agreement gate only.
The Workspace Manager (M3a) + single writer (M3c consumer) remain the sole deciders/persisters.

Two independent evidence sources, combined before any advancement (STREAM 9E hardening - Modified Option 2):
  * **tenant snapshot** - produced by ``run_observer.py`` running AS ``guvfx_u_<id>`` inside the tenant session
    (the ONLY context that can reach the session-bound MT5 IPC). It carries the attach/IPC/broker facts. It is
    tenant-attested and therefore, on its own, forgeable by a tenant who can execute code in-session.
  * **LocalSystem corroboration** - produced by ``Invoke-GuvfxObserver.ps1`` running as the LocalSystem daemon
    (a party the tenant cannot impersonate) and returned inside the SAME daemon-SIGNED response. It carries the
    OBJECTIVE host facts the tenant cannot forge: the terminal process exists, is the expected executable at the
    expected path, is owned by the expected Windows user, in an interactive session (>0), under the expected
    runtime root, holds a live external network connection, and a LocalSystem timestamp. These are gathered by
    LocalSystem's own CIM/network queries - NEVER copied from the tenant file.

Advancement (a non-``None`` observation) requires ALL of:
  1. the tenant snapshot is internally valid (``ok`` true, well-formed);
  2. the LocalSystem corroboration is present and matches the SERVER-DERIVED account / username / session /
     runtime for this workspace (never a value from the wire);
  3. the two sources AGREE - in particular a tenant ``terminal_connected`` claim is honoured ONLY when
     LocalSystem independently observed a live external network connection for that exact terminal process.
Any failure - flag off, ineligible state, no account, executor unresolved, transport error, host ``ok:false``,
malformed/missing/mismatched corroboration, or a tenant-vs-LocalSystem disagreement - returns ``None`` (the
runner ingests nothing -> the workspace's freshness lapses -> readiness holds). Observation is CAPABILITY only:
nothing here arms execution, logs in, provisions, confirms an account, or mutates the host.

Freshness is anchored on the LocalSystem-attested ``collected_at`` (a clock the tenant does not control) versus
the backend's trusted wall clock, with a positive limit - so a stale/replayed observation is rejected and a
genuinely fresh, corroborated, connected + matched + trade-allowed workspace can reach EXECUTION_READY.

The expected identity (login/server) is read from the WORKSPACE's own ``trading_account`` (server-owned) and
fed to the certified producer - NEVER taken from the host result - so a compromised/rogue host reply can never
assert its own identity and force ``account_match`` (the producer compares observed-vs-expected).

TRUST MODEL (ADR-0041). LocalSystem corroboration proves the OBJECTIVE host facts a tenant cannot forge
(process/owner/session/runtime + a live external connection), but it CANNOT independently prove the MT5 IPC
facts (login/server, attach/IPC, trade_allowed) - those exist only inside the tenant session, which is why the
observer runs there. The tenant-written handoff is therefore forgeable IFF the tenant can execute arbitrary
code in its own session. Accordingly, a Hosted Workspace observation is DEFINED as a BOUNDED workspace-readiness
signal that is trusted ONLY after RemoteApp isolation has been behaviourally certified
(``hosted_remoteapp_isolation_certified``, enforced in ``live_observe_fn``). It is NOT an execution-authority
signal: execution has its own independent runtime-identity validation, so even if RemoteApp isolation were
broken, observation integrity would break but EXECUTION integrity would not.
"""
from __future__ import annotations

import logging
import math

from hosted_workspace.flags import (
    hosted_mt5_observation_enabled,
    hosted_remoteapp_isolation_certified,
)
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
# Short-circuiting here means the host observer is NEVER triggered for a workspace that cannot use the result -
# PHASE 7 "avoid wasteful polling", enforced at the transport (before any host contact), not in the driver.
_OBSERVABLE_STATES = frozenset({
    S.WAITING_FOR_LOGIN.value, S.CONNECTED.value, S.EXECUTION_READY.value,
    S.EXECUTING.value, S.DISCONNECTED.value, S.RECOVERING.value,
})

# The freshness window for a live observation. Anchored on the LocalSystem-attested ``collected_at`` (not the
# tenant-controlled observed_at) vs the backend wall clock, so it is a real staleness/anti-replay guard. Matches
# the certified agent/certification path (60.0s).
_OBSERVATION_FRESHNESS_LIMIT_SECONDS = 60.0


def _bool_or_none(v):
    return v if isinstance(v, bool) else None


def _is_true(v):
    return v is True


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
    return v if (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)) else None


def _norm_path(v):
    """Normalise a Windows path for comparison: str, backslashes, trailing slash stripped, lowercased. None-safe."""
    s = _str_or_none(v)
    if s is None:
        return None
    return s.replace("/", "\\").rstrip("\\").lower()


def _account_of(workspace):
    return getattr(workspace, "trading_account", None)


def _server_derived_identity(acct_id):
    """The server-derived (username, runtime_root) for this account - the SINGLE SOURCE OF TRUTH the corroboration
    must match. Reuses the host dispatcher's derivation so the two can never drift. None on any derivation error."""
    try:
        from hosted_workspace.host_agent_dispatch import derive_slot
        slot = derive_slot(int(acct_id))
        return slot["username"], _norm_path(slot["runtime_root"])
    except Exception:  # noqa: BLE001
        return None, None


def corroboration_matches(corr, acct_id):
    """True ONLY when the LocalSystem corroboration block is present and matches the SERVER-DERIVED identity for
    ``acct_id``: same account, the terminal process present, owned by the expected ``guvfx_u_<id>`` in an
    interactive session (>0), under the expected runtime root, and carrying a usable ``collected_at`` timestamp.
    Fail-closed: a missing/malformed/mismatched block is False. The network fact is NOT asserted here - it is
    checked as the tenant<->LocalSystem AGREEMENT for a ``connected`` claim (see ``build_observation_from_host``)."""
    if not isinstance(corr, dict):
        return False
    expected_user, expected_runtime = _server_derived_identity(acct_id)
    if expected_user is None:
        return False
    if _int_or_none(corr.get("account_id")) != int(acct_id):
        return False
    if not _is_true(corr.get("process_present")):
        return False
    if _str_or_none(corr.get("owner_user")) != expected_user:
        return False
    session_id = _int_or_none(corr.get("session_id"))
    if session_id is None or session_id <= 0:
        return False
    if _norm_path(corr.get("runtime_root")) != expected_runtime:
        return False
    if _num_or_none(corr.get("collected_at")) is None:   # freshness anchor must be a real number
        return False
    return True


def build_observation_from_host(workspace, result, *, now=None):
    """Map the host's sanitised ``OBSERVE_WORKSPACE`` result (tenant snapshot + LocalSystem ``corroboration``)
    into a certified ``WorkspaceObservation``. Fail-closed at every branch; returns ``None`` unless the tenant
    snapshot is valid, the corroboration matches the server-derived identity, and the two sources AGREE.

    ``expected_*`` are the WORKSPACE's server-owned identity (never the host's), so the certified producer does
    the observed-vs-expected comparison. ``now`` is injectable for tests; production uses the backend wall clock."""
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    acct = _account_of(workspace)
    if acct is None:
        return None
    acct_id = getattr(acct, "id", None)
    if acct_id is None:
        return None

    # (2) LocalSystem corroboration must be present and match the SERVER-DERIVED identity/session/runtime.
    corr = result.get("corroboration")
    if not corroboration_matches(corr, acct_id):
        return None

    # (3) Agreement: a tenant-attested ``terminal_connected`` is trusted ONLY if LocalSystem independently
    # observed a live external network connection for that exact terminal process. A tenant that forges
    # ``connected`` without a real connection is refused here (fail closed -> no advancement).
    tenant_connected = _is_true(result.get("terminal_connected"))
    if tenant_connected and not _is_true(corr.get("network_active")):
        return None

    server = getattr(acct, "broker_server", None)
    # Freshness is anchored on the LocalSystem-attested collection time (a clock the tenant does not control),
    # NOT the tenant-supplied observed_at, so staleness/replay is a real guard and cannot be defeated by a
    # forged tenant timestamp. ``corroboration_matches`` already proved collected_at is a usable number.
    collected_at = _num_or_none(corr.get("collected_at"))
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
        observed_at=collected_at,
        freshness_limit_seconds=_OBSERVATION_FRESHNESS_LIMIT_SECONDS,
        attach_reason=_str_or_none(result.get("attach_reason")) or "",
        process_reason=_str_or_none(result.get("process_reason")) or "",
        connection_reason=_str_or_none(result.get("connection_reason")) or "",
    )
    if now is None:
        from django.utils import timezone
        now = timezone.now().timestamp()
    prev = str(getattr(workspace, "canonical_state", "") or "")
    return build_workspace_observation(
        snapshot, now=now, previous_state=prev, previous_reason=WorkspaceReason.NONE,
        clock_tolerance_seconds=DEFAULT_CLOCK_TOLERANCE_SECONDS)


def _node_rdp_host(workspace) -> str:
    node = getattr(workspace, "execution_node", None)
    return _str_or_none(getattr(node, "rdp_host", None)) or ""


def live_observe_fn(workspace):
    """The production ``observe_fn``: ``workspace -> WorkspaceObservation | None``. Fail-closed.

    TRUST-MODEL PREREQUISITE (ADR-0041): a live observation is produced ONLY when RemoteApp isolation has been
    behaviourally certified (``hosted_remoteapp_isolation_certified``). The observer runs inside the tenant
    session, so its MT5 IPC facts (login/ipc/trade_allowed) are trustworthy ONLY if the tenant cannot execute
    arbitrary code there. Without that certification this returns ``None`` (no observation ingested, no
    advancement) - the observation channel stays DARK. This is the code enforcement of the dependency
    REMOTEAPP_ISOLATION_CERTIFIED -> HOSTED_OBSERVATION -> WORKSPACE_READY. Execution is unaffected either way:
    its runtime-identity validation is independent of observation.

    Also gated on ``HOSTED_MT5_OBSERVATION_ENABLED``; the signed executor is separately gated (host-executor
    flag + keyring/base_url), so with the flags on but the executor unconfigured this still contacts no host."""
    if not hosted_remoteapp_isolation_certified():
        return None   # trust anchor absent -> observation is not trustworthy -> produce nothing (fail closed)
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
    except Exception:  # noqa: BLE001 - any resolver failure is ambiguous -> fail closed
        return None
    if executor is None:
        return None
    try:
        result = executor.observe()
    except Exception:  # noqa: BLE001 - transport already fails closed, but never raise into the driver
        logger.warning("live_observe: transport error account=%s", acct_id)
        return None
    return build_observation_from_host(workspace, result)
