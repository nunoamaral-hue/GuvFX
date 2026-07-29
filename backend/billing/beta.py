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
    """Beta provisioner liveness + health — **FAIL CLOSED**. Healthy ONLY if the singleton heartbeat was
    updated within ``BETA_PROVISIONER_HEARTBEAT_TTL_SECONDS`` (default 120) AND its status is a healthy
    state (``IDLE_READY`` or ``PROCESSING`` — a PROCESSING worker that keeps refreshing is healthy; a
    long-running job never reads stale). Missing / stale / DEGRADED / ERROR / unreadable ⇒ False."""
    try:
        # Parse the TTL INSIDE the guard — a malformed BETA_PROVISIONER_HEARTBEAT_TTL_SECONDS must fail
        # CLOSED (unhealthy), never raise an unhandled error out of a health check.
        ttl = int(getattr(settings, "BETA_PROVISIONER_HEARTBEAT_TTL_SECONDS", 0)
                  or os.getenv("BETA_PROVISIONER_HEARTBEAT_TTL_SECONDS", "120"))
        from django.utils import timezone

        from terminal_provisioning.models import ProvisionerHeartbeat
        hb = ProvisionerHeartbeat.objects.filter(pk=ProvisionerHeartbeat.SINGLETON_ID).first()
        if hb is None or hb.updated_at is None:
            return False
        if (timezone.now() - hb.updated_at).total_seconds() > ttl:
            return False
        return hb.status in ProvisionerHeartbeat.HEALTHY_STATES
    except Exception:  # unknown ⇒ fail closed
        return False


def host_agent_reachable() -> bool:
    """Windows agent/host reachability for a NEW runtime — **FAIL CLOSED**. An explicit
    ``HOST_AGENT_REACHABLE`` override (tests/ops) wins if set; otherwise a **bounded live HTTP probe with an
    explicit timeout** decides. Missing config / transport error / timeout / unknown ⇒ False. Only ever on
    the NEW-reservation path (rare), never on existing-runtime progression."""
    override = getattr(settings, "HOST_AGENT_REACHABLE", None)
    if override is None:
        override = os.getenv("HOST_AGENT_REACHABLE", "")
    if str(override).strip() != "":  # explicit override (deterministic tests / ops break-glass)
        return str(override).strip().lower() in ("1", "true", "yes", "on")
    base = (getattr(settings, "GUVFX_WINDOWS_AGENT_BASE_URL", "")
            or os.getenv("GUVFX_WINDOWS_AGENT_BASE_URL", "")).strip()
    if not base:
        return False  # no agent configured ⇒ cannot reserve ⇒ fail closed
    # Short bounded cache to prevent a probe storm across many reservations.
    from django.utils import timezone
    cache_ttl = float(getattr(settings, "HOST_AGENT_PROBE_CACHE_SECONDS", 0)
                      or os.getenv("HOST_AGENT_PROBE_CACHE_SECONDS", "10"))
    now = timezone.now()
    c = _HOST_AGENT_PROBE_CACHE
    if c["at"] is not None and (now - c["at"]).total_seconds() <= cache_ttl:
        return bool(c["ok"])
    ok = _probe_host_agent(base)
    _HOST_AGENT_PROBE_CACHE["at"] = now
    _HOST_AGENT_PROBE_CACHE["ok"] = ok
    return ok


# Fixed, trusted, read-only liveness endpoint. Liveness is proven ONLY by an expected status:
#   200 (open health) or 401/403 (auth-gated health, reached with NO credentials — nothing sensitive sent
#   or logged). Any other status / timeout / connection error / malformed response ⇒ fail closed.
_HOST_AGENT_PROBE_CACHE = {"at": None, "ok": None}
_HOST_AGENT_LIVENESS_STATUSES = (200, 401, 403)


def _probe_host_agent(base: str) -> bool:
    import urllib.error
    import urllib.request
    timeout = float(getattr(settings, "HOST_AGENT_PROBE_TIMEOUT_SECONDS", 0)
                    or os.getenv("HOST_AGENT_PROBE_TIMEOUT_SECONDS", "3"))
    path = (getattr(settings, "HOST_AGENT_HEALTH_PATH", "")
            or os.getenv("HOST_AGENT_HEALTH_PATH", "/")).strip() or "/"
    url = base.rstrip("/") + "/" + path.lstrip("/")
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout)
        return 200 <= int(getattr(resp, "status", 0) or 0) < 300  # explicit 2xx = live
    except urllib.error.HTTPError as e:  # the agent RESPONDED — only expected auth statuses prove liveness
        return int(getattr(e, "code", 0) or 0) in _HOST_AGENT_LIVENESS_STATUSES
    except Exception:  # timeout / connection refused / DNS / malformed ⇒ unreachable ⇒ fail closed
        return False


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
