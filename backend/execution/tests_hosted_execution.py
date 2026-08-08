"""ADR-0034 Execution Engine (G12) — Hosted Workspace execution telemetry + persistence seam.

Proves the provenance/telemetry seam is: DARK (no-op + no stamping while OFF or for a non-hosted job);
stamps the workspace uuid at creation and the HWX key at dispatch; records an append-only STARTED/FINISHED
occupancy with the sanitised outcome; emits workspace.execution_started/finished; idempotent on replay; and
secret-free. It records/telemeters ONLY — it drives no order and never touches the M3c canonical_state.
"""
from __future__ import annotations

import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from hosted_workspace.models import HostedMt5Workspace
from operational_events.models import OperationalEvent
from trading.models import BrokerServer, TradingAccount

from execution import hosted_execution as HE
from execution.models import ExecutionJob, HostedWorkspaceExecution
from execution.readiness import PERSISTENT_WORKSPACE, TEMPORARY_VALIDATION

U = get_user_model()


def _ops_on():
    return mock.patch.dict(os.environ, {"OPERATIONS_EVENTS_ENABLED": "1"}, clear=False)


def _account(provider=PERSISTENT_WORKSPACE, *, login="700900", server_name="IS6-Demo", with_workspace=True):
    user = U.objects.create_user(username=f"u{login}", email=f"{login}@x.invalid", password="x")
    server, _ = BrokerServer.objects.get_or_create(server_name=server_name)
    acct = TradingAccount.objects.create(
        user=user, name="a", broker_name="B", account_number=login, is_demo=True,
        broker_server=server, readiness_provider=provider)
    ws = None
    if with_workspace:
        ws = HostedMt5Workspace.objects.create(trading_account=acct)
    return acct, ws


def _close_job(acct):
    # CLOSE_TRADE is in IDENTITY_PIN_JOB_TYPES (so inject runs) but is NOT exposure-opening, so it bypasses
    # the kill-switch / broker-validation creation gates — a clean fixture for the provenance seam.
    return ExecutionJob.objects.create(account=acct, job_type=ExecutionJob.JobType.CLOSE_TRADE,
                                       payload={"expected_login": "700900", "expected_server": "IS6-Demo"})


class DarkTests(TestCase):
    def test_dark_no_stamp_no_provenance_no_telemetry(self):
        acct, _ = _account()  # flag OFF (default)
        job = _close_job(acct)
        self.assertEqual(job.hosted_workspace_uuid, "")  # not stamped while dark
        with _ops_on():
            self.assertFalse(HE.record_hosted_dispatch(job))
            self.assertFalse(HE.record_hosted_completion(job))
        self.assertEqual(HostedWorkspaceExecution.objects.count(), 0)
        self.assertFalse(OperationalEvent.objects.filter(event_type__startswith="workspace.execution").exists())


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
class ProvenanceTelemetryTests(TestCase):
    def test_inject_stamps_workspace_uuid(self):
        acct, ws = _account()
        job = _close_job(acct)
        self.assertEqual(job.hosted_workspace_uuid, str(ws.workspace_uuid))

    def test_dispatch_records_started_and_emits(self):
        acct, ws = _account()
        job = _close_job(acct)
        with _ops_on():
            self.assertTrue(HE.record_hosted_dispatch(job, correlation_id="c1"))
        job.refresh_from_db()
        self.assertTrue(job.hosted_idempotency_key.startswith("HWX-"))
        row = HostedWorkspaceExecution.objects.get(job=job, phase="STARTED")
        self.assertEqual(row.workspace_uuid, str(ws.workspace_uuid))
        self.assertEqual(row.seq, 1)
        self.assertEqual(row.hosted_idempotency_key, job.hosted_idempotency_key)
        ev = OperationalEvent.objects.get(event_type="workspace.execution_started")
        self.assertEqual(ev.account_id, acct.id)

    def test_completion_records_finished_with_outcome_and_emits(self):
        acct, _ = _account()
        job = _close_job(acct)
        job.status = ExecutionJob.Status.SUCCESS
        job.save(update_fields=["status"])
        with _ops_on():
            self.assertTrue(HE.record_hosted_completion(job, correlation_id="c2"))
        row = HostedWorkspaceExecution.objects.get(job=job, phase="FINISHED")
        self.assertEqual(row.outcome, "SUCCESS")
        self.assertEqual(row.seq, 2)
        self.assertTrue(OperationalEvent.objects.filter(event_type="workspace.execution_finished").exists())

    def test_replay_is_idempotent_single_row_single_event(self):
        acct, _ = _account()
        job = _close_job(acct)
        with _ops_on():
            self.assertTrue(HE.record_hosted_dispatch(job))
            self.assertFalse(HE.record_hosted_dispatch(job))  # replay
        self.assertEqual(HostedWorkspaceExecution.objects.filter(job=job, phase="STARTED").count(), 1)
        self.assertEqual(OperationalEvent.objects.filter(event_type="workspace.execution_started").count(), 1)

    def test_non_hosted_account_is_noop(self):
        acct, _ = _account(provider=TEMPORARY_VALIDATION, login="500500", with_workspace=False)
        job = _close_job(acct)
        self.assertEqual(job.hosted_workspace_uuid, "")
        with _ops_on():
            self.assertFalse(HE.record_hosted_dispatch(job))
        self.assertEqual(HostedWorkspaceExecution.objects.count(), 0)

    def test_hwx_key_deterministic_and_stable(self):
        acct, _ = _account()
        job = _close_job(acct)
        k1 = HE.stamp_hosted_idempotency_key(job)
        k2 = HE.stamp_hosted_idempotency_key(job)  # never recomputes over an existing key
        self.assertTrue(k1.startswith("HWX-"))
        self.assertEqual(k1, k2)

    def test_telemetry_is_secret_free(self):
        acct, _ = _account()
        job = _close_job(acct)
        with _ops_on():
            HE.record_hosted_dispatch(job)
        ev = OperationalEvent.objects.get(event_type="workspace.execution_started")
        blob = f"{ev.summary} {ev.metadata} {ev.reason_code} {ev.status}".lower()
        for banned in ("password", "secret", "accounts.dat", "token"):
            self.assertNotIn(banned, blob)

    def test_provenance_queryable_by_workspace(self):
        acct, ws = _account()
        job = _close_job(acct)
        with _ops_on():
            HE.record_hosted_dispatch(job)
        self.assertEqual(
            HostedWorkspaceExecution.objects.filter(workspace_uuid=str(ws.workspace_uuid)).count(), 1)


