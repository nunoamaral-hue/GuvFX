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

    # GFX-BETA-PHASE0 Increment 4 — beta cohort flag (default False; set only by the "beta" plan).
    # DEPRECATED as a marketplace-visibility gate (ADR-0021): visibility is now owned by
    # ``visible_marketplace_catalogues`` below. ``is_beta`` remains only as a read-only cohort LABEL.
    is_beta: bool = False

    # ADR-0021 — the entitlement layer OWNS which marketplace CATALOGUES a customer may browse (the
    # Visibility layer of Access → Visibility → Activation → Execution). Catalogue names are enduring
    # product groupings (NOT rollout phases). A consumer NEVER evaluates entitlement booleans to decide
    # visibility — it asks the entitlement for this set and renders items whose catalogue is in it.
    # Default empty (a viewer sees no catalogues).
    visible_marketplace_catalogues: frozenset = frozenset()

    # ADR-0034 Onboarding — durable capability: MAY this user use the Hosted Persistent MT5 Workspace
    # journey. DEFAULT False (fail-closed; absent grant = denied). It is a Visibility/Access gate only —
    # it NEVER grants order authority (that stays the layered arm + live bridge gate). Granted as DATA
    # (a plan/tier or the beta cohort), never inferred from accidental state; no existing user is
    # auto-opted-in. Combined at the admission predicate with the master flag + not-already-holding.
    can_use_hosted_workspace: bool = False

    # Phase C (Concurrent Broker Accounts) — product-model policy, config-driven so 5 is an ENTITLEMENT
    # value not a hard-coded ceiling. ``account_mode`` selects the product behaviour; ``concurrent_broker_
    # account_limit`` is the max simultaneously-ACTIVE broker accounts allowed in CONCURRENT mode (ignored
    # in STANDARD, where at most one account is active at a time). Defaults are conservative (STANDARD / 1)
    # so an unset user behaves exactly as today; raising the limit to 10/20/50 later is a data change, not
    # code. Enforcement is DARK (see ``trading.account_entitlement``): these fields carry policy only.
    account_mode: str = "standard"          # "standard" | "concurrent"
    concurrent_broker_account_limit: int = 1

    def to_dict(self) -> dict:
        # Keep this JSON-safe: the catalogue set is a frozenset (not JSON-serialisable) — emit it as a
        # sorted list so a caller can ``json.dumps(ent.to_dict())`` without a TypeError.
        d = asdict(self)
        d["visible_marketplace_catalogues"] = sorted(self.visible_marketplace_catalogues)
        return d


class AccountMode:
    """Phase C product behaviour for a user's broker accounts (config/entitlement-driven).

    STANDARD  — the user may OWN several broker accounts but only ONE is active (tradeable) at a time;
                activating another safely deactivates the current one.
    CONCURRENT — up to ``concurrent_broker_account_limit`` broker accounts may be active simultaneously.
    """
    STANDARD = "standard"
    CONCURRENT = "concurrent"
    ALL = frozenset({STANDARD, CONCURRENT})


class MarketplaceCatalogue:
    """Enduring marketplace catalogue identifiers (ADR-0021 Visibility layer). Names represent durable
    product groupings, not rollout phases. Marketplace items declare their catalogue; the entitlement
    layer answers which catalogues a customer may browse."""
    SIGNAL_COPY = "signal_copy"   # copy-trading strategies that mirror an external signal feed


# Every ACTIVE onboarding plan may browse the signal-copy catalogue. Adding a future catalogue is DATA
# (extend this set + the relevant plans), never a new boolean gate.
ALL_MARKETPLACE_CATALOGUES: frozenset = frozenset({MarketplaceCatalogue.SIGNAL_COPY})
_ONBOARDING_CATALOGUES: frozenset = frozenset({MarketplaceCatalogue.SIGNAL_COPY})


# ---------------------------------------------------------------------------
# Plan entitlement mappings (constant, not in DB)
# ---------------------------------------------------------------------------

