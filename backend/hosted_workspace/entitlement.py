"""hosted_workspace.entitlement — ADR-0034 Onboarding admission predicate (DARK).

Answers "may THIS user use / request a Hosted Persistent MT5 Workspace?" by combining the DARK subsystem
flags with the Hosted Workspace CAPABILITY. Fail-closed: an absent flag or capability = denied.

ADR-0034 amendment (Hosted-capability / commercial-plan decoupling): the capability is INDEPENDENT of the
commercial subscription. It is a fail-closed OR of two separate sources — the durable commercial entitlement
``can_use_hosted_workspace`` (a plan may grant it) OR active membership of the Hosted Beta programme (the
``BetaTester`` admission allowlist). A tester therefore keeps whatever commercial plan they selected at
registration and still gains hosted access; the Hosted Beta programme, not the commercial plan, is the
source of that access. Reuses the certified billing entitlement engine and the ADR-0021 ``(ok, reason)``
predicate shape (billing/beta.py). Admission is the Access/Visibility layer ONLY — it grants NO order
authority; the customer journey stops at assignment-eligibility, which is strictly below arming
(``execution_enabled``) and below the live order-time bridge gate.
"""
from __future__ import annotations

from billing.entitlements import resolve_entitlements
from billing.models import UserSubscriptionState

from hosted_workspace.flags import (
    hosted_persistent_mt5_enabled,
    hosted_workspace_onboarding_enabled,
)

# Stable, secret-free reason codes.
ADMIT_OK = "ok"
DENY_NO_USER = "no_user"
DENY_SUBSYSTEM_DARK = "hosted_workspace_subsystem_dark"
DENY_ONBOARDING_DARK = "onboarding_dark"
DENY_NOT_ENTITLED = "not_entitled"


def _entitlements(user):
    state = UserSubscriptionState.objects.filter(user=user).first()
    return resolve_entitlements(state)


def has_hosted_workspace_capability(user) -> bool:
    """Does *user* hold Hosted Workspace CAPABILITY? (ADR-0034 amendment — capability is INDEPENDENT of the
    commercial subscription.) Fail-closed OR of two SEPARATE sources:

      • the durable COMMERCIAL entitlement ``can_use_hosted_workspace`` (a plan may grant it), OR
      • active membership of the HOSTED BETA programme (the ``BetaTester`` admission allowlist).

    So a Hosted Beta tester keeps whatever commercial plan they chose at registration and still gains hosted
    access, and — independently — a commercial plan can grant it without any beta admission. No user, or
    NEITHER source, ⇒ False (fail-closed). This is an Access/Visibility capability ONLY: it grants NO order
    authority (that stays ``can_deploy_automation`` + the live bridge gate) and opens onboarding for nobody by
    itself — every caller still ANDs the DARK subsystem/onboarding flags on top of it."""
    if user is None or getattr(user, "pk", None) is None:
        return False
    # Source 1 — commercial entitlement (billing stays commercial-only; this module composes the two concerns).
    if _entitlements(user).can_use_hosted_workspace:
        return True
    # Source 2 — Hosted Beta programme membership (independent of the commercial plan). Lazy import: the beta
    # allowlist is a billing concern queried live and kept out of import-time coupling.
    from billing.beta import is_admitted_beta_tester
    return bool(is_admitted_beta_tester(user))


def hosted_workspace_admission(user) -> tuple[bool, str]:
    """``(ok, reason)`` — may *user* use the Hosted Workspace onboarding journey? Fail-closed AND of: the
    master flag ON, the onboarding flag ON, and the Hosted Workspace capability
    (``has_hosted_workspace_capability`` — commercial entitlement OR Hosted Beta programme membership). Checks
    are most-specific-first so each reason code stays reachable. Read live; never grants order authority."""
    if user is None or getattr(user, "pk", None) is None:
        return False, DENY_NO_USER
    if not hosted_persistent_mt5_enabled():
        return False, DENY_SUBSYSTEM_DARK
    if not hosted_workspace_onboarding_enabled():
        return False, DENY_ONBOARDING_DARK
    if not has_hosted_workspace_capability(user):
        return False, DENY_NOT_ENTITLED
    return True, ADMIT_OK


def user_holds_workspace(user) -> bool:
    """True iff *user* already OWNS a ``HostedMt5Workspace`` — resolved through the immutable
    ``trading_account.user`` binding (the single source of ownership; there is no separate ``owner`` FK). The
    basis for one-workspace-per-user idempotency at request time. Fail-closed on a missing user."""
    if user is None or getattr(user, "pk", None) is None:
        return False
    from hosted_workspace.models import HostedMt5Workspace
    return HostedMt5Workspace.objects.filter(trading_account__user=user).exists()
