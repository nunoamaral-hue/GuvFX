"""ADR-0034 Execution Engine (G4) — the Hosted Workspace claim-seam entitlement, proven at the REAL
``/api/execution/jobs/next/`` endpoint (not just against ``authorize_hosted_claim`` in isolation).

Closes the scope-item-3 evidence gap: a hosted mutation job that is not owner-bound-armed-routed to a
node-aware worker is FAILED under the row lock and NEVER handed out (204). And the seam is byte-for-byte
DARK — with the subsystem OFF the very same hosted job claims exactly like a legacy job.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from hosted_workspace.models import HostedMt5Workspace
from trading.models import BrokerServer, TradingAccount

from execution.models import ExecutionJob, WorkerIdentity
from execution.readiness import PERSISTENT_WORKSPACE

User = get_user_model()
NEXT = "/api/execution/jobs/next/"


class HostedClaimEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="hc", email="hc@x.invalid", password="x")
        server, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
        # Provider-B hosted account + workspace, deliberately NOT armed (execution_enabled defaults False).
        self.acct = TradingAccount.objects.create(
            user=self.user, name="A", account_number="700111", is_demo=True, broker_name="DemoBroker",
            broker_server=server, readiness_provider=PERSISTENT_WORKSPACE)
        HostedMt5Workspace.objects.create(trading_account=self.acct)
        WorkerIdentity.objects.create(
            worker_id="hcw", worker_secret_hash=WorkerIdentity.hash_secret("s1"),
            status=WorkerIdentity.Status.ACTIVE)

    def _hosted_close_job(self):
        # CLOSE_TRADE is a mutation job (IDENTITY_PIN_JOB_TYPES → the hosted claim seam applies) but is not
        # exposure-opening, so it bypasses the creation gates — a clean fixture for the claim seam.
        return ExecutionJob.objects.create(
            account=self.acct, job_type=ExecutionJob.JobType.CLOSE_TRADE, status="PENDING", payload={})

    def _claim(self):
        return APIClient().get(NEXT + "?worker_id=hcw&job_types=CLOSE_TRADE",
                               HTTP_X_WORKER_ID="hcw", HTTP_X_WORKER_SECRET="s1")

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_hosted_job_refused_under_lock_when_not_armed_routed(self):
        job = self._hosted_close_job()
        r = self._claim()
        self.assertEqual(r.status_code, 204)          # nothing handed out
        job.refresh_from_db()
        self.assertEqual(job.status, "FAILED")        # FAILED under the row lock
        self.assertIn("hosted claim entitlement refused", job.error_message)

    def test_dark_subsystem_claims_hosted_job_like_legacy(self):
        # Flag OFF (default): the hosted claim seam is byte-for-byte dark — the same job claims normally.
        job = self._hosted_close_job()
        r = self._claim()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], job.id)
        job.refresh_from_db()
        self.assertEqual(job.status, "RUNNING")
