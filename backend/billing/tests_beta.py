"""GFX-BETA-PHASE0 Increment 4 — beta entitlement (auto-granted, payment-bypassed), server-side
onboarding gate (default CLOSED), and entitlement-scoped marketplace foundation."""
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory, force_authenticate

from billing.models import UserSubscriptionState
from billing.entitlements import resolve_entitlements
from billing.beta import grant_beta_entitlement, beta_onboarding_open
from trading.models import TradingAccount
from strategies.models import Strategy, StrategyAssignment

U = get_user_model()


class BetaEntitlementTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="u", email="u@x.invalid", password="x")

    def test_grant_sets_beta_and_is_beta_entitlement(self):
        state = grant_beta_entitlement(self.user)
        self.assertEqual(state.current_plan, UserSubscriptionState.Plan.BETA)
        self.assertFalse(state.viewer_mode)
        ent = resolve_entitlements(state)
        self.assertTrue(ent.is_beta)
        self.assertEqual(ent.max_trading_accounts, 10)
        # CRITICAL: beta must NOT grant execution authorization in Phase 0 (fail-closed on placing
        # orders independent of provisioning).
        self.assertFalse(ent.can_deploy_automation)
        self.assertTrue(ent.can_assign_strategies)  # config setup still allowed

    def test_grant_does_not_clobber_paid_plan(self):
        UserSubscriptionState.objects.create(
            user=self.user, current_plan=UserSubscriptionState.Plan.PRO,
            plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False)
        state = grant_beta_entitlement(self.user)
        self.assertEqual(state.current_plan, UserSubscriptionState.Plan.PRO)  # untouched

    def test_grant_does_not_clobber_lapsed_paid_plan(self):
        # A lapsed paid plan has viewer_mode=True per the model invariant — it must still be preserved.
        UserSubscriptionState.objects.create(
            user=self.user, current_plan=UserSubscriptionState.Plan.PRO,
            plan_status=UserSubscriptionState.PlanStatus.EXPIRED, viewer_mode=True)
        state = grant_beta_entitlement(self.user)
        self.assertEqual(state.current_plan, UserSubscriptionState.Plan.PRO)  # not resurrected as beta

    def test_non_beta_entitlement_is_false(self):
        self.assertFalse(resolve_entitlements(None).is_beta)  # viewer defaults

    def test_registration_auto_grants_beta(self):
        from users.serializers import RegisterSerializer
        s = RegisterSerializer(data={"email": "n@x.invalid", "username": "n", "password": "Str0ngPass!"})
        s.is_valid(raise_exception=True)
        user = s.save()
        state = UserSubscriptionState.objects.get(user=user)
        self.assertEqual(state.current_plan, UserSubscriptionState.Plan.BETA)


class OnboardingGateTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="ob", email="ob@x.invalid", password="x")
        self.staff = U.objects.create_user(username="s", email="s@x.invalid", password="x", is_staff=True)
        self.acct = TradingAccount.objects.create(
            user=self.user, name="A", account_number="OB1", is_demo=True, is_active=True)

    def _prep(self, user):
        from onboarding import services
        state = services.get_or_create_onboarding_state(user)
        state.plan_selected = True
        state.email_verified = True
        state.risk_accepted = True
        state.save(update_fields=["plan_selected", "email_verified", "risk_accepted"])

    def test_gate_default_closed(self):
        self.assertFalse(beta_onboarding_open())

    def test_gate_blocks_account_connected_for_non_staff(self):
        from onboarding import services
        self._prep(self.user)
        with self.assertRaises(services.OnboardingStepError):
            services.mark_account_connected(self.user)  # gate closed → blocked

    @override_settings(BETA_ONBOARDING_ENABLED=True)
    def test_gate_open_allows_progression(self):
        from onboarding import services
        self._prep(self.user)
        state = services.mark_account_connected(self.user)  # gate open → proceeds
        self.assertTrue(state.account_connected)

    def test_staff_bypasses_closed_gate(self):
        from onboarding import services
        acct = TradingAccount.objects.create(
            user=self.staff, name="S", account_number="S1", is_demo=True, is_active=True)
        self._prep(self.staff)
        state = services.mark_account_connected(self.staff)  # staff bypass
        self.assertTrue(state.account_connected)


class BetaMarketplaceTests(TestCase):
    def setUp(self):
        from onboarding.views import BetaMarketplaceView
        self.View = BetaMarketplaceView
        self.beta = U.objects.create_user(username="b", email="b@x.invalid", password="x")
        grant_beta_entitlement(self.beta)
        self.viewer = U.objects.create_user(username="v", email="v@x.invalid", password="x")
        self.staff = U.objects.create_user(username="s", email="s@x.invalid", password="x", is_staff=True)
        self.factory = APIRequestFactory()

    def _get(self, user):
        req = self.factory.get("/x")
        force_authenticate(req, user=user)
        return self.View.as_view()(req)

    def test_beta_sees_two_wayond_strategies_not_activatable(self):
        r = self._get(self.beta)
        self.assertTrue(r.data["entitled"])
        self.assertFalse(r.data["onboarding_open"])
        keys = {s["key"] for s in r.data["strategies"]}
        self.assertEqual(keys, {"wayond_auto_demo", "wayond_wim"})
        for s in r.data["strategies"]:
            self.assertFalse(s["available"])          # truthful: not activatable
            self.assertFalse(s["provisioning_available"])
            self.assertIsNotNone(s["reason"])

    def test_non_beta_sees_empty(self):
        r = self._get(self.viewer)
        self.assertFalse(r.data["entitled"])
        self.assertEqual(r.data["strategies"], [])

    def test_staff_sees_strategies(self):
        r = self._get(self.staff)
        self.assertTrue(r.data["entitled"])
        self.assertEqual(len(r.data["strategies"]), 2)
