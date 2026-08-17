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
ARM_NODE_UNBOUND = "workspace_execution_node_unbound"      # capstone: no durable workspace->node binding
ARM_NODE_MISMATCH = "workspace_execution_node_mismatch"    # capstone: binding disagrees with account node
ARM_NOT_AUTHORIZED = "workspace_execution_not_authorized"  # ADR-0047: no explicit customer authorization


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
    # ADR-0044 supervised posture: while uncertified + the supervised flag is on, arming is permitted ONLY while
    # the bounded single-tenant carve-out holds for this workspace (no-op when certified or when the flag is
    # off). Fail-closed. Same predicate the order-time readiness gate applies, so arm and dispatch agree.
    if not R._execution_permitted_under_posture(ws):
        return ArmResult(False, R.RW_SUPERVISED_BOUNDARY)
    if getattr(account, "terminal_node_id", None) is None:      # non-NULL route (Decision C)
        return ArmResult(False, ARM_ROUTE_MISSING)
    # Capstone (PART 2): the durable workspace->node binding must exist AND agree with the account's node —
    # so the workspace, the account, and the (future) job all resolve to exactly ONE authorised node.
    if getattr(ws, "execution_node_id", None) is None:
        return ArmResult(False, ARM_NODE_UNBOUND)
    if ws.execution_node_id != account.terminal_node_id:
        return ArmResult(False, ARM_NODE_MISMATCH)
    if ws.proj_connected is not True:
        return ArmResult(False, R.RW_WORKSPACE_NOT_CONNECTED)
    if ws.proj_account_match is not True:
        return ArmResult(False, R.RW_ACTIVE_ACCOUNT_MISMATCH)
    if ws.proj_trade_allowed is not True or not ws.canonical_execution_ready:
        return ArmResult(False, R.RW_WORKSPACE_NOT_READY)
    if not R._observation_fresh(ws):                            # fresh observation
        return ArmResult(False, R.RW_OBSERVATION_STALE)
    # ADR-0047 (checked LAST so the more-specific route/posture/connection reasons stay reachable): MT5
    # automation CAPABILITY (trade_allowed / EXECUTION_READY) is NOT customer AUTHORIZATION. Arming — by the
    # autonomous auto_arm_runner OR the operator command, this being their shared chokepoint — is refused
    # unless the customer has EXPLICITLY authorized execution for THIS workspace (execution_authorized_at,
    # written only by the owner-scoped authorize_workspace_execution). NULL ⇒ never armed merely because the
    # workspace became otherwise-ready.
    if getattr(ws, "execution_authorized_at", None) is None:
        return ArmResult(False, ARM_NOT_AUTHORIZED)
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
    # An EXPLICIT arm clears any operator-disarm suppression (ADR-0044) — so a later auto-arm may resume it —
    # and sets execution_enabled. Idempotent.
    fields = []
    if ws.execution_enabled is not True:
        ws.execution_enabled = True
        fields.append("execution_enabled")
    if getattr(ws, "auto_arm_suppressed", False) is not False:
        ws.auto_arm_suppressed = False
        fields.append("auto_arm_suppressed")
    if fields:
        ws.save(update_fields=fields + ["updated_at"])
    _audit(request, "HOSTED_EXECUTION_ARMED", account, actor)
    return ArmResult(True, ARM_OK)


def disarm_hosted_workspace_execution(account, *, actor: str = "", request=None) -> ArmResult:
    """Immediate, safe disarm — set ``execution_enabled = False``. No preconditions (disarming is always
    allowed and fail-safe). Idempotent; audited. After this, dispatch/claim refuse the account at once."""
    ws = getattr(account, "hosted_workspace", None)
    if ws is None:
        return ArmResult(False, ARM_NO_WORKSPACE)
    # Disarm is fail-safe AND durable against autonomous re-arm (ADR-0044): set the operator-intent suppression
    # so ``auto_arm_runner`` never silently re-arms this workspace on the next cron cycle. Only an explicit arm
    # clears it. Idempotent.
    fields = []
    if ws.execution_enabled is not False:
        ws.execution_enabled = False
        fields.append("execution_enabled")
    if getattr(ws, "auto_arm_suppressed", False) is not True:
        ws.auto_arm_suppressed = True
        fields.append("auto_arm_suppressed")
    if fields:
        ws.save(update_fields=fields + ["updated_at"])
    _audit(request, "HOSTED_EXECUTION_DISARMED", account, actor)
    return ArmResult(True, DISARM_OK)


