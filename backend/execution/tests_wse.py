"""WP1B/WP2 Workstream E (ADR-0029) — execution-safety closure behaviour tests.

Covers the WSE fixes: the server-side final-dispatch gate at the next_job CLAIM boundary (the central
alternate-transport bypass closure); demo test-order + h4 scheduler refusal parity; and the promotion
pause pre-check. Everything is behind BROKER_CONNECTIVITY_EXECUTION_GATE (default OFF) — OFF is transparent.
"""
import inspect
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import AuditEvent
from execution.models import ExecutionJob, WorkerIdentity
from trading.models import TradingAccount

User = get_user_model()
NEXT = "/api/execution/jobs/next/"
_GATE_ON = {"BROKER_CONNECTIVITY_EXECUTION_GATE": "1"}


class NextJobDispatchGateTests(TestCase):
    """The WSE server-side final-dispatch gate at the authoritative claim boundary: no claimer/transport
    (ingest worker OR a direct host-bridge poller) can receive an ineligible exposure-opening job."""

    def setUp(self):
        self.user = User.objects.create_user(username="wse", email="wse@x.invalid", password="x")
        WorkerIdentity.objects.create(
            worker_id="wsew", worker_secret_hash=WorkerIdentity.hash_secret("s1"),
            status=WorkerIdentity.Status.ACTIVE)

    def _acct(self, *, validated):
        return TradingAccount.objects.create(
            user=self.user, name="A", account_number=f"WSE{validated}",
            is_demo=True, is_active=True, broker_name="DemoBroker",
            validation_status=("VALIDATED" if validated else "NEVER"),
            password_enc=("cipher" if validated else ""))

    def _job(self, acct, jt="PLACE_ORDER"):
        return ExecutionJob.objects.create(account=acct, job_type=jt, status="PENDING", payload={})

    def _claim(self, jt="PLACE_ORDER"):
        return APIClient().get(f"{NEXT}?worker_id=wsew&job_types={jt}",
                               HTTP_X_WORKER_ID="wsew", HTTP_X_WORKER_SECRET="s1")

    def test_ineligible_place_order_failed_at_claim_when_armed(self):
        job = self._job(self._acct(validated=False))
        with mock.patch.dict(os.environ, _GATE_ON):
            r = self._claim("PLACE_ORDER")
        self.assertEqual(r.status_code, 204)          # nothing served this poll
        job.refresh_from_db()
        self.assertEqual(job.status, "FAILED")        # failed under the row lock, never handed out
        self.assertTrue(AuditEvent.objects.filter(event_type="EXECUTION_DISPATCH_REFUSED").exists())

    def test_ineligible_place_test_order_failed_at_claim_when_armed(self):
        job = self._job(self._acct(validated=False), jt="PLACE_TEST_ORDER")
        with mock.patch.dict(os.environ, _GATE_ON):
            r = self._claim("PLACE_TEST_ORDER")
        self.assertEqual(r.status_code, 204)
        job.refresh_from_db()
        self.assertEqual(job.status, "FAILED")

    def test_eligible_place_order_served_when_armed(self):
        job = self._job(self._acct(validated=True))
        with mock.patch.dict(os.environ, _GATE_ON):
            r = self._claim("PLACE_ORDER")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], job.id)
        job.refresh_from_db()
        self.assertEqual(job.status, "RUNNING")

    def test_ineligible_place_order_served_when_gate_off(self):
        # Transparent when OFF: the ineligible job is claimed exactly as today (no gate, no extra read).
        job = self._job(self._acct(validated=False))
        r = self._claim("PLACE_ORDER")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], job.id)
        job.refresh_from_db()
        self.assertEqual(job.status, "RUNNING")

    def test_non_opening_sync_not_gated_when_armed(self):
        job = self._job(self._acct(validated=False), jt="SYNC_POSITIONS")
        with mock.patch.dict(os.environ, _GATE_ON):
            r = self._claim("SYNC_POSITIONS")
        self.assertEqual(r.status_code, 200)          # non-opening → not gated
        self.assertEqual(r.json()["id"], job.id)


class ParityStructureTests(TestCase):
    """The h4 / demo / promotion fixes are behaviourally identical to h1/m5 / the gate / eligibility paths
    (already tested); assert here that each site now carries the enforcing call, closing the known gaps."""

    def test_h4_scheduler_has_gate_specific_refusal_handler(self):
        from strategies.management.commands import run_h4_scheduler
        src = inspect.getsource(run_h4_scheduler)
        self.assertIn("ExecutionGateRefused", src)
        self.assertIn("ExecutionKillSwitchEngaged", src)
        self.assertIn('trigger="scheduler_h4"', src)
        self.assertIn("project_execution_refusal", src)

    def test_demo_test_order_uses_enforcing_audited_gate(self):
        from execution import views
        src = inspect.getsource(views.CreateDemoTradeJobView)
        self.assertIn("require_execution_gate", src)
        self.assertIn("require_not_broker_paused", src)

    def test_promotion_validate_has_pause_precheck(self):
        from execution import signal_promotion
        src = inspect.getsource(signal_promotion)
        self.assertIn("is_broker_paused", src)
        self.assertIn("broker_gate_paused", src)


class ModelGateBackstopTests(TestCase):
    """The AUTHORITATIVE safety net (independent of the file-granularity inventory drift guard): the
    ExecutionJob.save model gate fails closed on an exposure-opening INSERT for an ineligible account when
    armed, regardless of which call site created the job. This is what makes a new (or un-inventoried)
    creation site safe."""

    def setUp(self):
        self.user = User.objects.create_user(username="mg", email="mg@x.invalid", password="x")

    def _acct(self, *, validated):
        return TradingAccount.objects.create(
            user=self.user, name="A", account_number=f"MG{validated}", is_demo=True, is_active=True,
            broker_name="DemoBroker", validation_status=("VALIDATED" if validated else "NEVER"),
            password_enc=("cipher" if validated else ""))

    def test_save_gate_refuses_ineligible_exposure_opening_insert_when_armed(self):
        from execution.broker_gate import ExecutionGateRefused
        acct = self._acct(validated=False)
        with mock.patch.dict(os.environ, _GATE_ON):
            with self.assertRaises(ExecutionGateRefused):
                ExecutionJob.objects.create(account=acct, job_type="PLACE_ORDER", status="PENDING", payload={})
        # fail-closed: no job row was written
        self.assertFalse(ExecutionJob.objects.filter(account=acct).exists())

    def test_save_gate_transparent_when_off(self):
        acct = self._acct(validated=False)  # ineligible, but the gate is OFF
        job = ExecutionJob.objects.create(account=acct, job_type="PLACE_ORDER", status="PENDING", payload={})
        self.assertTrue(ExecutionJob.objects.filter(id=job.id).exists())  # created exactly as today

    def test_save_gate_allows_eligible_when_armed(self):
        acct = self._acct(validated=True)
        with mock.patch.dict(os.environ, _GATE_ON):
            job = ExecutionJob.objects.create(account=acct, job_type="PLACE_ORDER", status="PENDING", payload={})
        self.assertTrue(ExecutionJob.objects.filter(id=job.id).exists())
