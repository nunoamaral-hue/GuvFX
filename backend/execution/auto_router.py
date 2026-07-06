"""
AUTO-SHADOW FOUNDATION (GFX-PKT-AUTO-SHADOW-FOUNDATION) — the auto-execution router.

Routes a freshly-INTAKEN signal to the AUTO-SHADOW path ONLY when a full AND of config
gates is satisfied. DISABLED BY DEFAULT: every gate defaults to its safest value, so with
the default config ``effective_mode`` returns MANUAL and the receiver is a pure no-op —
every signal lands in the existing manual ``PendingSignalApproval`` gate exactly as before.

HARD BOUNDARY — the auto path output is ``PLACE_ORDER_SHADOW`` jobs ONLY. It calls the
existing, separately-gated ``approve() -> plan_demo_execution() -> promote_plan_to_shadow_jobs()``
functions (each re-validates its own gates), so it cannot skip a check even if it wanted to.
It NEVER creates a ``PLACE_ORDER`` / ``OPEN_TRADE`` / ``PLACE_TEST_ORDER`` job, never calls
``order_send`` / ``order_check``, never contacts a broker or bridge. EDITED signals NEVER
auto-execute (decision §13.5). Any error fails closed to MANUAL (the approval is left as-is).

Auto-demo / auto-live are NOT implemented here — the global ``signal_execution_mode`` is
SHADOW-only and an assignment set to ``AUTO_DEMO``/``AUTO_LIVE`` is treated as MANUAL until
its own future, separately-gated packet.
"""
from __future__ import annotations

import logging

from django.conf import settings

from signal_intake import services as intake_services
from signal_intake.models import AcquiredMessage
from strategies.models import StrategyAssignment

from execution.models import (
    ExecutionControl,
    SignalExecutionPlan,
    SignalSourceConfig,
    order_creation_kill_reason,
)
from execution.signal_planning import PlanRejected, plan_demo_execution
from execution.signal_promotion import PromotionRejected, promote_plan_to_shadow_jobs

logger = logging.getLogger("guvfx.execution.auto_router")

MODE_MANUAL = "MANUAL"
MODE_AUTO_SHADOW = "AUTO_SHADOW"

# Decision §13.4 — minimum parser certification to permit auto is MEDIUM.
_CONFIDENCE_OK = {"MEDIUM", "HIGH"}


def _confidence_ok(provider) -> bool:
    """Parse-confidence read path. Fail-closed: unknown/LOW → False."""
    try:
        return getattr(provider.parser_profile, "certification_level", "LOW") in _CONFIDENCE_OK
    except Exception:
        return False


def resolve_auto_shadow_target():
    """The UNIQUE active AUTO_SHADOW assignment (stage LIVE, demo account), or None.

    Fail-closed on 0 or >1 matches: an ambiguous config never auto-executes — the
    operator must make the target unambiguous.
    """
    hits = list(
        StrategyAssignment.objects.filter(
            execution_mode=StrategyAssignment.ExecutionMode.AUTO_SHADOW,
            is_active=True,
            stage=StrategyAssignment.STAGE_LIVE,
            account__is_demo=True,
        ).select_related("account")[:2]
    )
    return hits[0] if len(hits) == 1 else None


def _system_actor():
    """The designated auto-execution system reviewer. None → fail-closed.

    Resolved by username (``AUTO_EXECUTION_SYSTEM_USERNAME``, default
    ``guvfx-auto-system``). The actor must hold ``signal_intake.review_signals`` — if it
    does not, ``approve()`` raises and the caller fails closed to MANUAL.
    """
    from django.contrib.auth import get_user_model

    username = getattr(settings, "AUTO_EXECUTION_SYSTEM_USERNAME", "guvfx-auto-system")
    return get_user_model().objects.filter(username=username, is_active=True).first()


