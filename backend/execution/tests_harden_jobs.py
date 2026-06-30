"""
EXEC-HARDEN-JOBS tests — lock down generic ExecutionJob creation.

Proves: (1) the generic CRUD write surface on ExecutionJobViewSet is disabled,
so an ordinary authenticated user cannot POST/PUT/PATCH/DELETE an order-bearing
job directly; (2) order-defining fields are read-only on the serializer; (3) the
functional kill switch blocks order-opening job creation at the model layer
(covering every creation path) while leaving non-order jobs and the kill-off
case working; (4) the OpenTradeJobView fails closed with 503; and (5) E1a
proposals still create no ExecutionJob (regression).
"""

import os

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from execution import signal_proposals as bridge
from execution.models import (
    ExecutionControl,
    ExecutionJob,
    ExecutionKillSwitchEngaged,
    ProposedSignalOrder,
    order_creation_kill_reason,
)
from execution.serializers import ExecutionJobSerializer
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount

User = get_user_model()


def _approved(message_id, symbol="EURUSD", direction="BUY"):
    return PendingSignalApproval.objects.create(
        source=PendingSignalApproval.Source.WAYOND_TELEGRAM,
        message_id=message_id, symbol=symbol, direction=direction,
        entry="1.0850", stop_loss="1.0800", take_profit="1.0900",
        status=PendingSignalApproval.Status.APPROVED,
    )


class GenericCreateDisabledTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="u", email="u@example.invalid", password="x"
        )
        self.account = TradingAccount.objects.create(
            user=self.user, name="A", account_number="N1", is_demo=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_post_place_order_blocked(self):
        before = ExecutionJob.objects.count()
        resp = self.client.post(
            reverse("execution-job-list"),
            {"job_type": "PLACE_ORDER", "account": self.account.id,
             "payload": {"symbol": "EURUSD"}}, format="json",
        )
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(ExecutionJob.objects.count(), before)

    def test_post_open_trade_blocked(self):
        resp = self.client.post(
            reverse("execution-job-list"),
            {"job_type": "OPEN_TRADE", "account": self.account.id}, format="json",
        )
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_post_any_type_blocked(self):
        # Generic create is disabled entirely, not just for order types.
        resp = self.client.post(
            reverse("execution-job-list"),
            {"job_type": "SYNC_POSITIONS", "account": self.account.id}, format="json",
        )
        self.assertEqual(resp.status_code, 405)

    def test_update_and_delete_blocked(self):
        job = ExecutionJob.objects.create(
            account=self.account, job_type="SYNC_POSITIONS", payload={}
        )
        url = reverse("execution-job-detail", args=[job.id])
        self.assertEqual(
            self.client.patch(url, {"job_type": "PLACE_ORDER"}, format="json").status_code, 405
        )
        self.assertEqual(
            self.client.put(url, {"job_type": "PLACE_ORDER"}, format="json").status_code, 405
        )
        self.assertEqual(self.client.delete(url).status_code, 405)
        job.refresh_from_db()
        self.assertEqual(job.job_type, "SYNC_POSITIONS")  # unchanged

    def test_order_defining_fields_are_read_only(self):
        fields = ExecutionJobSerializer().fields
        for name in ("job_type", "account", "payload", "strategy", "assignment",
                     "terminal_node", "status"):
            self.assertTrue(fields[name].read_only, f"{name} must be read-only")


class KillSwitchModelGuardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="k", email="k@example.invalid", password="x"
        )
        self.account = TradingAccount.objects.create(
            user=self.user, name="A", account_number="N2", is_demo=True
        )

    def _make(self, job_type):
        return ExecutionJob.objects.create(
            account=self.account, job_type=job_type, payload={"symbol": "EURUSD"}
        )

    def test_kill_switch_blocks_order_opening_jobs(self):
        bridge.engage_kill_switch(actor=self.user, reason="test")
        for jt in ("OPEN_TRADE", "PLACE_ORDER", "PLACE_TEST_ORDER"):
            before = ExecutionJob.objects.count()
            with self.assertRaises(ExecutionKillSwitchEngaged):
                self._make(jt)
            self.assertEqual(ExecutionJob.objects.count(), before)  # nothing created

    def test_env_kill_switch_blocks_order_job(self):
        os.environ["GUVFX_EXECUTION_DISABLED"] = "true"
        try:
            with self.assertRaises(ExecutionKillSwitchEngaged):
                self._make("OPEN_TRADE")
        finally:
            del os.environ["GUVFX_EXECUTION_DISABLED"]

    def test_kill_switch_allows_non_order_jobs(self):
        bridge.engage_kill_switch(actor=self.user)
        job = self._make("SYNC_POSITIONS")  # not exposure-opening — must succeed
        self.assertIsNotNone(job.id)

    def test_kill_off_allows_order_job(self):
        self.assertIsNone(order_creation_kill_reason())
        job = self._make("OPEN_TRADE")  # sanctioned path still works when off
        self.assertIsNotNone(job.id)

    def test_reading_kill_reason_creates_no_control_row(self):
        # order_creation_kill_reason must be read-only (no get_or_create).
        ExecutionControl.objects.all().delete()
        self.assertIsNone(order_creation_kill_reason())
        self.assertEqual(ExecutionControl.objects.count(), 0)


class OpenTradeEndpointKillSwitchTests(TestCase):
    def test_open_trade_endpoint_503_when_kill_engaged(self):
        user = User.objects.create_user(
            username="o", email="o@example.invalid", password="x"
        )
        bridge.engage_kill_switch(actor=user, reason="drill")
        client = APIClient()
        client.force_authenticate(user=user)
        # Kill switch is checked first (before entitlement/serializer), so an
        # empty body still yields 503 — fail closed for everyone.
        resp = client.post("/api/execution/open-trade/", {}, format="json")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.data.get("reason"), "kill_switch_engaged")


class ProposalNoOrderRegressionTests(TestCase):
    """E1a guarantee must survive the hardening: proposals create no jobs."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="p", email="p@example.invalid", password="x"
        )
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="N3", is_demo=True
        )

    def test_proposal_creates_no_execution_job(self):
        before = ExecutionJob.objects.count()
        proposal = bridge.propose_order_from_approval(
            _approved("h1"), account=self.demo, actor=self.user
        )
        self.assertIsInstance(proposal, ProposedSignalOrder)
        self.assertEqual(ExecutionJob.objects.count(), before)
        self.assertEqual(ExecutionJob.objects.count(), 0)

    def test_kill_switch_blocks_proposal_and_creates_no_job(self):
        bridge.engage_kill_switch(actor=self.user)
        with self.assertRaises(bridge.ProposalRejected) as ctx:
            bridge.propose_order_from_approval(_approved("h2"), account=self.demo)
        self.assertEqual(ctx.exception.code, "kill_switch_engaged")
        self.assertEqual(ExecutionJob.objects.count(), 0)
        self.assertEqual(ProposedSignalOrder.objects.count(), 0)
