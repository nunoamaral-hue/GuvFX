"""ADR-0034 Onboarding — foundation: DARK admission predicate + derived-ownership invariant.

Proves: the onboarding flag defaults OFF; admission is a fail-closed AND of (master flag ∧ onboarding flag ∧
the durable ``can_use_hosted_workspace`` capability) with reachable most-specific reason codes; and that
ownership is the SINGLE immutable fact ``trading_account.user`` (there is no separate ``owner`` FK to
diverge — the workspace<->account binding is immutable, so the owner cannot drift). No order is placed and
nothing is armed.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from billing.models import UserSubscriptionState
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import entitlement as E
from hosted_workspace.flags import hosted_workspace_onboarding_enabled
from hosted_workspace.models import HostedMt5Workspace

U = get_user_model()

_FLAGS_ON = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_WORKSPACE_ONBOARDING_ENABLED="1")


def _user(name="u1", *, entitled=True):
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.update_or_create(
        user=u, defaults=dict(current_plan=("beta" if entitled else "starter_trial"),
                              plan_status="active", viewer_mode=False))
    return u


def _account(user, login="700900"):
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    return TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number=login,
                                         is_demo=True, broker_server=srv)


class OnboardingFlagTests(TestCase):
    def test_flag_default_off(self):
        self.assertFalse(hosted_workspace_onboarding_enabled())

    @override_settings(HOSTED_WORKSPACE_ONBOARDING_ENABLED="1")
    def test_flag_on(self):
        self.assertTrue(hosted_workspace_onboarding_enabled())


class AdmissionTests(TestCase):
    def test_dark_subsystem_denied(self):
        ok, reason = E.hosted_workspace_admission(_user())      # both flags OFF (default)
        self.assertFalse(ok)
        self.assertEqual(reason, E.DENY_SUBSYSTEM_DARK)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1")
    def test_onboarding_dark_denied(self):
        ok, reason = E.hosted_workspace_admission(_user())      # master on, onboarding off
        self.assertFalse(ok)
        self.assertEqual(reason, E.DENY_ONBOARDING_DARK)

    @override_settings(**_FLAGS_ON)
    def test_entitled_user_admitted(self):
        ok, reason = E.hosted_workspace_admission(_user(entitled=True))
        self.assertTrue(ok, reason)
        self.assertEqual(reason, E.ADMIT_OK)

    @override_settings(**_FLAGS_ON)
    def test_non_entitled_user_denied(self):
        ok, reason = E.hosted_workspace_admission(_user(name="u2", entitled=False))
        self.assertFalse(ok)
        self.assertEqual(reason, E.DENY_NOT_ENTITLED)

    def test_no_user_denied(self):
        ok, reason = E.hosted_workspace_admission(None)
        self.assertFalse(ok)
        self.assertEqual(reason, E.DENY_NO_USER)


class OwnershipTests(TestCase):
    """Ownership is the single immutable fact ``trading_account.user`` — there is no separate ``owner`` FK to
    diverge. Creating a workspace on a user's account IS ownership; entitlement resolves it through the join."""

    def test_owner_is_the_trading_account_user(self):
        u1 = _user("o3")
        ws = HostedMt5Workspace.objects.create(trading_account=_account(u1))
        self.assertEqual(ws.trading_account.user_id, u1.pk)      # ownership derived, not a second column
        self.assertTrue(E.user_holds_workspace(u1))

    def test_user_without_workspace_is_not_holding(self):
        u1 = _user("o4")
        self.assertFalse(E.user_holds_workspace(u1))             # no workspace on any of their accounts

    def test_holding_is_scoped_to_the_owning_user(self):
        u1, u2 = _user("o5"), _user("o6")
        HostedMt5Workspace.objects.create(trading_account=_account(u1))
        self.assertTrue(E.user_holds_workspace(u1))
        self.assertFalse(E.user_holds_workspace(u2))             # another user's workspace is never theirs

    def test_account_binding_is_immutable_after_creation(self):
        # The workspace<->account binding is the load-bearing guard now that ownership is derived from it:
        # once bound, re-pointing the workspace at a different account (a different owner) must fail closed.
        u1 = _user("o7")
        ws = HostedMt5Workspace.objects.create(trading_account=_account(u1))
        other = _account(_user("o8"), login="800800")
        ws.trading_account = other
        with self.assertRaises(ValueError):
            ws.save(update_fields=["trading_account"])