def effective_mode(approval):
    """Return ``(mode, reason)`` — the mode SSOT, an AND of independent config gates.

    Returns ``(MODE_MANUAL, reason)`` unless EVERY gate is explicitly satisfied. No
    single flag can enable auto; any unset/unknown value yields MANUAL.
    """
    # Edited signals NEVER auto-execute (§13.5) — checked first. Defense-in-depth:
    # re-read the edited flag from the DB so a concurrent edit that set source_edited
    # cannot be missed through a stale in-memory copy (the edit path does not fire this
    # signal today, but this closes the window unconditionally).
    approval.refresh_from_db(fields=["source_edited"])
    if approval.source_edited:
        return MODE_MANUAL, "edited_signal"

    ctrl = ExecutionControl.get_solo()
    if not ctrl.auto_execution_enabled:
        return MODE_MANUAL, "auto_execution_disabled"
    if ctrl.signal_execution_mode != ExecutionControl.SignalExecutionMode.SHADOW:
        return MODE_MANUAL, "execution_mode_not_shadow"
    if order_creation_kill_reason():
        return MODE_MANUAL, "kill_switch"

    provider = approval.provider
    if provider is None or not provider.is_armed():
        return MODE_MANUAL, "provider_not_armed"

    cfg = SignalSourceConfig.objects.filter(source=approval.source).first()
    if cfg is None or not cfg.auto_demo_execution_enabled:
        return MODE_MANUAL, "source_not_enabled"

    if not _confidence_ok(provider):
        return MODE_MANUAL, "parser_confidence_below_medium"

    if resolve_auto_shadow_target() is None:
        return MODE_MANUAL, "no_unique_auto_shadow_assignment"

    return MODE_AUTO_SHADOW, "armed"


def should_auto_execute(approval):
    """``(bool, reason)``. Fail-closed: any exception → ``(False, error)``."""
    try:
        mode, reason = effective_mode(approval)
        return (mode == MODE_AUTO_SHADOW), reason
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("auto_router: effective_mode failed (%s) -> MANUAL", exc)
        return False, f"error:{type(exc).__name__}"


def _auto_execute_shadow(approval, acquired) -> None:
    """Run the shadow-only path: approve → plan → promote-to-shadow.

    Output is ``PLACE_ORDER_SHADOW`` jobs ONLY. Any rejection/exception is swallowed —
    no order is ever placed and acquisition is never disrupted.
    """
    actor = _system_actor()
    target = resolve_auto_shadow_target()
    if actor is None or target is None:
        logger.info("auto_router: unresolved actor/target -> manual")
        return
    signal_ts = getattr(acquired, "telegram_date", None)
    try:
        # Re-read live state before acting (idempotent + audit-preserving on a race/replay).
        approval.refresh_from_db(fields=["status", "source_edited"])
        if approval.source_edited:
            return  # last-line defense: never auto-execute an edited signal
        if approval.status == approval.Status.PENDING_APPROVAL:
            intake_services.approve(approval, reviewer=actor, notes="auto-shadow (system)")
        elif approval.status != approval.Status.APPROVED:
            return  # rejected / expired / quarantined — never act; don't overwrite a decision
        # else: already APPROVED (e.g. by a human) — keep their metadata; just plan/promote.
        plan = plan_demo_execution(
            approval, account=target.account, actor=actor, signal_timestamp=signal_ts,
        )
        if plan.status != SignalExecutionPlan.Status.PLANNED:
            return  # HELD/VOIDED (data/staleness) → no promotion, no job
        promote_plan_to_shadow_jobs(plan, actor=actor)  # PLACE_ORDER_SHADOW only
    except (PlanRejected, PromotionRejected) as exc:
        logger.info("auto_router: shadow path rejected (%s)", getattr(exc, "code", exc))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("auto_router: shadow path error (%s)", exc)


def route_acquired_signal(sender=None, *, provider=None, acquired=None,
                          approval=None, outcome=None, **kwargs) -> None:
    """``signal_acquired`` receiver. Fail-closed; a no-op unless fully armed.

    Only an INTAKEN outcome with an approval is considered; everything else (dropped,
    stale, quarantined, update) returns immediately without touching the DB.
    """
    try:
        if outcome != AcquiredMessage.Outcome.INTAKEN or approval is None:
            return
        go, reason = should_auto_execute(approval)
        if not go:
            return  # MANUAL — the approval stays PENDING exactly as today
        _auto_execute_shadow(approval, acquired)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("auto_router: route failed (%s) -> left manual", exc)
