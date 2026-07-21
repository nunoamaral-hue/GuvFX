"""CVM-Inc-3 sub-increment A — narrow beta-activation gate + runtime-ready semantics.

The global ``BETA_RUNTIMES_ENABLED`` flag is NECESSARY but NOT SUFFICIENT to launch a beta runtime
(Nuno control 2). ``assert_beta_activation_allowed`` is the single chokepoint the provisioner must pass
before ANY box side-effect (materialise/launch): it re-verifies EVERY activation condition so a
non-admitted user can never create, reserve or launch a beta runtime even while the global flag is on.

It also defines the three DISTINCT onboarding truth-states (Nuno requirement 1):
 - ``runtime_ready``    — the owned MT5 runtime is materialised, launched, process/session verified,
                          heartbeat-fresh and has an immutable Provisioning Verification Report.
 - ``broker_connected`` — the assigned broker login + server were INDEPENDENTLY verified by GuvFX
                          (``broker_login_verified``). FALSE throughout the broker-independent walk.
 - ``automation_ready`` — broker_connected AND an eligible strategy assigned AND sizing valid AND the
                          execution gate approved. (Composed by later increments.)
"""
from django.conf import settings
from django.utils import timezone

from .models import AccountRuntime, ProvisioningVerificationReport, RuntimeState

# A runtime heartbeat older than this is considered stale for the runtime_ready check.
RUNTIME_HEARTBEAT_FRESH_SECONDS = 300


class ActivationDenied(Exception):
    """Raised when a beta runtime may NOT be activated. ``reason_code`` is user-safe/sanitised."""
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def beta_active_tester_count() -> int:
    from billing.models import BetaTester
    return BetaTester.objects.filter(is_active=True).count()


def assert_beta_activation_allowed(runtime: AccountRuntime) -> None:
    """Control-2 narrow-activation gate. ALL conditions must hold before a beta runtime is
    materialised/launched, else raise ``ActivationDenied``. Enforced at the provisioner chokepoint so
    the global kill switch alone can never provision an arbitrary account."""
    from billing.beta import is_admitted_beta_tester
    from .beta_capacity import HELD_STATES, beta_runtimes_enabled
    from .beta_paths import canonical_beta_runtime_root

    # 1. global beta-runtime flag enabled
    if not beta_runtimes_enabled():
        raise ActivationDenied("beta_runtimes_disabled")
    # 2. runtime cohort is BETA (never a PRODUCTION / Nuno runtime)
    if runtime.cohort != AccountRuntime.Cohort.BETA:
        raise ActivationDenied("not_a_beta_runtime")
    acct = getattr(runtime, "trading_account", None)
    user = getattr(acct, "user", None)
    # 3. the owning account's user is an ACTIVE admitted BetaTester (per-identity, not just the flag)
    if user is None or not is_admitted_beta_tester(user):
        raise ActivationDenied("user_not_admitted")
    # 4. active admitted-tester count within the configured cap
    cap = int(getattr(settings, "BETA_MAX_TESTERS", 1) or 1)
    if beta_active_tester_count() > cap:
        raise ActivationDenied("tester_cap_exceeded")
    # 5. account belongs to that admitted user (structural: runtime is 1:1 with the account) — implied by 3
    # 6. a beta slot reservation succeeded (the runtime holds a pool slot)
    if runtime.state not in HELD_STATES:
        raise ActivationDenied("slot_not_reserved")
    # 7. runtime path is canonical AND server-generated (never a client-supplied path)
    expected = canonical_beta_runtime_root(runtime.runtime_uuid)
    if not runtime.runtime_root or runtime.runtime_root != expected:
        raise ActivationDenied("noncanonical_runtime_path")


def latest_verification_report(runtime: AccountRuntime):
    return (ProvisioningVerificationReport.objects
            .filter(runtime=runtime).order_by("-created_at").first())


def runtime_ready(runtime: AccountRuntime) -> bool:
    """``runtime_ready``: the owned MT5 runtime is verified up (RUNNING), heartbeat-fresh, and has an
    immutable Provisioning Verification Report. This is the broker-INDEPENDENT readiness — it says
    NOTHING about broker connectivity (see ``broker_connected``)."""
    if runtime.cohort != AccountRuntime.Cohort.BETA or runtime.state != RuntimeState.RUNNING:
        return False
    hb = runtime.last_heartbeat_at
    if not hb or (timezone.now() - hb).total_seconds() > RUNTIME_HEARTBEAT_FRESH_SECONDS:
        return False
    return latest_verification_report(runtime) is not None


def broker_connected(runtime: AccountRuntime) -> bool:
    """``broker_connected``: GuvFX independently verified the assigned broker login+server. FALSE in the
    broker-independent phase (there is no broker login) — never present a runtime as broker-connected
    before ``broker_login_verified`` is true on its report."""
    rep = latest_verification_report(runtime)
    return bool(rep and rep.broker_login_verified)