_PLAN_ENTITLEMENTS: dict[str, dict] = {
    "starter_trial": {
        "visible_marketplace_catalogues": _ONBOARDING_CATALOGUES,
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
        "visible_marketplace_catalogues": _ONBOARDING_CATALOGUES,
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
        "visible_marketplace_catalogues": _ONBOARDING_CATALOGUES,
        "can_view_dashboard": True,
        "can_browse_marketplace": True,
        "can_run_backtests": True,
        "can_assign_strategies": True,
        "can_deploy_automation": True,
        "max_trading_accounts": 5,
        "max_active_strategies": 15,
        "historical_data_tier": "full",
        "concurrent_broker_account_limit": 5,
    },
    "advanced": {
        "visible_marketplace_catalogues": _ONBOARDING_CATALOGUES,
        "can_view_dashboard": True,
        "can_browse_marketplace": True,
        "can_run_backtests": True,
        "can_assign_strategies": True,
        "can_deploy_automation": True,
        "max_trading_accounts": 10,
        "max_active_strategies": 50,
        "historical_data_tier": "full",
        "concurrent_broker_account_limit": 5,
    },
    # GFX-BETA-PHASE0 Increment 4 — the beta cohort (payment-bypassed). Grants config-level
    # capabilities (up to 10 broker accounts, assign strategies) so a beta user can set things up.
    # It does NOT by itself make trading reachable: external onboarding stays gated
    # (BETA_ONBOARDING_ENABLED, default off) and terminal provisioning is undeployed.
    "beta": {
        "visible_marketplace_catalogues": _ONBOARDING_CATALOGUES,
        "can_view_dashboard": True,
        "can_browse_marketplace": True,
        "can_run_backtests": True,
        "can_assign_strategies": True,
        # can_deploy_automation is the server-side EXECUTION-authorization entitlement (require_entitlement
        # gates create_open_trade_job / create_place_order_job / the manual open-trade endpoints). It stays
        # FALSE for Phase-0 beta — a fail-closed block on placing orders that is INDEPENDENT of whether a
        # terminal is provisioned. Do NOT flip this to True until Phase 4 (tie it to beta_onboarding_open()).
        "can_deploy_automation": False,
        "max_trading_accounts": 10,
        "max_active_strategies": 50,
        "historical_data_tier": "standard",
        "concurrent_broker_account_limit": 5,
        "is_beta": True,
        # ADR-0034 Onboarding — the beta cohort is the Hosted Workspace pilot cohort, so it MAY use the
        # onboarding journey (DATA grant). This is Visibility/Access only: it does NOT grant order authority
        # (can_deploy_automation stays False above), and the journey itself stops at assignment-eligibility,
        # which is strictly below arming and below the live order-time bridge gate.
        "can_use_hosted_workspace": True,
    },
}

_VIEWER_DEFAULTS: dict = {
    "visible_marketplace_catalogues": frozenset(),
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


# ---------------------------------------------------------------------------
# Override-aware resolver (Phase C) — layers active EntitlementOverride rows on
# top of the plan-derived entitlements.
# ---------------------------------------------------------------------------

# Numeric limits that an operator override may raise/lower; coerced to int, else ignored.
_NUMERIC_OVERRIDE_CAPS = frozenset({"max_trading_accounts", "max_active_strategies",
                                    "concurrent_broker_account_limit"})
# Boolean capability gates an override may grant/deny.
_BOOL_OVERRIDE_CAPS = frozenset({"can_view_dashboard", "can_browse_marketplace", "can_run_backtests",
                                 "can_assign_strategies", "can_deploy_automation",
                                 "can_use_hosted_workspace"})


def _coerce_override(capability: str, override_value):
    """Extract the effective value from an ``EntitlementOverride.override_value`` payload for a KNOWN
    capability, or return ``None`` to IGNORE a malformed/unknown override (fail-safe — a bad override
    never crashes resolution and never silently corrupts an entitlement)."""
    if not isinstance(override_value, dict):
        return None
    if capability in _BOOL_OVERRIDE_CAPS:
        if "granted" in override_value:
            return bool(override_value["granted"])
        return None
    if capability in _NUMERIC_OVERRIDE_CAPS:
        try:
            return int(override_value.get("value"))
        except (TypeError, ValueError):
            return None
    if capability == "account_mode":
        val = str(override_value.get("value", "")).strip().lower()
        return val if val in AccountMode.ALL else None
    return None


def resolve_effective_entitlements(user) -> Entitlements:
    """Plan-derived entitlements (``resolve_entitlements``) with any ACTIVE, non-expired
    ``EntitlementOverride`` rows applied on top — the authoritative per-user resolver.

    This is what fixes the previously-dead ``EntitlementOverride('max_trading_accounts')`` path. It is
    additive and side-effect-free; callers that don't need overrides keep using ``resolve_entitlements``.
    A user with NO active overrides gets exactly the plan-derived result (byte-identical).
    """
    from dataclasses import fields as _dc_fields
    base = resolve_entitlements(UserSubscriptionState.objects.filter(user=user).first())
    try:
        from admin_ops.models import EntitlementOverride
        from django.utils import timezone
        rows = list(EntitlementOverride.objects.filter(
            user=user, is_active=True, expires_at__gt=timezone.now()).values("capability", "override_value"))
    except Exception:  # noqa: BLE001 — absence of the app / a query error must not break resolution
        rows = []
    if not rows:
        return base
    data = {f.name: getattr(base, f.name) for f in _dc_fields(base)}
    for row in rows:
        cap = row.get("capability")
        if cap not in data:
            continue  # only known entitlement fields may be overridden
        val = _coerce_override(cap, row.get("override_value"))
        if val is not None:
            data[cap] = val
    return Entitlements(**data)
