"""ADR-0034 Onboarding — foundation: DARK admission predicate + derived-ownership invariant.

Proves: the onboarding flag defaults OFF; admission is a fail-closed AND of (master flag ∧ onboarding flag ∧
the durable ``can_use_hosted_workspace`` capability) with reachable most-specific reason codes; and that
ownership is the SINGLE immutable fact ``trading_account.user`` (there is no separate ``owner`` FK to
diverge — the workspace<->account binding is immutable, so the owner cannot drift). No order is placed and
nothing is armed.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from billing.models import BetaTester, UserSubscriptionState
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


def _std_user(name, *, plan="standard"):
    """A user on a NON-hosted COMMERCIAL plan (default 'standard'). Distinct from ``_user(entitled=True)``,
    which puts the user on the 'beta' plan (the commercial hosted-entitlement source)."""
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.update_or_create(
        user=u, defaults=dict(current_plan=plan, plan_status="active", viewer_mode=False))
    return u


def _admit(user, *, active=True):
    """Enrol *user* into the Hosted Beta programme (the ``BetaTester`` admission allowlist)."""
    return BetaTester.objects.create(email=user.email, is_active=active)


class HostedCapabilityDecouplingTests(TestCase):
    """ADR-0034 amendment — Hosted Workspace CAPABILITY is INDEPENDENT of the commercial subscription: a
    fail-closed OR of (commercial entitlement) OR (active Hosted Beta programme membership). A tester keeps
    their commercial plan and still gains hosted access; paid users NOT in the programme are never broadened in."""

    # --- capability predicate (both sources, fail-closed) ---
    def test_capability_commercial_source(self):
        self.assertTrue(E.has_hosted_workspace_capability(_user(entitled=True)))          # beta plan

    def test_capability_beta_programme_source_keeps_commercial_plan(self):
        u = _std_user("cap_std"); _admit(u)
        self.assertTrue(E.has_hosted_workspace_capability(u))
        self.assertEqual(UserSubscriptionState.objects.get(user=u).current_plan, "standard")  # plan UNTOUCHED

    def test_capability_neither_source_false(self):
        self.assertFalse(E.has_hosted_workspace_capability(_std_user("cap_none")))

    def test_capability_inactive_betatester_false(self):
        u = _std_user("cap_inact"); _admit(u, active=False)      # inactive allowlist row => not admitted
        self.assertFalse(E.has_hosted_workspace_capability(u))

    def test_capability_no_user_false(self):
        self.assertFalse(E.has_hosted_workspace_capability(None))

    # --- admission composes flags AND capability ---
    @override_settings(**_FLAGS_ON)
    def test_admission_via_beta_programme_on_standard_plan(self):
        # The exact certification-identity shape: standard commercial plan + active Hosted Beta membership.
        u = _std_user("adm_std"); _admit(u)
        ok, reason = E.hosted_workspace_admission(u)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, E.ADMIT_OK)

    @override_settings(**_FLAGS_ON)
    def test_admission_commercial_source_without_beta_membership(self):
        ok, reason = E.hosted_workspace_admission(_user(name="adm_beta", entitled=True))  # beta plan, no allowlist row
        self.assertTrue(ok, reason)
        self.assertEqual(reason, E.ADMIT_OK)

    @override_settings(**_FLAGS_ON)
    def test_admission_denied_paid_user_not_in_programme(self):
        ok, reason = E.hosted_workspace_admission(_std_user("adm_paid", plan="advanced"))
        self.assertFalse(ok)
        self.assertEqual(reason, E.DENY_NOT_ENTITLED)

    @override_settings(**_FLAGS_ON)
    def test_beta_membership_does_not_bypass_dark_flags(self):
        # Fail-closed: capability alone never admits — with onboarding OFF a Hosted Beta member is still denied.
        with override_settings(HOSTED_WORKSPACE_ONBOARDING_ENABLED="0"):
            u = _std_user("adm_dark"); _admit(u)
            ok, reason = E.hosted_workspace_admission(u)
            self.assertFalse(ok)
            self.assertEqual(reason, E.DENY_ONBOARDING_DARK)

    # --- eligibility projection honours the same OR (admission and eligibility stay consistent) ---
    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1")
    def test_eligibility_entitled_via_beta_programme(self):
        from hosted_workspace.eligibility import CHECK_ENTITLED, strategy_assignment_eligibility
        u = _std_user("elig_bt"); _admit(u)
        proj = strategy_assignment_eligibility(_account(u), user=u)
        entitled = next(c["ok"] for c in proj["checklist"] if c["key"] == CHECK_ENTITLED)
        self.assertTrue(entitled)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1")
    def test_eligibility_not_entitled_paid_user_not_in_programme(self):
        # Negative lock on the SAME predicate the amendment rewired into eligibility: a paid user who is NOT in
        # the Hosted Beta programme must have CHECK_ENTITLED False (this fails if eligibility hardcodes True).
        from hosted_workspace.eligibility import CHECK_ENTITLED, strategy_assignment_eligibility
        u = _std_user("elig_paid", plan="advanced")     # paid, NOT admitted to the programme
        proj = strategy_assignment_eligibility(_account(u), user=u)
        entitled = next(c["ok"] for c in proj["checklist"] if c["key"] == CHECK_ENTITLED)
        self.assertFalse(entitled)


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