class AppendOnlyTests(TestCase):
    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_update_and_delete_refused(self):
        acct, ws = _account()
        job = _close_job(acct)
        row = HostedWorkspaceExecution.objects.create(
            job=job, workspace_uuid=str(ws.workspace_uuid), phase="STARTED", seq=1)
        with self.assertRaises(ValueError):
            row.save(update_fields=["outcome"])
        with self.assertRaises(ValueError):
            row.delete()


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
class ReconcileTests(TestCase):
    def _reconcile(self, job, **ev):
        from execution.hosted_reconcile import AmbiguousEvidence, reconcile_hosted_execution
        base = dict(order_found=False, position_found=False, deal_found=False,
                    reconciliation_authoritative=False)
        base.update(ev)
        return reconcile_hosted_execution(job, AmbiguousEvidence(**base))

    def test_confirmed_executed_no_retry_no_alert(self):
        acct, _ = _account()
        job = _close_job(acct)
        with _ops_on():
            res = self._reconcile(job, order_found=True, reconciliation_authoritative=True)
        self.assertEqual(res.classification, "confirmed_executed")
        self.assertFalse(res.retry_permitted)
        self.assertFalse(res.quarantined)
        self.assertFalse(res.alerted)
        row = HostedWorkspaceExecution.objects.get(job=job, phase="RECONCILED")
        # The full classification is persisted UNTRUNCATED (the 18/22-char confirmed_* codes must survive).
        self.assertEqual(row.outcome, "confirmed_executed")

    def test_reconciled_outcome_persisted_untruncated_for_all_classes(self):
        """Every classifier code round-trips through the persisted outcome field without truncation."""
        for ev, expected in (
                (dict(order_found=True, reconciliation_authoritative=True), "confirmed_executed"),
                (dict(reconciliation_authoritative=True), "confirmed_not_executed"),
                (dict(), "still_ambiguous")):
            acct, _ = _account(login=f"7009{len(expected)}")
            job = _close_job(acct)
            with _ops_on():
                self._reconcile(job, **ev)
            row = HostedWorkspaceExecution.objects.get(job=job, phase="RECONCILED")
            self.assertEqual(row.outcome, expected)

    def test_confirmed_not_executed_permits_retry_advisory_only(self):
        acct, _ = _account()
        job = _close_job(acct)
        with _ops_on():
            res = self._reconcile(job, reconciliation_authoritative=True)  # authoritative, no evidence
        self.assertEqual(res.classification, "confirmed_not_executed")
        self.assertTrue(res.retry_permitted)   # advisory — NOT auto-acted
        self.assertFalse(res.quarantined)

    def test_still_ambiguous_quarantines_and_alerts(self):
        acct, ws = _account()
        job = _close_job(acct)
        with _ops_on():
            res = self._reconcile(job)  # not authoritative, no evidence
        self.assertEqual(res.classification, "still_ambiguous")
        self.assertTrue(res.quarantined)
        self.assertTrue(res.alerted)
        self.assertTrue(OperationalEvent.objects.filter(
            event_type="workspace.execution_ambiguous", severity="WARNING").exists())

    def test_dark_reconcile_is_noop(self):
        acct, _ = _account()  # flag OFF via no override on this method
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED=False):
            job = _close_job(acct)
            res = self._reconcile(job)
        self.assertFalse(res.recorded)
        self.assertEqual(HostedWorkspaceExecution.objects.count(), 0)

    def test_reconcile_never_creates_a_job_no_auto_resend(self):
        """Item 9 stance: reconciliation NEVER auto-resends. Even CONFIRMED_NOT_EXECUTED (retry-permitted)
        creates NO new ExecutionJob — a retry is only ever a human-gated action."""
        acct, _ = _account()
        job = _close_job(acct)
        before = ExecutionJob.objects.count()
        with _ops_on():
            self._reconcile(job, reconciliation_authoritative=True)
        self.assertEqual(ExecutionJob.objects.count(), before)  # no order re-sent


