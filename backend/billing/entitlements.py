"""
Computed entitlements service.

Maps UserSubscriptionState (current_plan, plan_status, viewer_mode) into a
canonical Entitlements object that downstream code can branch on without
reimplementing plan/status logic.

No database table.  No side effects.  Deterministic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from .models import UserSubscriptionState


# ---------------------------------------------------------------------------
# Canonical entitlements object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entitlements:
    # ---- Capability gates ----
    can_view_dashboard: bool
    can_browse_marketplace: bool
    can_run_backtests: bool
    can_assign_strategies: bool
    can_deploy_automation: bool

    # ---- Numeric limits ----
    max_trading_accounts: int
    max_active_strategies: int

    # ---- Tier ----
    historical_data_tier: str  # "none" | "basic" | "standard" | "full"

    # ---- Source metadata ----
    source_plan: Optional[str]
    source_plan_status: str
    viewer_mode: bool
    resolved_access_mode: str  # "viewer" | "trial" | "active" | "degraded"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Plan entitlement mappings (constant, not in DB)
# ---------------------------------------------------------------------------

_PLAN_ENTITLEMENTS: dict[str, dict] = {
    "starter_trial": {
        "can_view_dashboard": True,
        "can_browse_marketplace": True,
        "can_run_backtests": True,
        "can_assign_strategies": True,
        "can_deploy_automation": False,
        "max_trading_accounts": 1,
        "max_active_strategies": 2,
        "historical_data_tier": "basic",
    },
    "standard": {
        "can_view_dashboard": True,
        "can_browse_marketplace": True,
        "can_run_backtests": True,
        "can_assign_strategies": True,
        "can_deploy_automation": True,
        "max_trading_accounts": 2,
        "max_active_strategies": 5,
        "historical_data_tier": "standard",
    },
    "pro": {
        "can_view_dashboard": True,
        "can_browse_marketplace": True,
        "can_run_backtests": True,
        "can_assign_strategies": True,
        "can_deploy_automation": True,
        "max_trading_accounts": 5,
        "max_active_strategies": 15,
        "historical_data_tier": "full",
    },
    "advanced": {
        "can_view_dashboard": True,
        "can_browse_marketplace": True,
        "can_run_backtests": True,
        "can_assign_strategies": True,
        "can_deploy_automation": True,
        "max_trading_accounts": 10,
        "max_active_strategies": 50,
        "historical_data_tier": "full",
    },
}

_VIEWER_DEFAULTS: dict = {
    "can_view_dashboard": True,
    "can_browse_marketplace": True,
    "can_run_backtests": False,
    "can_assign_strategies": False,
    "can_deploy_automation": False,
    "max_trading_accounts": 0,
    "max_active_strategies": 0,
    "historical_data_tier": "none",
}

# Plan statuses that collapse to viewer-equivalent entitlements.
_VIEWER_STATUSES = frozenset(
    {
        UserSubscriptionState.PlanStatus.EXPIRED,
        UserSubscriptionState.PlanStatus.VIEWER_ONLY,
        UserSubscriptionState.PlanStatus.CANCELLED,
    }
)


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------


def resolve_entitlements(
    state: Optional[UserSubscriptionState],
) -> Entitlements:
    """
    Compute entitlements from a UserSubscriptionState instance.

    Resolution order (deterministic, no side effects):
      1. If *state* is None or current_plan is empty  -> viewer defaults
      2. Resolve base plan entitlements from current_plan
      3. Apply plan_status restrictions
      4. Apply viewer_mode override **last** (hard override)
      5. Return frozen Entitlements dataclass

    ``past_due`` is intentionally permissive in Phase 1: full plan
    capabilities are preserved, with ``resolved_access_mode="degraded"``.
    """

    # ------------------------------------------------------------------
    # 1. No subscription row at all -> viewer defaults
    # ------------------------------------------------------------------
    if state is None:
        return Entitlements(
            **_VIEWER_DEFAULTS,
            source_plan=None,
            source_plan_status=UserSubscriptionState.PlanStatus.VIEWER_ONLY,
            viewer_mode=True,
            resolved_access_mode="viewer",
        )

    plan = state.current_plan  # may be None / ""
    plan_status = state.plan_status
    is_viewer = state.viewer_mode

    # ------------------------------------------------------------------
    # 2. Resolve base plan entitlements
    # ------------------------------------------------------------------
    base = dict(_PLAN_ENTITLEMENTS.get(plan, _VIEWER_DEFAULTS)) if plan else dict(_VIEWER_DEFAULTS)

    # ------------------------------------------------------------------
    # 3. Apply plan_status restrictions
    # ------------------------------------------------------------------
    if plan_status in _VIEWER_STATUSES:
        # Expired / cancelled / viewer_only -> collapse to viewer.
        base = dict(_VIEWER_DEFAULTS)
        resolved_mode = "viewer"
    elif plan_status == UserSubscriptionState.PlanStatus.PAST_DUE:
        # Grace period: keep full capabilities, flag the mode.
        resolved_mode = "degraded"
    elif plan_status == UserSubscriptionState.PlanStatus.TRIAL_ACTIVE:
        resolved_mode = "trial"
    elif plan_status == UserSubscriptionState.PlanStatus.ACTIVE:
        resolved_mode = "active"
    else:
        # Unknown status -> safe fallback to viewer.
        base = dict(_VIEWER_DEFAULTS)
        resolved_mode = "viewer"

    # ------------------------------------------------------------------
    # 4. Apply viewer_mode override last (hard override)
    # ------------------------------------------------------------------
    if is_viewer:
        base = dict(_VIEWER_DEFAULTS)
        resolved_mode = "viewer"

    # ------------------------------------------------------------------
    # 5. Return canonical entitlement object
    # ------------------------------------------------------------------
    return Entitlements(
        **base,
        source_plan=plan or None,
        source_plan_status=plan_status,
        viewer_mode=is_viewer,
        resolved_access_mode=resolved_mode,
    )
