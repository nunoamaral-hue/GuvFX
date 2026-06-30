"""
EXEC-HARDEN-JOBS-R2 tests — worker-action gating + clean kill-switch handling.

Proves: (1) ordinary authenticated users cannot claim (next) or complete jobs —
only validated workers (or staff) can; (2) legitimate worker-token auth still
works; (3) an engaged kill switch yields a clean 503 (not a 500) on the admin
retry path and creates no order-bearing job; (4) the E1a proposal no-order
guarantee is intact.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from execution import signal_proposals as bridge
from execution.models import (
    ExecutionControl,
    ExecutionJob,
    ProposedSignalOrder,
    WorkerIdentity,
)
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount

User = get_user_model()

NEXT_URL = "/api/execution/jobs/next/"


class WorkerActionGatingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="ordinary", email="ord@example.invalid", password="x"
        )
        self.account = TradingAccount.objects.create(
            user=self.user, name="A", account_number="N1", is_demo=True
        )
        # A claimable job so next_job has something to consider.
        self.job = ExecutionJob.objects.create(
            account=self.account, job_type="SYNC_POSITIONS",
            status=ExecutionJob.Status.PENDING, payload={},
        )

    # ----- ordinary users are blocked -----------------------------------

    def test_ordinary_user_cannot_claim(self):
        c = APIClient()
        c.force_authenticate(user=self.user)
        self.assertEqual(c.get(NEXT_URL).status_code, 403)

    def test_ordinary_user_cannot_complete(self):
        c = APIClient()
        c.force_authenticate(user=self.user)
        resp = c.post(f"/api/execution/jobs/{self.job.id}/complete/",
                      {"status": "SUCCESS"}, format="json")
        self.assertEqual(resp.status_code, 403)

    # ----- workers (and staff) are permitted ----------------------------

    def test_worker_token_can_claim(self):
        WorkerIdentity.objects.create(
            worker_id="w1", worker_secret_hash=WorkerIdentity.hash_secret("s1"),
            status=WorkerIdentity.Status.ACTIVE,
        )
        c = APIClient()  # no user — worker-header auth path
        resp = c.get(NEXT_URL, HTTP_X_WORKER_ID="w1", HTTP_X_WORKER_SECRET="s1")
        self.assertNotIn(resp.status_code, (401, 403))  # claimed (200) or no_jobs (204)

    def test_worker_token_can_complete(self):
        WorkerIdentity.objects.create(
            worker_id="w2", worker_secret_hash=WorkerIdentity.hash_secret("s2"),
            status=WorkerIdentity.Status.ACTIVE,
        )
        c = APIClient()
        resp = c.post(
            f"/api/execution/jobs/{self.job.id}/complete/", {"status": "SUCCESS"},
            format="json", HTTP_X_WORKER_ID="w2", HTTP_X_WORKER_SECRET="s2",
        )
        # Past the permission gate (worker accepted); not a 401/403 rejection.
        self.assertNotIn(resp.status_code, (401, 403))

    def test_staff_can_claim(self):
        staff = User.objects.create_user(
            username="staff", email="staff@example.invalid", password="x", is_staff=True
        )
        c = APIClient()
        c.force_authenticate(user=staff)
        self.assertNotIn(c.get(NEXT_URL).status_code, (401, 403))

    def test_invalid_worker_secret_rejected(self):
        WorkerIdentity.objects.create(
            worker_id="w3", worker_secret_hash=WorkerIdentity.hash_secret("right"),
            status=WorkerIdentity.Status.ACTIVE,
        )
        c = APIClient()
        resp = c.get(NEXT_URL, HTTP_X_WORKER_ID="w3", HTTP_X_WORKER_SECRET="wrong")
        self.assertIn(resp.status_code, (401, 403))


class KillSwitchCleanHandlingTests(TestCase):
    """An engaged kill switch must fail closed CLEANLY (503), never 500, and
    create no order-bearing job — on the admin retry path."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="su", email="su@example.invalid", password="x", is_superuser=True
        )
        self.account = TradingAccount.objects.create(
            user=self.admin, name="A", account_number="N2", is_demo=True
        )
        # A FAILED order job to retry (created with the switch OFF).
        self.failed = ExecutionJob.objects.create(
            account=self.account, job_type="PLACE_ORDER",
            status=ExecutionJob.Status.FAILED, payload={"symbol": "EURUSD"},
        )

    def test_admin_retry_returns_503_and_creates_no_job_when_kill_engaged(self):
        bridge.engage_kill_switch(actor=self.admin, reason="drill")
        before = ExecutionJob.objects.count()
        c = APIClient()
        c.force_authenticate(user=self.admin)
        resp = c.post(f"/api/admin/execution/jobs/{self.failed.id}/retry/", {}, format="json")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(ExecutionJob.objects.count(), before)  # no new (order) job

    def test_admin_retry_works_when_kill_off(self):
        # Regression: retry still works normally when the switch is off.
        c = APIClient()
        c.force_authenticate(user=self.admin)
        resp = c.post(f"/api/admin/execution/jobs/{self.failed.id}/retry/", {}, format="json")
        self.assertIn(resp.status_code, (200, 201))
        self.assertEqual(
            ExecutionJob.objects.filter(job_type="PLACE_ORDER", status="PENDING").count(), 1
        )


class ProposalRegressionTests(TestCase):
    def test_proposal_still_creates_no_execution_job(self):
        user = User.objects.create_user(
            username="p", email="p@example.invalid", password="x"
        )
        demo = TradingAccount.objects.create(
            user=user, name="Demo", account_number="N3", is_demo=True
        )
        approval = PendingSignalApproval.objects.create(
            source=PendingSignalApproval.Source.WAYOND_TELEGRAM, message_id="r2",
            symbol="EURUSD", direction="BUY", entry="1.0850", stop_loss="1.0800",
            take_profit="1.0900", status=PendingSignalApproval.Status.APPROVED,
        )
        before = ExecutionJob.objects.count()
        proposal = bridge.propose_order_from_approval(approval, account=demo, actor=user)
        self.assertIsInstance(proposal, ProposedSignalOrder)
        self.assertEqual(ExecutionJob.objects.count(), before)
        self.assertEqual(ExecutionJob.objects.count(), 0)
