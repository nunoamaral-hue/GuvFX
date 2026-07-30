"""CZFS (Customer Zero Flow Simplification, Option 2) — onboarding covers only the minimum account setup
(email + plan + risk); broker connection and strategy assignment are POST-onboarding PLATFORM setup, not
prerequisites for ``onboarding_completed``. ``resolve_setup_stage`` is the intelligent resume router."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from billing.beta import grant_beta_entitlement
from onboarding import services
from onboarding.views import OnboardingCompleteView, SetupStatusView
from strategies.models import Strategy, StrategyAssignment
from terminal_provisioning.models import AccountRuntime, RuntimeState
from trading.models import TradingAccount

U = get_user_model()


def _prep_min(user, *, risk=True):
    """Give the user the minimum onboarding prerequisites (email + plan [+ risk])."""
    st = services.get_or_create_onboarding_state(user)
    st.email_verified = True
    st.plan_selected = True
    st.risk_accepted = risk
    st.save()
    return st


def _acct(user):
    return TradingAccount.objects.create(
        user=user, name="A", account_number="1302575", broker_name="IS6Technologies-Demo",
        is_demo=True, is_active=False)


class OnboardingCompletionTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="n", email="n@x.invalid", password="x")

    def test_required_steps_exclude_broker_and_strategy(self):
        self.assertNotIn("account_connected", services.REQUIRED_STEPS)
        self.assertNotIn("strategy_assigned", services.REQUIRED_STEPS)
        self.assertEqual(services.REQUIRED_STEPS, {"email_verified", "risk_accepted", "plan_selected"})

    def test_completes_with_only_email_plan_risk(self):
        _prep_min(self.user)
        st = services.finalize_onboarding(self.user)
        self.assertTrue(st.onboarding_completed)
        # broker/strategy NOT required and NOT set
        self.assertFalse(st.account_connected)
        self.assertFalse(st.strategy_assigned)

    def test_does_not_complete_without_risk(self):
        _prep_min(self.user, risk=False)
        st = services.finalize_onboarding(self.user)
        self.assertFalse(st.onboarding_completed)

    def test_finalize_is_idempotent(self):
        _prep_min(self.user)
        first = services.finalize_onboarding(self.user)
        second = services.finalize_onboarding(self.user)
        self.assertTrue(first.onboarding_completed)
        self.assertTrue(second.onboarding_completed)
        self.assertEqual(first.onboarding_completed_at, second.onboarding_completed_at)  # not re-stamped


class SetupStageLadderTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="s", email="s@x.invalid", password="x")
        grant_beta_entitlement(self.user)

    def _stage(self):
        return services.resolve_setup_stage(self.user)["stage"]

    def test_onboarding_incomplete_routes_to_onboarding(self):
        r = services.resolve_setup_stage(self.user)
        self.assertEqual(r["stage"], "onboarding")
        self.assertEqual(r["next_route"], "/onboarding")

    def test_connect_broker_when_completed_but_no_account(self):
        _prep_min(self.user); services.finalize_onboarding(self.user)
        r = services.resolve_setup_stage(self.user)
        self.assertEqual(r["stage"], "connect_broker")
        self.assertEqual(r["next_route"], "/accounts")

    def test_provisioning_when_account_but_runtime_not_ready(self):
        _prep_min(self.user); services.finalize_onboarding(self.user)
        _acct(self.user)   # no runtime yet
        self.assertEqual(self._stage(), "provisioning")

    def test_select_strategy_when_runtime_running_no_strategy(self):
        _prep_min(self.user); services.finalize_onboarding(self.user)
        acct = _acct(self.user)
        AccountRuntime.objects.create(
            trading_account=acct, cohort=AccountRuntime.Cohort.BETA, state=RuntimeState.RUNNING)
        r = services.resolve_setup_stage(self.user)
        self.assertEqual(r["stage"], "select_strategy")
        self.assertEqual(r["next_route"], "/strategies/marketplace")

    def test_enable_trading_then_complete(self):
        # Uses the REAL assignment shapes: marketplace_assign creates is_active=True, stage=TEST (selected,
        # NOT trading); arming sets AUTO_DEMO + stage=LIVE (the authoritative armed signal).
        _prep_min(self.user); services.finalize_onboarding(self.user)
        acct = _acct(self.user)
        AccountRuntime.objects.create(
            trading_account=acct, cohort=AccountRuntime.Cohort.BETA, state=RuntimeState.RUNNING)
        strat = Strategy.objects.create(owner=self.user, name="WIM")
        asn = StrategyAssignment.objects.create(
            strategy=strat, account=acct, signal_source="ti_signals",
            is_active=True, stage=StrategyAssignment.STAGE_TEST)   # selected (marketplace shape)
        self.assertEqual(self._stage(), "enable_trading")   # is_active alone must NOT read as complete
        asn.execution_mode = StrategyAssignment.ExecutionMode.AUTO_DEMO
        asn.stage = StrategyAssignment.STAGE_LIVE
        asn.save(update_fields=["execution_mode", "stage"])   # armed
        r = services.resolve_setup_stage(self.user)
        self.assertEqual(r["stage"], "complete")
        self.assertEqual(r["next_route"], "/dashboard")


class SetupEndpointTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="e", email="e@x.invalid", password="x")
        self.factory = APIRequestFactory()

    def test_complete_endpoint_finalizes_and_returns_setup(self):
        _prep_min(self.user)
        req = self.factory.post("/api/onboarding/complete/")
        force_authenticate(req, user=self.user)
        resp = OnboardingCompleteView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["onboarding_completed"])
        self.assertEqual(resp.data["setup"]["stage"], "connect_broker")   # completed → connect broker next

    def test_setup_status_endpoint(self):
        req = self.factory.get("/api/onboarding/setup-status/")
        force_authenticate(req, user=self.user)
        resp = SetupStatusView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["stage"], "onboarding")   # nothing done yet

    def test_complete_endpoint_with_existing_account_is_provisioning(self):
        # The exact Customer Zero handoff: email+plan+risk done AND an un-provisioned account already
        # exists (like account #12). Finalizing must complete onboarding and route to 'provisioning'
        # (/accounts), never 'connect_broker' — CZ already has the account.
        _prep_min(self.user)
        TradingAccount.objects.create(
            user=self.user, name="A", account_number="1302575", broker_name="IS6Technologies-Demo",
            is_demo=True, is_active=False)
        req = self.factory.post("/api/onboarding/complete/")
        force_authenticate(req, user=self.user)
        resp = OnboardingCompleteView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["onboarding_completed"])
        self.assertEqual(resp.data["setup"]["stage"], "provisioning")
        self.assertEqual(resp.data["setup"]["next_route"], "/accounts")


class DurableSetupStateTests(TestCase):
    """Broker/strategy are removed from REQUIRED_STEPS but their milestone marks must still function as
    durable POST-onboarding setup state (the reviewer's explicit ask)."""
    def setUp(self):
        self.user = U.objects.create_user(username="d", email="d@x.invalid", password="x")
        grant_beta_entitlement(self.user)

    def test_account_connected_is_durable_state_separate_from_completion(self):
        _prep_min(self.user)
        services.finalize_onboarding(self.user)   # onboarding completes WITHOUT a broker
        st = services.get_or_create_onboarding_state(self.user)
        self.assertTrue(st.onboarding_completed)
        self.assertFalse(st.account_connected)     # durable setup milestone, independent of completion
        # mark_account_connected is UNCHANGED by CZFS: it still governs the account_connected milestone
        # via its own runtime-ready prerequisite (raises a structured reason when the owned runtime is not
        # ready), independently of onboarding_completed — proving it still functions as durable setup state.
        with self.assertRaises(services.OnboardingStepError):
            services.mark_account_connected(self.user)   # no broker account/runtime → structured refusal
        st.refresh_from_db()
        self.assertFalse(st.account_connected)     # not falsely flipped
        self.assertTrue(st.onboarding_completed)   # completion untouched
