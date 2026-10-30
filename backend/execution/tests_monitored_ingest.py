"""Tests for execution.monitored_ingest — the periodic read-only deal-history trigger.

Covers: the monitored-account set (connected + READY per-tenant endpoint only), idempotent
dedup against an in-flight sync, fail-closed on a missing Windows identity, the failed-probe
re-enqueue backoff, and the trading-boundary invariant (only SYNC_POSITIONS is ever created —
never an order job). The enqueued job is read-only; nothing here places, modifies or closes a trade.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution.models import ExecutionJob, HostedExecutionEndpoint, TerminalNode
from execution.monitored_ingest import enqueue_monitored_deal_syncs, monitored_account_ids
from execution.readiness import PERSISTENT_WORKSPACE
from hosted_workspace.models import HostedMt5Workspace
from terminal_provisioning.models import AccountProvisioning
from trading.models import TradingAccount

User = get_user_model()


class Base(TestCase):
    def setUp(self):
        self.node = TerminalNode.objects.create(hostname="mon-node", rdp_host="10.0.0.1")
        self._port = 8800

    def _tenant(self, login, *, state=HostedExecutionEndpoint.State.READY, base_url=None,
                disconnected=False, provisioned=True):
        port = self._port
        self._port += 1
        user = User.objects.create_user(username=f"m-{login}", email=f"m-{login}@x.invalid", password="x")
        acct = TradingAccount.objects.create(
            user=user, name="a", broker_name="B", account_number=str(login), is_demo=False,
            is_active=False, readiness_provider=PERSISTENT_WORKSPACE, terminal_node=self.node,
            disconnected_at=(timezone.now() if disconnected else None))
        ws = HostedMt5Workspace.objects.create(
            trading_account=acct, execution_node=self.node, workspace_node=self.node)
        if provisioned:
            AccountProvisioning.objects.create(
                trading_account=acct, windows_username=f"guvfx_u_{login}",
                runtime_root=fr"C:\GuvFX\accounts\{acct.id}",
                status=AccountProvisioning.Status.PROVISIONED)
        HostedExecutionEndpoint.objects.create(
            workspace=ws, trading_account=acct, terminal_node=self.node, host="10.0.0.1", port=port,
            base_url=(base_url if base_url is not None else f"http://10.0.0.1:{port}"),
            windows_username=f"guvfx_u_{login}", runtime_path="x", workspace_uuid=ws.workspace_uuid,
            state=state)
        return acct


class MonitoredSetTests(Base):
    def test_only_connected_ready_endpoints_are_monitored(self):
        ready = self._tenant("100")
        self._tenant("101", state=HostedExecutionEndpoint.State.ALLOCATED)   # not READY
        self._tenant("102", disconnected=True)                               # disconnected
        self._tenant("103", base_url="")                                     # READY but no base_url
        self.assertEqual(monitored_account_ids(), [ready.id])


class EnqueueTests(Base):
    def test_creates_one_readonly_sync_with_monitored_payload(self):
        acct = self._tenant("200")
        summary = enqueue_monitored_deal_syncs(account_ids=[acct.id])
        self.assertEqual(summary["queued"], 1)

        jobs = ExecutionJob.objects.filter(account_id=acct.id)
        self.assertEqual(jobs.count(), 1)
        job = jobs.first()
        self.assertEqual(job.job_type, ExecutionJob.JobType.SYNC_POSITIONS)
        self.assertEqual(job.status, ExecutionJob.Status.PENDING)
        self.assertTrue(job.payload.get("monitored_ingest"))
        self.assertEqual(job.payload.get("windows_username"), "guvfx_u_200")
        self.assertEqual(job.terminal_node_id, self.node.id)

    def test_trading_boundary_never_creates_an_order_job(self):
        acct = self._tenant("201")
        enqueue_monitored_deal_syncs(account_ids=[acct.id])
        # The ONLY job type this path may ever create is the read-only SYNC_POSITIONS.
        self.assertFalse(
            ExecutionJob.objects.exclude(job_type=ExecutionJob.JobType.SYNC_POSITIONS).exists())

    def test_idempotent_skips_when_sync_in_flight(self):
        acct = self._tenant("202")
        ExecutionJob.objects.create(
            account_id=acct.id, terminal_node_id=self.node.id,
            job_type=ExecutionJob.JobType.SYNC_POSITIONS, status=ExecutionJob.Status.PENDING,
            payload={"windows_username": "guvfx_u_202"})
        summary = enqueue_monitored_deal_syncs(account_ids=[acct.id])
        self.assertEqual(summary["queued"], 0)
        self.assertEqual(summary["skipped_in_flight"], 1)
        self.assertEqual(ExecutionJob.objects.filter(account_id=acct.id).count(), 1)

    def test_fail_closed_when_no_windows_identity(self):
        # No provisioning and no mt5_instance -> _sync_windows_username resolves None -> skip, never guess.
        acct = self._tenant("203", provisioned=False)
        summary = enqueue_monitored_deal_syncs(account_ids=[acct.id])
        self.assertEqual(summary["queued"], 0)
        self.assertEqual(summary["skipped_no_identity"], 1)
        self.assertEqual(ExecutionJob.objects.filter(account_id=acct.id).count(), 0)

    def test_cooldown_skips_recent_failed_monitored_probe(self):
        acct = self._tenant("204")
        ExecutionJob.objects.create(
            account_id=acct.id, terminal_node_id=self.node.id,
            job_type=ExecutionJob.JobType.SYNC_POSITIONS, status=ExecutionJob.Status.FAILED,
            payload={"windows_username": "guvfx_u_204", "monitored_ingest": True})
        # Within cooldown -> skipped.
        summary = enqueue_monitored_deal_syncs(account_ids=[acct.id], failure_cooldown_minutes=180)
        self.assertEqual(summary["queued"], 0)
        self.assertEqual(summary["skipped_cooldown"], 1)
        self.assertFalse(
            ExecutionJob.objects.filter(account_id=acct.id, status=ExecutionJob.Status.PENDING).exists())
        # Cooldown disabled -> enqueues.
        summary = enqueue_monitored_deal_syncs(account_ids=[acct.id], failure_cooldown_minutes=0)
        self.assertEqual(summary["queued"], 1)

    def test_cooldown_ignores_old_failures_and_non_monitored_failures(self):
        # (a) An OLD monitored failure (outside the window) must not suppress a fresh sync.
        a = self._tenant("205")
        old = ExecutionJob.objects.create(
            account_id=a.id, terminal_node_id=self.node.id,
            job_type=ExecutionJob.JobType.SYNC_POSITIONS, status=ExecutionJob.Status.FAILED,
            payload={"windows_username": "guvfx_u_205", "monitored_ingest": True})
        ExecutionJob.objects.filter(id=old.id).update(created_at=timezone.now() - timedelta(minutes=200))
        self.assertEqual(enqueue_monitored_deal_syncs(account_ids=[a.id], failure_cooldown_minutes=180)["queued"], 1)

        # (b) A recent FAILED sync that is NOT a monitored probe must not trigger the backoff.
        b = self._tenant("206")
        ExecutionJob.objects.create(
            account_id=b.id, terminal_node_id=self.node.id,
            job_type=ExecutionJob.JobType.SYNC_POSITIONS, status=ExecutionJob.Status.FAILED,
            payload={"windows_username": "guvfx_u_206"})  # no monitored_ingest flag
        self.assertEqual(enqueue_monitored_deal_syncs(account_ids=[b.id], failure_cooldown_minutes=180)["queued"], 1)

    def test_default_set_used_when_account_ids_omitted(self):
        ready = self._tenant("207")
        with mock.patch("execution.monitored_ingest.monitored_account_ids", return_value=[ready.id]) as m:
            summary = enqueue_monitored_deal_syncs()
        m.assert_called_once()
        self.assertEqual(summary["considered"], 1)
        self.assertEqual(summary["queued"], 1)

    def _sync(self, acct, status, *, minutes_ago):
        j = ExecutionJob.objects.create(
            account_id=acct.id, terminal_node_id=self.node.id,
            job_type=ExecutionJob.JobType.SYNC_POSITIONS, status=status,
            payload={"windows_username": f"guvfx_u_{acct.account_number}", "monitored_ingest": True})
        ExecutionJob.objects.filter(id=j.id).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))
        return j

    def test_cooldown_clears_after_a_successful_recovery(self):
        # ADR-0067: a recent FAILED probe would back off, but a SUBSEQUENT successful sync (recovered bridge) is the
        # latest → backoff clears immediately; the account resumes scheduled coverage without waiting the full window.
        acct = self._tenant("208")
        self._sync(acct, ExecutionJob.Status.FAILED, minutes_ago=30)     # failed...
        self._sync(acct, ExecutionJob.Status.SUCCESS, minutes_ago=5)     # ...then recovered (latest)
        summary = enqueue_monitored_deal_syncs(account_ids=[acct.id], failure_cooldown_minutes=180)
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["skipped_cooldown"], 0)

    def test_cooldown_still_backs_off_when_latest_is_failed(self):
        acct = self._tenant("209")
        self._sync(acct, ExecutionJob.Status.SUCCESS, minutes_ago=30)    # older success...
        self._sync(acct, ExecutionJob.Status.FAILED, minutes_ago=5)      # ...latest is a fresh failure → back off
        summary = enqueue_monitored_deal_syncs(account_ids=[acct.id], failure_cooldown_minutes=180)
        self.assertEqual(summary["queued"], 0)
        self.assertEqual(summary["skipped_cooldown"], 1)

    def test_cooldown_recovery_is_account_scoped(self):
        recovered = self._tenant("210")      # latest = SUCCESS → queued
        self._sync(recovered, ExecutionJob.Status.FAILED, minutes_ago=30)
        self._sync(recovered, ExecutionJob.Status.SUCCESS, minutes_ago=5)
        stuck = self._tenant("211")          # latest = FAILED → still backed off
        self._sync(stuck, ExecutionJob.Status.FAILED, minutes_ago=5)
        summary = enqueue_monitored_deal_syncs(account_ids=[recovered.id, stuck.id], failure_cooldown_minutes=180)
        self.assertEqual(summary["queued"], 1)            # only the recovered one
        self.assertEqual(summary["skipped_cooldown"], 1)  # the stuck one isolated
        self.assertTrue(ExecutionJob.objects.filter(account_id=recovered.id, status=ExecutionJob.Status.PENDING).exists())
        self.assertFalse(ExecutionJob.objects.filter(account_id=stuck.id, status=ExecutionJob.Status.PENDING).exists())
