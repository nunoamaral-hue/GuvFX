"""hosted_workspace.entitlement — ADR-0034 Onboarding admission predicate (DARK).

Answers "may THIS user use / request a Hosted Persistent MT5 Workspace?" by combining the DARK subsystem
flags with the durable billing entitlement capability. Fail-closed: an absent flag or capability = denied.
Reuses the certified billing entitlement engine (the durable capability) and the ADR-0021 ``(ok, reason)``
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


def hosted_workspace_admission(user) -> tuple[bool, str]:
    """``(ok, reason)`` — may *user* use the Hosted Workspace onboarding journey? Fail-closed AND of: the
    master flag ON, the onboarding flag ON, and the durable ``can_use_hosted_workspace`` capability. Checks
    are most-specific-first so each reason code stays reachable. Read live; never grants order authority."""
    if user is None or getattr(user, "pk", None) is None:
        return False, DENY_NO_USER
    if not hosted_persistent_mt5_enabled():
        return False, DENY_SUBSYSTEM_DARK
    if not hosted_workspace_onboarding_enabled():
        return False, DENY_ONBOARDING_DARK
    if not _entitlements(user).can_use_hosted_workspace:
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
