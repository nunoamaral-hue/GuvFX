"""hosted_workspace.auto_arm_runner — hosted-execution arming completion driver (DARK, demo-only).

ADR-0047 (Sponsor 2026-08-17) SUPERSEDES ADR-0044 Decision 2: MT5 automation CAPABILITY (trade_allowed /
EXECUTION_READY) is NOT customer AUTHORIZATION to trade. Reaching EXECUTION_READY must NEVER autonomously arm
a workspace. Arming now requires an EXPLICIT, durable customer authorization (``execution_authorized_at``, set
only by the customer's owner-scoped "Enable automated trading" action). This runner is therefore no longer an
autonomous *arming* path — it can only COMPLETE an arm the customer has ALREADY authorized (e.g. re-apply it
after a transient EXECUTION_READY flap), enforced both by its candidate filter below AND fail-closed inside
``arm_hosted_workspace_execution``'s preconditions.

Historical context (ADR-0044 Decision 2, 2026-08-14, now superseded): the intent was to remove the per-customer
operator CLI step so onboarding reached an executable demo account autonomously. That autonomy is withdrawn for
the arm: the customer's explicit click is the sole authorization. This idempotent, retry-safe driver still
calls the SAME certified ``arm_hosted_workspace_execution`` the operator command calls.

It is NOT a new arming path and it does NOT relax a single precondition: ``arm_hosted_workspace_execution``
re-proves EVERY arm precondition (hosted flags on, Provider B, active, demo, workspace owner-bound, non-NULL
route, workspace↔node binding agrees with the account node, connected + matched + trade_allowed + canonical
EXECUTION_READY + fresh observation) and only then flips the one durable boolean. So this driver can only ever
arm a workspace that the operator command would also have armed. It performs NO host or broker action, places
no order, and the live order-time bridge gate remains the sole order authority.

Two-level darkness: a no-op returning an empty summary unless BOTH ``hosted_persistent_mt5_enabled()`` (master)
AND ``hosted_mt5_execution_enabled()`` (execution subsystem) are on — the same conjunction ``_arm_preconditions``
enforces, checked here first so the driver does no per-workspace work while execution is dark. Reversible: an
operator ``disarm``/``clear_workspace_execution_node`` still wins; nothing here re-arms a deliberately disarmed
workspace that is no longer EXECUTION_READY.
"""
from __future__ import annotations

import logging

from hosted_workspace.flags import hosted_mt5_execution_enabled, hosted_persistent_mt5_enabled
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

logger = logging.getLogger("guvfx.hosted_workspace")

SOURCE = "hosted_workspace.auto_arm_runner"


def run_hosted_auto_arm(*, actor: str = SOURCE) -> dict:
    """Arm ``execution_enabled`` for every workspace that has reached canonical EXECUTION_READY but is not yet
    armed, by calling the certified arm action (which re-proves all preconditions). Idempotent + fail-open per
    workspace. Returns a secret-free summary. DARK unless the master AND execution flags are both on."""
    if not (hosted_persistent_mt5_enabled() and hosted_mt5_execution_enabled()):
        return {"enabled": False, "candidates": 0, "armed": 0, "already": 0, "refused": 0, "errors": 0}

    from execution.hosted_provisioning import ARM_OK, arm_hosted_workspace_execution

    candidates = armed = already = refused = errors = 0
    # Candidates = workspaces canonically EXECUTION_READY that are not yet armed, not operator-suppressed, AND
    # have an EXPLICIT customer authorization (ADR-0047, execution_authorized_at NOT NULL). A workspace that
    # later leaves EXECUTION_READY is simply not a candidate; a DELIBERATELY disarmed one (auto_arm_suppressed
    # =True, ADR-0044) is excluded so an operator disarm is never silently reverted; and an UNAUTHORIZED one is
    # excluded so reaching EXECUTION_READY can NEVER autonomously arm without the customer's explicit consent —
    # this runner can now only COMPLETE an already-authorized arm, never manufacture one. (The same authz gate
    # is also enforced fail-closed inside arm_hosted_workspace_execution's preconditions, so this filter is
    # defense-in-depth, not the sole guard.)
    qs = (HostedMt5Workspace.objects
          .filter(canonical_state=str(S.EXECUTION_READY), execution_enabled=False, auto_arm_suppressed=False,
                  execution_authorized_at__isnull=False)
          .select_related("trading_account")
          .iterator())
    for ws in qs:
        candidates += 1
        account = getattr(ws, "trading_account", None)
        if account is None:
            errors += 1
            continue
        try:
            res = arm_hosted_workspace_execution(account, actor=actor)
        except Exception:  # noqa: BLE001 — one workspace's failure must not stop the pass
            errors += 1
            logger.exception("auto-arm failed for workspace=%s", getattr(ws, "pk", None))
            continue
        if res.ok and res.reason_code == ARM_OK:
            # ``arm_hosted_workspace_execution`` is idempotent; since we filtered execution_enabled=False, an OK
            # here is a genuine transition to armed.
            armed += 1
        else:
            refused += 1
    return {"enabled": True, "candidates": candidates, "armed": armed, "already": already,
            "refused": refused, "errors": errors}
