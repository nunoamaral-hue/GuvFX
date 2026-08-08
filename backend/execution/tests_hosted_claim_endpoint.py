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

from django.utils import timezone

from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from trading.models import BrokerServer, TradingAccount

from execution.models import ExecutionJob, HostedWorkspaceExecution, TerminalNode, WorkerIdentity
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


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True, HOSTED_MT5_EXECUTION_ENABLED=True)
class HostedClaimEndpointPositiveControlTests(TestCase):
    """RULE 11 positive control: the ``/api/execution/jobs/next/`` measurement path is proven to SERVE — not
    only refuse. A fully provisioned + armed + node-bound hosted job, polled by a NODE-AWARE worker (modern
    X-Worker-Id/Secret → ``authorized_nodes`` populated), is handed out (200/RUNNING) AND records the G12
    STARTED provenance row. Without this, a regression that silently refused every correct hosted claim would
    stay green until the live human demo order — exactly the failure RULE 11 exists to prevent."""

    def test_provisioned_armed_node_bound_job_served_to_node_aware_worker(self):
        user = User.objects.create_user(username="pc", email="pc@x.invalid", password="x")
        server, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
        node = TerminalNode.objects.create(hostname="node-pos")   # defaults status=ACTIVE
        acct = TradingAccount.objects.create(
            user=user, name="A", account_number="700111", is_demo=True, broker_name="DemoBroker",
            broker_server=server, readiness_provider=PERSISTENT_WORKSPACE, terminal_node=node)
        HostedMt5Workspace.objects.create(
            trading_account=acct, canonical_state=S.EXECUTION_READY, proj_connected=True,
            proj_trade_allowed=True, proj_account_match=True, proj_execution_ready=True,
            last_decision_at=timezone.now(), execution_enabled=True, execution_node=node)
        WorkerIdentity.objects.create(
            worker_id="nodeworker", worker_secret_hash=WorkerIdentity.hash_secret("s1"),
            status=WorkerIdentity.Status.ACTIVE, worker_permissions={"authorized_nodes": ["node-pos"]})
        job = ExecutionJob.objects.create(
            account=acct, job_type=ExecutionJob.JobType.CLOSE_TRADE, status="PENDING", payload={},
            terminal_node=node)

        r = APIClient().get(NEXT + "?worker_id=nodeworker&job_types=CLOSE_TRADE",
                            HTTP_X_WORKER_ID="nodeworker", HTTP_X_WORKER_SECRET="s1")

        self.assertEqual(r.status_code, 200, r.content)      # SERVED, not refused
        self.assertEqual(r.json()["id"], job.id)
        job.refresh_from_db()
        self.assertEqual(job.status, "RUNNING")
        # G12 provenance: the dispatch hook appended exactly one STARTED occupancy row for this job.
        started = HostedWorkspaceExecution.objects.filter(job=job, phase="STARTED")
        self.assertEqual(started.count(), 1)

    def test_non_node_aware_worker_cannot_claim_node_bound_hosted_job(self):
        # Companion negative: the SAME correctly-provisioned job, polled by a worker with NO authorized_nodes
        # (empty perms ⇒ routing_mode legacy_null_node), is NOT served — the node-aware requirement is real,
        # not incidental. The non-node-aware branch filters to NULL-node jobs, so a node-bound hosted job is
        # simply never in its claimable set (filtered out, so left PENDING — not FAILED).
        user = User.objects.create_user(username="nn", email="nn@x.invalid", password="x")
        server, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
        node = TerminalNode.objects.create(hostname="node-nn")
        acct = TradingAccount.objects.create(
            user=user, name="A", account_number="700222", is_demo=True, broker_name="DemoBroker",
            broker_server=server, readiness_provider=PERSISTENT_WORKSPACE, terminal_node=node)
        HostedMt5Workspace.objects.create(
            trading_account=acct, canonical_state=S.EXECUTION_READY, proj_connected=True,
            proj_trade_allowed=True, proj_account_match=True, proj_execution_ready=True,
            last_decision_at=timezone.now(), execution_enabled=True, execution_node=node)
        WorkerIdentity.objects.create(
            worker_id="plainworker", worker_secret_hash=WorkerIdentity.hash_secret("s2"),
            status=WorkerIdentity.Status.ACTIVE, worker_permissions={})  # no authorized_nodes
        job = ExecutionJob.objects.create(
            account=acct, job_type=ExecutionJob.JobType.CLOSE_TRADE, status="PENDING", payload={},
            terminal_node=node)
        r = APIClient().get(NEXT + "?worker_id=plainworker&job_types=CLOSE_TRADE",
                            HTTP_X_WORKER_ID="plainworker", HTTP_X_WORKER_SECRET="s2")
        self.assertEqual(r.status_code, 204)     # never handed out to a non-node-aware worker
        job.refresh_from_db()
        self.assertEqual(job.status, "PENDING")  # filtered out of the claimable set, not FAILED

    def _armed_hosted_job(self, node_hostname, login):
        user = User.objects.create_user(username=f"u{login}", email=f"{login}@x.invalid", password="x")
        server, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
        node = TerminalNode.objects.create(hostname=node_hostname)
        acct = TradingAccount.objects.create(
            user=user, name="A", account_number=login, is_demo=True, broker_name="DemoBroker",
            broker_server=server, readiness_provider=PERSISTENT_WORKSPACE, terminal_node=node)
        HostedMt5Workspace.objects.create(
            trading_account=acct, canonical_state=S.EXECUTION_READY, proj_connected=True,
            proj_trade_allowed=True, proj_account_match=True, proj_execution_ready=True,
            last_decision_at=timezone.now(), execution_enabled=True, execution_node=node)
        job = ExecutionJob.objects.create(
            account=acct, job_type=ExecutionJob.JobType.CLOSE_TRADE, status="PENDING", payload={},
            terminal_node=node)
        return node, job

    def test_node_aware_worker_cannot_claim_another_nodes_hosted_job(self):
        # Decision C ("one authorised worker"): a worker authorised for node-A must NOT receive a
        # correctly-provisioned hosted job bound to node-B — per-worker node scoping, not merely
        # node-aware-vs-legacy. The job is on node-B; the worker's authorized_nodes=['node-A'].
        TerminalNode.objects.create(hostname="node-A")   # the worker's (other) node
        _node_b, job = self._armed_hosted_job("node-B", "700333")
        WorkerIdentity.objects.create(
            worker_id="worker-A", worker_secret_hash=WorkerIdentity.hash_secret("sA"),
            status=WorkerIdentity.Status.ACTIVE, worker_permissions={"authorized_nodes": ["node-A"]})
        r = APIClient().get(NEXT + "?worker_id=worker-A&job_types=CLOSE_TRADE",
                            HTTP_X_WORKER_ID="worker-A", HTTP_X_WORKER_SECRET="sA")
        self.assertEqual(r.status_code, 204)     # node-B job not in node-A worker's claimable set
        job.refresh_from_db()
        self.assertEqual(job.status, "PENDING")

    def test_shared_legacy_identity_is_never_node_aware_even_if_granted(self):
        # Defence-in-depth: even a mis-provisioned `legacy-worker` row carrying authorized_nodes is forced
        # non-node-aware at the claim path, so the shared identity can never claim a hosted node-bound job.
        from execution.auth import LEGACY_WORKER_ID
        node, job = self._armed_hosted_job("node-L", "700444")
        WorkerIdentity.objects.create(
            worker_id=LEGACY_WORKER_ID, worker_secret_hash=WorkerIdentity.hash_secret("sL"),
            status=WorkerIdentity.Status.ACTIVE, worker_permissions={"authorized_nodes": ["node-L"]})
        r = APIClient().get(NEXT + f"?worker_id={LEGACY_WORKER_ID}&job_types=CLOSE_TRADE",
                            HTTP_X_WORKER_ID=LEGACY_WORKER_ID, HTTP_X_WORKER_SECRET="sL")
        self.assertEqual(r.status_code, 204)     # legacy identity forced non-node-aware → node-L job excluded
        job.refresh_from_db()
        self.assertEqual(job.status, "PENDING")
