"""ADR-0034 Execution Engine (G4) — the Hosted Workspace claim-seam entitlement, proven at the REAL
``/api/execution/jobs/next/`` endpoint (not just against ``authorize_hosted_claim`` in isolation).

Closes the scope-item-3 evidence gap: a hosted mutation job that is not owner-bound-armed-routed to a
node-aware worker is FAILED under the row lock and NEVER handed out (204). And the seam is byte-for-byte
DARK — with the subsystem OFF the very same hosted job claims exactly like a legacy job.
"""
from __future__ import annotations

import os
from unittest import mock

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
            broker_server=server, readiness_provider=PERSISTENT_WORKSPACE, terminal_node=node,
            workspace_confirmed_at=timezone.now())  # ADR-0034 Onboarding — armed ⇒ confirmed
        HostedMt5Workspace.objects.create(
            trading_account=acct, canonical_state=S.EXECUTION_READY, proj_connected=True,
            proj_trade_allowed=True, proj_account_match=True, proj_execution_ready=True,
            last_decision_at=timezone.now(), execution_enabled=True, execution_node=node,
            execution_authorized_at=timezone.now())  # ADR-0047: a ready workspace is customer-authorized
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
            broker_server=server, readiness_provider=PERSISTENT_WORKSPACE, terminal_node=node,
            workspace_confirmed_at=timezone.now())  # ADR-0034 Onboarding — armed ⇒ confirmed
        HostedMt5Workspace.objects.create(
            trading_account=acct, canonical_state=S.EXECUTION_READY, proj_connected=True,
            proj_trade_allowed=True, proj_account_match=True, proj_execution_ready=True,
            last_decision_at=timezone.now(), execution_enabled=True, execution_node=node,
            execution_authorized_at=timezone.now())  # ADR-0047: a ready workspace is customer-authorized
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
            broker_server=server, readiness_provider=PERSISTENT_WORKSPACE, terminal_node=node,
            workspace_confirmed_at=timezone.now())  # ADR-0034 Onboarding — armed ⇒ confirmed
        HostedMt5Workspace.objects.create(
            trading_account=acct, canonical_state=S.EXECUTION_READY, proj_connected=True,
            proj_trade_allowed=True, proj_account_match=True, proj_execution_ready=True,
            last_decision_at=timezone.now(), execution_enabled=True, execution_node=node,
            execution_authorized_at=timezone.now())  # ADR-0047: a ready workspace is customer-authorized
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

    def _complete(self, client, job_id, worker_id, secret, **body):
        return client.post(f"/api/execution/jobs/{job_id}/complete/", body, format="json",
                           HTTP_X_WORKER_ID=worker_id, HTTP_X_WORKER_SECRET=secret)

    def test_complete_endpoint_drives_finished_provenance_and_telemetry(self):
        # RULE-11 positive control for the COMPLETION half (symmetric with the STARTED control): the real
        # POST /complete/ drives record_hosted_completion → FINISHED provenance row + execution_finished event.
        from operational_events.models import OperationalEvent
        node, job = self._armed_hosted_job("node-comp", "700555")
        WorkerIdentity.objects.create(
            worker_id="cw", worker_secret_hash=WorkerIdentity.hash_secret("sc"),
            status=WorkerIdentity.Status.ACTIVE, worker_permissions={"authorized_nodes": ["node-comp"]})
        client = APIClient()
        with mock.patch.dict(os.environ, {"OPERATIONS_EVENTS_ENABLED": "1"}, clear=False):
            rc = client.get(NEXT + "?worker_id=cw&job_types=CLOSE_TRADE",
                            HTTP_X_WORKER_ID="cw", HTTP_X_WORKER_SECRET="sc")
            self.assertEqual(rc.status_code, 200, rc.content)      # claimed → RUNNING + STARTED
            rr = self._complete(client, job.id, "cw", "sc", status="SUCCESS", result={})
        self.assertEqual(rr.status_code, 200, rr.content)
        job.refresh_from_db()
        self.assertEqual(job.status, "SUCCESS")
        self.assertEqual(HostedWorkspaceExecution.objects.filter(job=job, phase="FINISHED").count(), 1)
        self.assertTrue(OperationalEvent.objects.filter(
            event_type="workspace.execution_finished").exists())

    def test_complete_endpoint_refuses_unentitled_worker(self):
        # Completion-half isolation: a worker NOT entitled to the job's node cannot complete a hosted job —
        # the job is left RUNNING and no FINISHED provenance is written (mirrors the claim seam).
        node, job = self._armed_hosted_job("node-x", "700666")
        WorkerIdentity.objects.create(
            worker_id="owner", worker_secret_hash=WorkerIdentity.hash_secret("so"),
            status=WorkerIdentity.Status.ACTIVE, worker_permissions={"authorized_nodes": ["node-x"]})
        WorkerIdentity.objects.create(
            worker_id="intruder", worker_secret_hash=WorkerIdentity.hash_secret("si"),
            status=WorkerIdentity.Status.ACTIVE, worker_permissions={})  # no authorized_nodes
        client = APIClient()
        rc = client.get(NEXT + "?worker_id=owner&job_types=CLOSE_TRADE",
                        HTTP_X_WORKER_ID="owner", HTTP_X_WORKER_SECRET="so")
        self.assertEqual(rc.status_code, 200, rc.content)          # entitled worker claims → RUNNING
        rr = self._complete(client, job.id, "intruder", "si", status="FAILED", error_message="x")
        self.assertEqual(rr.status_code, 403, rr.content)          # unentitled completer refused
        job.refresh_from_db()
        self.assertEqual(job.status, "RUNNING")                    # not mutated
        self.assertFalse(HostedWorkspaceExecution.objects.filter(job=job, phase="FINISHED").exists())

    def test_complete_succeeds_even_if_node_drains_after_claim(self):
        # Entitlement is worker MEMBERSHIP, not node liveness: a worker that legitimately claimed a job while
        # its node was ACTIVE must still be able to REPORT the outcome after the node enters maintenance —
        # otherwise the already-placed order's result is stranded. Node drains between claim and complete.
        node, job = self._armed_hosted_job("node-drain", "700777")
        WorkerIdentity.objects.create(
            worker_id="dw", worker_secret_hash=WorkerIdentity.hash_secret("sd"),
            status=WorkerIdentity.Status.ACTIVE, worker_permissions={"authorized_nodes": ["node-drain"]})
        client = APIClient()
        rc = client.get(NEXT + "?worker_id=dw&job_types=CLOSE_TRADE",
                        HTTP_X_WORKER_ID="dw", HTTP_X_WORKER_SECRET="sd")
        self.assertEqual(rc.status_code, 200, rc.content)          # claimed while ACTIVE → RUNNING
        node.status = TerminalNode.Status.DRAINING                 # operator drains the node mid-flight
        node.save(update_fields=["status"])
        rr = self._complete(client, job.id, "dw", "sd", status="SUCCESS", result={})
        self.assertEqual(rr.status_code, 200, rr.content)          # completion still entitled (membership)
        job.refresh_from_db()
        self.assertEqual(job.status, "SUCCESS")
        self.assertEqual(HostedWorkspaceExecution.objects.filter(job=job, phase="FINISHED").count(), 1)

    def test_complete_refuses_shared_legacy_identity_even_if_granted(self):
        # Completion-side mirror of the claim guard: even a mis-provisioned `legacy-worker` row carrying
        # authorized_nodes is forced non-node-aware, so the shared identity can never complete a hosted job.
        from execution.auth import LEGACY_WORKER_ID
        node, job = self._armed_hosted_job("node-lg", "700888")
        WorkerIdentity.objects.create(
            worker_id=LEGACY_WORKER_ID, worker_secret_hash=WorkerIdentity.hash_secret("sl"),
            status=WorkerIdentity.Status.ACTIVE, worker_permissions={"authorized_nodes": ["node-lg"]})
        WorkerIdentity.objects.create(
            worker_id="owner2", worker_secret_hash=WorkerIdentity.hash_secret("so2"),
            status=WorkerIdentity.Status.ACTIVE, worker_permissions={"authorized_nodes": ["node-lg"]})
        client = APIClient()
        rc = client.get(NEXT + "?worker_id=owner2&job_types=CLOSE_TRADE",
                        HTTP_X_WORKER_ID="owner2", HTTP_X_WORKER_SECRET="so2")
        self.assertEqual(rc.status_code, 200, rc.content)          # entitled worker claims → RUNNING
        rr = self._complete(client, job.id, LEGACY_WORKER_ID, "sl", status="SUCCESS", result={})
        self.assertEqual(rr.status_code, 403, rr.content)          # legacy identity forced non-node-aware
        job.refresh_from_db()
        self.assertEqual(job.status, "RUNNING")

    def test_complete_gate_survives_account_reclassification(self):
        # The gate keys off the DURABLE job stamp (hosted_workspace_uuid), so reclassifying the account's
        # readiness_provider mid-flight does NOT skip the entitlement check — an unentitled worker is still
        # refused (the provenance write it guards keys off the same durable stamp).
        node, job = self._armed_hosted_job("node-rc", "700999")
        WorkerIdentity.objects.create(
            worker_id="owner3", worker_secret_hash=WorkerIdentity.hash_secret("so3"),
            status=WorkerIdentity.Status.ACTIVE, worker_permissions={"authorized_nodes": ["node-rc"]})
        WorkerIdentity.objects.create(
            worker_id="intr2", worker_secret_hash=WorkerIdentity.hash_secret("si2"),
            status=WorkerIdentity.Status.ACTIVE, worker_permissions={})   # unentitled
        client = APIClient()
        rc = client.get(NEXT + "?worker_id=owner3&job_types=CLOSE_TRADE",
                        HTTP_X_WORKER_ID="owner3", HTTP_X_WORKER_SECRET="so3")
        self.assertEqual(rc.status_code, 200, rc.content)
        acct = job.account
        acct.readiness_provider = ""                               # reclassify OFF persistent_workspace
        acct.save(update_fields=["readiness_provider"])
        rr = self._complete(client, job.id, "intr2", "si2", status="SUCCESS", result={})
        self.assertEqual(rr.status_code, 403, rr.content)          # durable-keyed gate still enforced
        job.refresh_from_db()
        self.assertEqual(job.status, "RUNNING")

    def test_complete_fails_closed_when_node_deleted_to_null(self):
        # A SET_NULL node deletion mid-flight NULLs the RUNNING job's terminal_node while its durable
        # hosted_workspace_uuid survives. The gate keys off the uuid (still fires), and a NULL node returns
        # None from the node lookup → 403 (fail-closed, matching the claim seam) — so provenance is NOT forged
        # for an un-routable job. Even the previously-entitled worker is refused.
        node, job = self._armed_hosted_job("node-del", "701000")
        WorkerIdentity.objects.create(
            worker_id="ownerd", worker_secret_hash=WorkerIdentity.hash_secret("sod"),
            status=WorkerIdentity.Status.ACTIVE, worker_permissions={"authorized_nodes": ["node-del"]})
        client = APIClient()
        rc = client.get(NEXT + "?worker_id=ownerd&job_types=CLOSE_TRADE",
                        HTTP_X_WORKER_ID="ownerd", HTTP_X_WORKER_SECRET="sod")
        self.assertEqual(rc.status_code, 200, rc.content)          # claimed while node ACTIVE → RUNNING
        ExecutionJob.objects.filter(pk=job.pk).update(terminal_node=None)   # SET_NULL node deletion
        rr = self._complete(client, job.id, "ownerd", "sod", status="SUCCESS", result={})
        self.assertEqual(rr.status_code, 403, rr.content)          # un-routable NULL node → fail-closed
        job.refresh_from_db()
        self.assertEqual(job.status, "RUNNING")
        self.assertFalse(HostedWorkspaceExecution.objects.filter(job=job, phase="FINISHED").exists())