def assign_workspace_execution_node(account_or_ws, node, *, actor: str = "", request=None):
    """PART 2/3 provisioning contract — durably bind a Hosted Workspace to its ONE authorised execution
    TerminalNode. Server-side only; VERSIONED (``execution_binding_generation`` increments by one on each
    (re)assignment). It NEVER arms execution. Reversible while DARK: reassign to a different node here, or
    ``clear_workspace_execution_node`` to unbind; arming still additionally requires the binding to AGREE with
    ``account.terminal_node`` (``_arm_preconditions``). Fail-closed on a falsy node. Audited. No credential."""
    from django.db import transaction

    from hosted_workspace.models import HostedMt5Workspace
    ws = (account_or_ws if isinstance(account_or_ws, HostedMt5Workspace)
          else getattr(account_or_ws, "hosted_workspace", None))
    if ws is None or getattr(ws, "pk", None) is None or node is None or getattr(node, "pk", None) is None:
        return None
    # ADR-0043 Addendum B: fail-closed CO-RESIDENCY guard at the execution-node single writer. It protects the
    # ``workspace.execution_node`` binding and raises BEFORE any generation bump / write, so a rejected binding
    # leaves ``execution_node`` unchanged. The OTHER binding surface — ``account.terminal_node`` — is kept safe
    # by the CALLERS, not here: the allocator pre-filters forbidden nodes out of candidate selection, and the
    # ``provision_hosted_execution`` command pre-checks with ``assert_allocation_allowed`` before its own
    # ``account.terminal_node`` write. No-op while HOSTED_TENANT_NODE_ISOLATION_ENABLED is OFF (returns early).
    from hosted_workspace.tenant_isolation import assert_allocation_allowed
    assert_allocation_allowed(getattr(ws, "trading_account_id", None), node)
    # Row-lock the workspace and compute the version from the LOCKED value, so two concurrent (re)assignments
    # serialise and the generation stays strictly monotonic (+1 each) — no Python-level lost update.
    with transaction.atomic():
        locked = HostedMt5Workspace.objects.select_for_update().get(pk=ws.pk)
        if locked.execution_node_id == node.pk:
            return locked  # idempotent — no redundant generation bump
        locked.execution_node = node
        locked.execution_binding_generation = int(locked.execution_binding_generation or 0) + 1
        locked.save(update_fields=["execution_node", "execution_binding_generation", "updated_at"])
    _audit(request, "HOSTED_EXECUTION_NODE_ASSIGNED", locked.trading_account, actor)
    return locked


def clear_workspace_execution_node(account_or_ws, *, actor: str = "", request=None):
    """Reverse the binding while DARK — unbind the workspace from its execution node (``execution_node`` →
    NULL, generation still increments so the change is versioned/auditable). After this the workspace is NOT
    execution-routable (fail-closed). Idempotent; audited."""
    from django.db import transaction

    from hosted_workspace.models import HostedMt5Workspace
    ws = (account_or_ws if isinstance(account_or_ws, HostedMt5Workspace)
          else getattr(account_or_ws, "hosted_workspace", None))
    if ws is None or getattr(ws, "pk", None) is None:
        return ws
    with transaction.atomic():
        locked = HostedMt5Workspace.objects.select_for_update().get(pk=ws.pk)
        if locked.execution_node_id is None:
            return locked  # already unbound — no redundant generation bump
        locked.execution_node = None
        locked.execution_binding_generation = int(locked.execution_binding_generation or 0) + 1
        locked.save(update_fields=["execution_node", "execution_binding_generation", "updated_at"])
    _audit(request, "HOSTED_EXECUTION_NODE_CLEARED", locked.trading_account, actor)
    return locked


def _audit(request, event_type, account, actor) -> None:
    """Best-effort, non-secret audit of an arm/disarm (fail-open — never blocks the state change)."""
    try:
        from core.audit import log_event
        log_event(request, event_type, severity="WARN", entity_type="TradingAccount",
                  entity_id=getattr(account, "pk", None), metadata={"actor": str(actor or "")})
    except Exception:  # noqa: BLE001
        pass
