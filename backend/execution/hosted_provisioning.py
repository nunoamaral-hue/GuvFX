"""ADR-0034 Execution Engine (G5) — Hosted Workspace PROVISION vs ARM (DARK, demo-only).

PROVISIONED ≠ CONNECTED ≠ EXECUTION_READY ≠ EXECUTION_ARMED. Provisioning a workspace must NEVER make it
execution-capable. Arming is a SEPARATE, EXPLICIT, audited action whose sole purpose is "arm execution", and
it defaults off: ``HostedMt5Workspace.execution_enabled`` defaults False and no migration or lifecycle event
(healthy broker, strategy assignment, onboarding, observation) ever flips it. Only ``arm_hosted_workspace_
execution`` does — and only when EVERY precondition holds.

Nothing here executes, attaches, logs in, launches MT5, switches accounts, or writes canonical lifecycle
state (that stays the M3c writer's). Arming sets one durable boolean; the live order-time bridge gate remains
the sole order authority.
"""
from __future__ import annotations

from dataclasses import dataclass

from execution import readiness as R


@dataclass(frozen=True)
class ArmResult:
    ok: bool
    reason_code: str

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason_code": self.reason_code}


# Arm-time preconditions reason codes reuse the readiness vocabulary; plus a routing one.
ARM_OK = "armed"
DISARM_OK = "disarmed"
ARM_NO_WORKSPACE = R.RW_WORKSPACE_MISSING
ARM_ROUTE_MISSING = "workspace_route_missing"


def _hosted_flags_on() -> bool:
    return R._hosted_persistent_mt5_enabled() and R._hosted_mt5_execution_enabled()


def _arm_preconditions(account) -> ArmResult:
    """Everything that must hold to ARM (ADR-0034 G5), EXCEPT ``execution_enabled`` itself (which arming is
    about to set). Fail-closed, most-specific-first, reusing the readiness reason codes so the arm speaks the
    same vocabulary as dispatch. The LIVE conditions are re-proven by the bridge before every order."""
    if account is None or getattr(account, "pk", None) is None:
        return ArmResult(False, "broker_account_missing")
    if not _hosted_flags_on():                                   # global + execution flags
        return ArmResult(False, R.RW_EXECUTION_FEATURE_DISABLED)
    if str(getattr(account, "readiness_provider", "") or "") != R.PERSISTENT_WORKSPACE:
        return ArmResult(False, R.RW_SUBSYSTEM_DISABLED)
    if not getattr(account, "is_active", False):
        return ArmResult(False, "broker_account_inactive")
    if getattr(account, "disconnected_at", None) is not None:
        return ArmResult(False, "broker_account_disconnected")
    if getattr(account, "is_demo", False) is not True:          # demo-only wall
        return ArmResult(False, R.RW_REAL_ACCOUNT_NOT_ENABLED)
    ws = getattr(account, "hosted_workspace", None)
    if ws is None:
        return ArmResult(False, ARM_NO_WORKSPACE)
    if getattr(ws, "trading_account_id", None) != account.pk:   # owner-bound
        return ArmResult(False, "workspace_owner_mismatch")
    if getattr(account, "terminal_node_id", None) is None:      # non-NULL route (Decision C)
        return ArmResult(False, ARM_ROUTE_MISSING)
    if ws.proj_connected is not True:
        return ArmResult(False, R.RW_WORKSPACE_NOT_CONNECTED)
    if ws.proj_account_match is not True:
        return ArmResult(False, R.RW_ACTIVE_ACCOUNT_MISMATCH)
    if ws.proj_trade_allowed is not True or not ws.canonical_execution_ready:
        return ArmResult(False, R.RW_WORKSPACE_NOT_READY)
    if not R._observation_fresh(ws):                            # fresh observation
        return ArmResult(False, R.RW_OBSERVATION_STALE)
    return ArmResult(True, ARM_OK)


def provision_hosted_workspace(account, *, attach_path: str = "", actor: str = "", request=None):
    """Provision a Hosted Workspace for an account (set the Provider-B path + ensure the workspace row).
    This is the FIRST, separate step — it NEVER arms execution: ``execution_enabled`` stays False. Returns
    the ``HostedMt5Workspace``. Audited. No canonical-state write (that stays the M3c writer's), no attach,
    no login."""
    from hosted_workspace.models import HostedMt5Workspace
    ws, _created = HostedMt5Workspace.objects.get_or_create(trading_account=account)
    fields = []
    if attach_path and ws.attach_path != attach_path:
        ws.attach_path = attach_path
        fields.append("attach_path")
    # A provisioning step must NEVER arm — belt-and-braces: if a row somehow arrived armed, provisioning
    # does not rely on that; arming is only ever the explicit arm action.
    if fields:
        ws.save(update_fields=fields + ["updated_at"])
    if str(getattr(account, "readiness_provider", "") or "") != R.PERSISTENT_WORKSPACE:
        account.readiness_provider = R.PERSISTENT_WORKSPACE
        account.save(update_fields=["readiness_provider"])
    _audit(request, "HOSTED_WORKSPACE_PROVISIONED", account, actor)
    return ws


def arm_hosted_workspace_execution(account, *, actor: str = "", request=None) -> ArmResult:
    """The ONE explicit arm action. Verifies ALL preconditions, then sets ``execution_enabled = True`` and
    audits it. Fail-closed: a single failing precondition refuses without mutating. Idempotent."""
    pre = _arm_preconditions(account)
    if not pre.ok:
        return pre
    ws = account.hosted_workspace
    if ws.execution_enabled is not True:
        ws.execution_enabled = True
        ws.save(update_fields=["execution_enabled", "updated_at"])
    _audit(request, "HOSTED_EXECUTION_ARMED", account, actor)
    return ArmResult(True, ARM_OK)


def disarm_hosted_workspace_execution(account, *, actor: str = "", request=None) -> ArmResult:
    """Immediate, safe disarm — set ``execution_enabled = False``. No preconditions (disarming is always
    allowed and fail-safe). Idempotent; audited. After this, dispatch/claim refuse the account at once."""
    ws = getattr(account, "hosted_workspace", None)
    if ws is None:
        return ArmResult(False, ARM_NO_WORKSPACE)
    if ws.execution_enabled is not False:
        ws.execution_enabled = False
        ws.save(update_fields=["execution_enabled", "updated_at"])
    _audit(request, "HOSTED_EXECUTION_DISARMED", account, actor)
    return ArmResult(True, DISARM_OK)


def _audit(request, event_type, account, actor) -> None:
    """Best-effort, non-secret audit of an arm/disarm (fail-open — never blocks the state change)."""
    try:
        from core.audit import log_event
        log_event(request, event_type, severity="WARN", entity_type="TradingAccount",
                  entity_id=getattr(account, "pk", None), metadata={"actor": str(actor or "")})
    except Exception:  # noqa: BLE001
        pass
