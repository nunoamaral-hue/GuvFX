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

    RETAINED for backward-compat / staff paths only. ADR-0021 replaces this with STAGE-SPECIFIC operational
    predicates (``registration_allowed``, ``can_reserve_new_runtime``, runtime-state progression); new code
    MUST NOT use this for customer eligibility."""
    val = getattr(settings, "BETA_ONBOARDING_ENABLED", None)
    if val is None:
        val = os.getenv("BETA_ONBOARDING_ENABLED", "")
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _flag(name: str, default: str) -> bool:
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in ("1", "true", "yes", "on")


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# ADR-0021 — STAGE-SPECIFIC operational predicates. There is deliberately NO universal onboarding gate:
# capacity / registration / provisioning-availability must never block a customer who ALREADY owns a
# runtime. Each predicate is scoped to exactly one transition. Each returns a structured reason code.
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def registration_allowed() -> bool:
    """Governs ONLY the creation of NEW user accounts (the register endpoint). Closing registration must
    never block an existing registered customer from logging in or completing onboarding."""
    return _flag("REGISTRATION_ENABLED", "true")


def provisioning_service_healthy() -> bool:
    """The dedicated-runtime provisioner is operationally available: the kill switch is on AND the
    provisioner worker is heartbeating recently. Used ONLY when allocating a NEW runtime."""
    from terminal_provisioning.beta_capacity import beta_runtimes_enabled
    if not beta_runtimes_enabled():
        return False
    return _provisioner_heartbeat_fresh()


def _provisioner_heartbeat_fresh() -> bool:
    """Beta provisioner liveness. Fresh if a heartbeat was recorded within
    ``BETA_PROVISIONER_HEARTBEAT_TTL_SECONDS`` (default 120). Absent record ⇒ treat as healthy only when
    the worker model has no heartbeat column yet (backward-compat); otherwise fail closed."""
    ttl = int(getattr(settings, "BETA_PROVISIONER_HEARTBEAT_TTL_SECONDS", 0)
              or os.getenv("BETA_PROVISIONER_HEARTBEAT_TTL_SECONDS", "120"))
    try:
        from terminal_provisioning.models import ProvisionerHeartbeat  # type: ignore
    except Exception:
        return True  # heartbeat model not present in this build — do not block on it
    from django.utils import timezone
    hb = ProvisionerHeartbeat.objects.order_by("-updated_at").first()
    if hb is None:
        return False
    return (timezone.now() - hb.updated_at).total_seconds() <= ttl


def host_agent_reachable() -> bool:
    """The Windows agent/host required to MATERIALISE a runtime is reachable. Used ONLY when allocating a
    NEW runtime (never on progression of an existing one). Cheap cached signal; never a per-request probe
    in the hot path — see host-reachability cache. Default True if no signal is configured."""
    val = getattr(settings, "HOST_AGENT_REACHABLE", None)
    if val is None:
        val = os.getenv("HOST_AGENT_REACHABLE", "")
    if str(val).strip() == "":
        return True  # no explicit signal configured — do not block (host_has_capacity already probes)
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def runtime_capacity_available() -> bool:
    """A NEW dedicated runtime slot can be allocated (global cap + host capacity). Enforced hard and
    idempotently at ``reserve_beta_slot``; this is the entry-time view of the same fact."""
    from terminal_provisioning.beta_capacity import (
        BETA_MAX_ACTIVE_RUNTIMES, active_beta_runtime_count, host_has_capacity)
    return active_beta_runtime_count() < BETA_MAX_ACTIVE_RUNTIMES and host_has_capacity()


def user_holds_runtime(user) -> bool:
    """True if the user already owns a BETA runtime (active OR held). Such a user must NEVER be blocked by
    capacity/registration/provisioning-availability — their progression is driven by the runtime's state."""
    if user is None:
        return False
    from terminal_provisioning.beta_capacity import HELD_STATES
    from terminal_provisioning.models import AccountRuntime, RuntimeState
    live = set(HELD_STATES) | {RuntimeState.RUNNING}
    return AccountRuntime.objects.filter(
        trading_account__user=user, cohort=AccountRuntime.Cohort.BETA, state__in=live).exists()


def can_reserve_new_runtime(user) -> tuple[bool, str]:
    """ADR-0021 — gate for allocating a NEW dedicated runtime (BrokerAdded → Provisioning). Applies ONLY
    when the customer does NOT already own an active/held runtime; an existing owner is a no-op re-drive
    (``reserve_beta_slot`` returns their held runtime). Returns ``(ok, reason)``."""
    if user_holds_runtime(user):
        return (True, "already_owned")  # not a new reservation — reserve_beta_slot is idempotent
    from terminal_provisioning.beta_capacity import beta_runtimes_enabled
    if not beta_runtimes_enabled():
        return (False, "provisioning_disabled")
    if not _provisioner_heartbeat_fresh():
        return (False, "provisioner_unhealthy")
    if not host_agent_reachable():
        return (False, "host_unreachable")
    if not runtime_capacity_available():
        return (False, "capacity_full")
    return (True, "available")
