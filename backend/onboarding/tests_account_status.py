"""GFX-BETA-PHASE0 Increment 3 — Account Status panel: truthful states; never implies a terminal exists
while provisioning is undeployed; account-owner scoped."""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory, force_authenticate

from trading.models import TradingAccount
from strategies.models import Strategy, StrategyAssignment
from execution.models import ExecutionJob
from terminal_provisioning.runtime_state import get_or_create_runtime, record_transition
from terminal_provisioning.models import RuntimeState
from onboarding.views import AccountStatusView

U = get_user_model()


class AccountStatusTests(TestCase):
    def setUp(self):
        self.owner = U.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.other = U.objects.create_user(username="p", email="p@x.invalid", password="x")
        self.staff = U.objects.create_user(username="s", email="s@x.invalid", password="x", is_staff=True)
        self.acct = TradingAccount.objects.create(
            user=self.owner, name="A", account_number="A1", is_demo=True)
        self.factory = APIRequestFactory()

    def _get(self, user, account_id=None):
        q = f"?account_id={account_id}" if account_id else ""
        req = self.factory.get("/x" + q)
        force_authenticate(req, user=user)
        return AccountStatusView.as_view()(req)

    def _stage(self, data, key):
        return next(s for s in data["stages"] if s["key"] == key)

    def test_fresh_account_never_implies_terminal(self):
        r = self._get(self.owner, self.acct.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._stage(r.data, "mt5_runtime")["state"], "NOT_CONFIGURED")
        self.assertEqual(self._stage(r.data, "hosted_terminal")["state"], "NOT_CONFIGURED")
        self.assertEqual(r.data["overall"], "NOT_CONFIGURED")
        self.assertFalse(r.data["terminal_provisioning_available"])
        self.assertEqual(self._stage(r.data, "account_created")["state"], "HEALTHY")

    def test_strategy_and_execution_still_not_running_without_runtime(self):
        # Even with an enabled strategy + executions, without a provisioned runtime the panel must NOT
        # claim a hosted terminal is available; overall stays NOT_CONFIGURED (truthful).
        strat = Strategy.objects.create(owner=self.owner, name="WIM")
        StrategyAssignment.objects.create(
            strategy=strat, account=self.acct, execution_mode="AUTO_DEMO", is_active=True)
        ExecutionJob.objects.create(job_type="PLACE_ORDER", account=self.acct, status="SUCCESS")
        r = self._get(self.owner, self.acct.id)
        self.assertEqual(self._stage(r.data, "strategy_assigned")["state"], "HEALTHY")
        self.assertEqual(self._stage(r.data, "strategy_enabled")["state"], "HEALTHY")
        self.assertEqual(self._stage(r.data, "last_execution")["state"], "HEALTHY")
        self.assertEqual(self._stage(r.data, "hosted_terminal")["state"], "NOT_CONFIGURED")  # key truth
        self.assertEqual(r.data["overall"], "NOT_CONFIGURED")

    def test_failed_runtime_surfaces_failed(self):
        rt = get_or_create_runtime(self.acct)
        record_transition(rt, RuntimeState.FAILED, reason_code="provision_terminal_error")
        r = self._get(self.owner, self.acct.id)
        self.assertEqual(self._stage(r.data, "mt5_runtime")["state"], "FAILED")
        self.assertEqual(self._stage(r.data, "mt5_runtime")["detail"], "provision_terminal_error")
        self.assertEqual(r.data["overall"], "FAILED")

    def test_running_runtime_shows_running_terminal(self):
        rt = get_or_create_runtime(self.acct)
        record_transition(rt, RuntimeState.RUNNING)
        strat = Strategy.objects.create(owner=self.owner, name="WIM")
        StrategyAssignment.objects.create(
            strategy=strat, account=self.acct, execution_mode="AUTO_DEMO", is_active=True)
        r = self._get(self.owner, self.acct.id)
        self.assertEqual(self._stage(r.data, "mt5_runtime")["state"], "RUNNING")
        self.assertEqual(self._stage(r.data, "hosted_terminal")["state"], "RUNNING")
        self.assertEqual(r.data["overall"], "HEALTHY")

    def test_owner_scoping(self):
        self.assertEqual(self._get(self.other, self.acct.id).status_code, 404)
        self.assertEqual(self._get(self.staff, self.acct.id).status_code, 200)

    def test_default_to_own_primary_account(self):
        r = self._get(self.owner)  # no account_id → the caller's primary
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["account_id"], self.acct.id)

    def test_non_numeric_account_id_is_404_not_500(self):
        req = self.factory.get("/x?account_id=abc")
        force_authenticate(req, user=self.owner)
        self.assertEqual(AccountStatusView.as_view()(req).status_code, 404)

    def test_failed_last_execution_is_not_green(self):
        ExecutionJob.objects.create(job_type="PLACE_ORDER", account=self.acct, status="FAILED")
        r = self._get(self.owner, self.acct.id)
        self.assertEqual(self._stage(r.data, "last_execution")["state"], "WARNING")  # not HEALTHY
        self.assertEqual(r.data["overall"], "NOT_CONFIGURED")  # not over-escalated to FAILED
