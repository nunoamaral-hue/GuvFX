"""Tests for the signal-copy enable/disable endpoints (StrategyViewSet.signal_copy_toggle /
signal_copy_status) used by the "Wayond WIM Strategy" marketplace card.

Key safety property: the toggle PAUSES/RESUMES an already-armed AUTO_DEMO assignment only.
It NEVER creates an assignment and NEVER changes execution_mode/stage — so "enable" can never
grant new trading authority (arming stays a separate, human-gated step).
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from strategies.models import Strategy, StrategyAssignment
from trading.models import TradingAccount

User = get_user_model()
AM = StrategyAssignment.ExecutionMode
STATUS_URL = "/api/strategies/strategies/signal-copy/status/"
TOGGLE_URL = "/api/strategies/strategies/signal-copy/toggle/"
WIM = "mp-010"


class SignalCopyToggleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u", email="u@x.invalid", password="x")
        self.other = User.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True,
        )
        self.strategy = Strategy.objects.create(owner=self.user, name="Wayond WIM Strategy")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _arm(self, *, active=True, source="ti_signals", strategy=None):
        """Simulate the separate, gated arming step: an AUTO_DEMO assignment bound to source."""
        return StrategyAssignment.objects.create(
            strategy=strategy or self.strategy, account=self.demo, execution_mode=AM.AUTO_DEMO,
            signal_source=source, is_active=active, stage=StrategyAssignment.STAGE_LIVE,
        )

    def test_status_reports_not_armed_before_arming(self):
        r = self.client.get(STATUS_URL, {"marketplace_strategy_id": WIM})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["armed"])
        self.assertFalse(r.data["enabled"])
        self.assertEqual(r.data["signal_source"], "ti_signals")

    def test_toggle_before_arming_is_conflict_and_creates_nothing(self):
        r = self.client.post(TOGGLE_URL, {"marketplace_strategy_id": WIM, "enabled": True}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["status"], "not_armed")
        # Nothing was created — the endpoint can never arm.
        self.assertEqual(StrategyAssignment.objects.count(), 0)

    def test_disable_then_enable_flips_is_active_only(self):
        asn = self._arm(active=True)
        # Disable
        r = self.client.post(TOGGLE_URL, {"marketplace_strategy_id": WIM, "enabled": False}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], "disabled")
        asn.refresh_from_db()
        self.assertFalse(asn.is_active)
        # execution_mode + stage are untouched (still armed, just paused).
        self.assertEqual(asn.execution_mode, AM.AUTO_DEMO)
        self.assertEqual(asn.stage, StrategyAssignment.STAGE_LIVE)
        # Re-enable
        r = self.client.post(TOGGLE_URL, {"marketplace_strategy_id": WIM, "enabled": True}, format="json")
        self.assertEqual(r.data["status"], "enabled")
        asn.refresh_from_db()
        self.assertTrue(asn.is_active)

    def test_status_reflects_enabled_state(self):
        self._arm(active=True)
        r = self.client.get(STATUS_URL, {"marketplace_strategy_id": WIM})
        self.assertTrue(r.data["armed"])
        self.assertTrue(r.data["enabled"])

    def test_missing_enabled_boolean_is_400(self):
        self._arm()
        r = self.client.post(TOGGLE_URL, {"marketplace_strategy_id": WIM}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_non_signal_copy_template_is_rejected(self):
        # mp-005 is a scheduler strategy with no signal_source binding.
        r = self.client.post(TOGGLE_URL, {"marketplace_strategy_id": "mp-005", "enabled": True}, format="json")
        self.assertEqual(r.status_code, 400)
        r2 = self.client.get(STATUS_URL, {"marketplace_strategy_id": "mp-005"})
        self.assertEqual(r2.status_code, 400)

    def test_unknown_template_is_rejected(self):
        r = self.client.post(TOGGLE_URL, {"marketplace_strategy_id": "mp-999", "enabled": True}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_other_user_cannot_toggle_and_sees_not_armed(self):
        self._arm(active=True)  # owned by self.user
        client = APIClient()
        client.force_authenticate(self.other)
        r = client.post(TOGGLE_URL, {"marketplace_strategy_id": WIM, "enabled": False}, format="json")
        self.assertEqual(r.status_code, 409)  # not visible to a non-owner → not_armed
        self.assertEqual(StrategyAssignment.objects.get().is_active, True)  # untouched

    def test_enable_refuses_ambiguous_config(self):
        self._arm(active=False)
        strat2 = Strategy.objects.create(owner=self.user, name="Wayond WIM Strategy 2")
        self._arm(active=False, strategy=strat2)  # second ti_signals-bound AUTO_DEMO
        r = self.client.post(TOGGLE_URL, {"marketplace_strategy_id": WIM, "enabled": True}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["status"], "ambiguous")

    def test_disable_is_a_reliable_kill_even_with_a_leftover_row(self):
        # A leftover second armed row must NOT jam the safety stop: disable pauses EVERY active
        # arm bound to the source (adversarial-review finding: jammed disable).
        self._arm(active=True)
        strat2 = Strategy.objects.create(owner=self.user, name="Wayond WIM Strategy 2")
        self._arm(active=True, strategy=strat2)
        r = self.client.post(TOGGLE_URL, {"marketplace_strategy_id": WIM, "enabled": False}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], "disabled")
        self.assertEqual(r.data["paused_count"], 2)
        self.assertEqual(StrategyAssignment.objects.filter(is_active=True).count(), 0)

    def test_stage_test_arm_is_not_visible(self):
        # An AUTO_DEMO arm left at stage=TEST is not routable → status/toggle treat it as not-armed
        # (mirrors the router, no false "enabled").
        StrategyAssignment.objects.create(
            strategy=self.strategy, account=self.demo, execution_mode=AM.AUTO_DEMO,
            signal_source="ti_signals", is_active=True, stage=StrategyAssignment.STAGE_TEST,
        )
        s = self.client.get(STATUS_URL, {"marketplace_strategy_id": WIM})
        self.assertFalse(s.data["armed"])
        r = self.client.post(TOGGLE_URL, {"marketplace_strategy_id": WIM, "enabled": True}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["status"], "not_armed")

    def test_inactive_account_arm_is_not_visible(self):
        self._arm(active=True)
        self.demo.is_active = False
        self.demo.save(update_fields=["is_active"])
        s = self.client.get(STATUS_URL, {"marketplace_strategy_id": WIM})
        self.assertFalse(s.data["armed"])

    def test_strategy_owner_who_does_not_own_account_cannot_toggle(self):
        # Arm owned-strategy on ANOTHER user's demo account (staff-created cross-owned pair).
        demo_b = TradingAccount.objects.create(
            user=self.other, name="DemoB", account_number="DB1", is_demo=True,
        )
        asn = StrategyAssignment.objects.create(
            strategy=self.strategy, account=demo_b, execution_mode=AM.AUTO_DEMO,
            signal_source="ti_signals", is_active=True, stage=StrategyAssignment.STAGE_LIVE,
        )
        # self.user owns the strategy but NOT the account → cannot control it.
        r = self.client.post(TOGGLE_URL, {"marketplace_strategy_id": WIM, "enabled": False}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["status"], "not_armed")
        asn.refresh_from_db()
        self.assertTrue(asn.is_active)  # untouched

    def test_marketplace_assign_rejects_signal_copy_template(self):
        from billing.models import UserSubscriptionState
        UserSubscriptionState.objects.create(
            user=self.user, current_plan="starter_trial",
            plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False,
        )
        r = self.client.post(
            "/api/strategies/strategies/marketplace/assign/",
            {"marketplace_strategy_id": WIM, "account_id": self.demo.id}, format="json",
        )
        self.assertEqual(r.status_code, 400)
        # No Strategy/assignment created via the generic Assign path.
        self.assertEqual(StrategyAssignment.objects.count(), 0)