class RetryStanceTests(TestCase):
    def test_may_retry_has_no_auto_resend_consumer(self):
        """Guard: no production code turns ``may_retry_after_ambiguous`` into a job-creation/re-send. The
        only references are the pure predicate, its tests, and the (human-gated) reconcile advisory."""
        import subprocess
        out = subprocess.run(
            ["grep", "-rln", "--include=*.py", "may_retry_after_ambiguous",
             "execution", "hosted_workspace", "scripts"],
            capture_output=True, text=True).stdout.split()
        allowed = {
            "execution/hosted_idempotency.py",       # the predicate itself
            "execution/hosted_reconcile.py",          # advisory-only consumer (never re-sends)
            "execution/tests_hosted_g2g10.py",        # tests
            "execution/tests_hosted_execution.py",    # tests
        }
        unexpected = [p for p in out if p not in allowed]
        self.assertEqual(unexpected, [], f"unexpected may_retry consumer(s): {unexpected}")


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
class FailSafeTests(TestCase):
    """Failure-matrix row 'completion callback / telemetry / DB failure -> fail-safe': the provenance/telemetry
    seam is called post-commit from the hot claim/complete paths, so a raising recorder or DB write MUST be
    swallowed (return False) and NEVER propagate — otherwise an already-committed RUNNING/completed job would
    500 the worker. These prove the documented fail-safe instead of trusting the try/except by inspection."""

    def test_dispatch_swallows_raising_telemetry(self):
        acct, _ = _account()
        job = _close_job(acct)
        with _ops_on(), mock.patch("execution.hosted_execution.record_event",
                                   side_effect=RuntimeError("boom")):
            self.assertFalse(HE.record_hosted_dispatch(job))   # returns False, does NOT raise
        # the STARTED provenance row was still written (get_or_create runs before the emit)
        self.assertTrue(HostedWorkspaceExecution.objects.filter(job=job, phase="STARTED").exists())

    def test_dispatch_swallows_raising_db_write(self):
        acct, _ = _account()
        job = _close_job(acct)
        with _ops_on(), mock.patch(
                "execution.models.HostedWorkspaceExecution.objects.get_or_create",
                side_effect=RuntimeError("db boom")):
            self.assertFalse(HE.record_hosted_dispatch(job))   # swallowed, no propagation

    def test_completion_swallows_raising_telemetry(self):
        acct, _ = _account()
        job = _close_job(acct)
        job.status = ExecutionJob.Status.SUCCESS
        job.save(update_fields=["status"])
        with _ops_on(), mock.patch("execution.hosted_execution.record_event",
                                   side_effect=RuntimeError("boom")):
            self.assertFalse(HE.record_hosted_completion(job))

    def test_stamp_key_swallows_raising_save(self):
        acct, _ = _account()
        job = _close_job(acct)
        with mock.patch.object(job, "save", side_effect=RuntimeError("save boom")):
            key = HE.stamp_hosted_idempotency_key(job)         # computes key, save fails, no propagation
        self.assertTrue(key.startswith("HWX-"))
