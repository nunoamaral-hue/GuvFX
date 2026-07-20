"""GFX-BETA-PHASE0 Increment 4 — beta cohort: entitlement grant + the server-side onboarding gate.

Beta entitlement is auto-assigned in the data model (payment-bypassed). It does NOT make trading
reachable: external onboarding stays behind ``beta_onboarding_open()`` (DEFAULT CLOSED), which must not
be opened until the Phase-4 isolation gates pass, and terminal provisioning is undeployed.
"""
import os

from django.conf import settings

from .models import UserSubscriptionState


def grant_beta_entitlement(user) -> UserSubscriptionState:
    """Auto-assign the beta plan for a user (idempotent). Never clobbers an existing PAID plan — only
    a viewer/empty/beta state is (re)set to beta. Does NOT open onboarding."""
    state, _ = UserSubscriptionState.objects.get_or_create(user=user)
    paid = {UserSubscriptionState.Plan.STARTER_TRIAL, UserSubscriptionState.Plan.STANDARD,
            UserSubscriptionState.Plan.PRO, UserSubscriptionState.Plan.ADVANCED}
    # Never clobber a real (even lapsed/expired) paid subscription — a lapsed paid plan has
    # viewer_mode=True by the model invariant, so guard on the plan alone, not viewer_mode.
    if state.current_plan in paid:
        return state
    state.current_plan = UserSubscriptionState.Plan.BETA
    state.plan_status = UserSubscriptionState.PlanStatus.ACTIVE
    state.viewer_mode = False
    state.save()
    return state


def beta_onboarding_open() -> bool:
    """The server-side beta-onboarding gate. **DEFAULT CLOSED.** External beta onboarding may proceed
    only when explicitly opened via ``BETA_ONBOARDING_ENABLED`` (settings or env) — which must NOT happen
    until Phase-4 proves per-user isolation. Nothing in Phase 0 opens it."""
    val = getattr(settings, "BETA_ONBOARDING_ENABLED", None)
    if val is None:
        val = os.getenv("BETA_ONBOARDING_ENABLED", "")
    return str(val).strip().lower() in ("1", "true", "yes", "on")
