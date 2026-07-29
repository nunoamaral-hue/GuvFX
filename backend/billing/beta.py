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


def is_admitted_beta_tester(user) -> bool:
    """CVM controlled-beta admission check. True only for an email on the ACTIVE admission allowlist
    (``BetaTester``). This is a strictly PER-IDENTITY admission — it never opens onboarding globally, and
    an empty allowlist means nobody is admitted (public onboarding stays closed via ``beta_onboarding_open``)."""
    from .models import BetaTester
    email = (getattr(user, "email", "") or "").strip().lower()
    if not email:
        return False
    return BetaTester.objects.filter(email__iexact=email, is_active=True).exists()


def beta_onboarding_open() -> bool:
    """The server-side beta-onboarding gate. **DEFAULT CLOSED.** External beta onboarding may proceed
    only when explicitly opened via ``BETA_ONBOARDING_ENABLED`` (settings or env) — which must NOT happen
    until Phase-4 proves per-user isolation. Nothing in Phase 0 opens it.

    RETAINED for backward-compat / staff paths only. ADR-0021 makes ``onboarding_available()`` the single
    eligibility gate for customers; new code MUST NOT use this for customer eligibility."""
    val = getattr(settings, "BETA_ONBOARDING_ENABLED", None)
    if val is None:
        val = os.getenv("BETA_ONBOARDING_ENABLED", "")
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _flag(name: str, default: str) -> bool:
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def registration_enabled() -> bool:
    """Operational kill-switch for new registrations. DEFAULT ON (registration is public)."""
    return _flag("REGISTRATION_ENABLED", "true")


def provisioning_service_healthy() -> bool:
    """Provisioning is operationally available (the dedicated-runtime kill switch is on)."""
    from terminal_provisioning.beta_capacity import beta_runtimes_enabled
    return beta_runtimes_enabled()


def runtime_capacity_available() -> bool:
    """A NEW dedicated runtime slot can be allocated (global cap + host capacity). Enforced hard and
    idempotently at ``reserve_beta_slot``; this is the entry-time view of the same fact."""
    from terminal_provisioning.beta_capacity import (
        BETA_MAX_ACTIVE_RUNTIMES, active_beta_runtime_count, host_has_capacity)
    return active_beta_runtime_count() < BETA_MAX_ACTIVE_RUNTIMES and host_has_capacity()


def _user_holds_runtime(user) -> bool:
    """True if the user already owns a beta runtime slot (so capacity must never block their progress)."""
    if user is None:
        return False
    from terminal_provisioning.models import AccountRuntime
    return AccountRuntime.objects.filter(
        trading_account__user=user, cohort=AccountRuntime.Cohort.BETA).exists()


def onboarding_available(user=None) -> tuple[bool, str]:
    """ADR-0021 — the SINGLE customer onboarding-eligibility gate. Operational health, NOT an allowlist.
    Returns ``(ok, reason)`` where reason is a structured code the frontend maps to friendly copy.
    Capacity blocks only a user who does not already hold a runtime slot (an existing holder progresses)."""
    if not registration_enabled():
        return (False, "registration_closed")
    if not provisioning_service_healthy():
        return (False, "provisioning_unhealthy")
    if not _user_holds_runtime(user) and not runtime_capacity_available():
        return (False, "capacity_full")
    return (True, "available")
