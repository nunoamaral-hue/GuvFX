"""
GFX-PKT-OPS-OBSERVABILITY-FOUNDATION tests.

Proves: (1) the observability helpers emit structured JSON records and are
fail-open; (2) a single correlation id propagates signal → plan → shadow job
payload; (3) the worker emits the shadow lifecycle stages + metrics under that
correlation id — all WITHOUT changing execution behaviour (no order placed).
"""

import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from core.observability import emit_metric, log_stage, new_correlation_id
from execution.signal_planning import plan_demo_execution
from execution.signal_promotion import promote_plan_to_shadow_jobs
from execution.models import (
    ExecutionJob,
    SignalExecutionPlan,
    SignalSourceConfig,
    WorkerIdentity,
)
from signal_intake import services as intake_services
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount

User = get_user_model()
SRC = PendingSignalApproval.Source.WAYOND_TELEGRAM
SIGNAL_MSG = (
    "XAUUSD | Potential downward movement\n\nXAUUSD | SELL 3350.0\n\n"
    "❌ Stop Loss 3360.0 (100 pips)\n\n✅ TP1 3335.0\n✅ TP2 3320.0"
)


def _records(cm):
    out = []
    for r in cm.records:
        try:
            out.append(json.loads(r.getMessage()))
        except Exception:
            pass
    return out


class ObservabilityHelperTests(SimpleTestCase):
    def test_new_correlation_id_is_unique_hex(self):
        a, b = new_correlation_id(), new_correlation_id()
        self.assertEqual(len(a), 32)
        int(a, 16)  # hex — raises if not
        self.assertNotEqual(a, b)

    def test_log_stage_emits_structured_record(self):
        with self.assertLogs("guvfx.execution.lifecycle", level="INFO") as cm:
            log_stage("worker_claimed", "corr-1", job_id=7, queue_depth=3)
        rec = _records(cm)[0]
        self.assertEqual(rec["event"], "execution_lifecycle")
        self.assertEqual(rec["stage"], "worker_claimed")
        self.assertEqual(rec["correlation_id"], "corr-1")
        self.assertEqual(rec["job_id"], 7)
        self.assertEqual(rec["queue_depth"], 3)

    def test_emit_metric_emits_structured_record(self):
        with self.assertLogs("guvfx.execution.metrics", level="INFO") as cm:
            emit_metric("mt5_response_latency", 42, correlation_id="corr-2", unit="ms")
        rec = _records(cm)[0]
        self.assertEqual(rec["event"], "execution_metric")
        self.assertEqual(rec["metric"], "mt5_response_latency")
        self.assertEqual(rec["value"], 42)
        self.assertEqual(rec["unit"], "ms")
        self.assertEqual(rec["correlation_id"], "corr-2")

    def test_helpers_are_fail_open(self):
        # A value json cannot natively serialise must NOT raise (default=str).
        class Weird:
            def __repr__(self):
                return "weird"

        with self.assertLogs("guvfx.execution.lifecycle", level="INFO"):
            log_stage("planning_complete", "corr-3", obj=Weird())  # no exception
        with self.assertLogs("guvfx.execution.metrics", level="INFO"):
            emit_metric("execution_duration", Decimal("1.5"), unit="ms")  # no exception


class CorrelationPropagationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True
        )
        SignalSourceConfig.objects.create(
            source=SRC, auto_demo_execution_enabled=True, total_lot_target=Decimal("0.02")
        )

    def test_intake_generates_a_correlation_id(self):
        a = intake_services.intake_message(SIGNAL_MSG, "m-corr", actor=self.user)
        self.assertEqual(a.status, PendingSignalApproval.Status.PENDING_APPROVAL)
        self.assertTrue(a.correlation_id)
        self.assertEqual(len(a.correlation_id), 32)

    def _approved(self, cid, mid="corr-prop"):
        return PendingSignalApproval.objects.create(
            source=SRC, message_id=mid, symbol="EURUSD", direction="BUY",
            stop_loss="1.0800", take_profits=["1.0900", "1.0950"],
            correlation_id=cid, status=PendingSignalApproval.Status.APPROVED,
        )

    def test_correlation_propagates_approval_to_plan_to_shadow_job(self):
        cid = "corr-abc-123"
        approval = self._approved(cid)
        plan = plan_demo_execution(approval, account=self.demo, total_lot=Decimal("0.02"))
        self.assertEqual(plan.status, SignalExecutionPlan.Status.PLANNED)
        self.assertEqual(plan.correlation_id, cid)

        jobs = promote_plan_to_shadow_jobs(plan)
        self.assertTrue(jobs)
        for job in jobs:
            self.assertEqual(job.payload.get("correlation_id"), cid)

    def test_plan_mints_fresh_id_when_approval_has_none(self):
        approval = self._approved("", mid="corr-empty")  # pre-existing row: no id
        plan = plan_demo_execution(approval, account=self.demo, total_lot=Decimal("0.02"))
        self.assertTrue(plan.correlation_id)
        self.assertEqual(len(plan.correlation_id), 32)


class WorkerShadowLifecycleTests(SimpleTestCase):
    def setUp(self):
        import mt5_trade_ingest_worker as worker
        self.worker = worker

    def _job(self, **payload):
        base = {"symbol": "EURUSD", "side": "BUY", "lots": "0.01", "comment": "WAY1L1",
                "execution_mode": "SHADOW", "correlation_id": "corr-worker-9"}
        base.update(payload)
        return {"id": 5, "job_type": "PLACE_ORDER_SHADOW", "account": 1,
                "created_at": "2026-07-01T00:00:00+00:00", "payload": base}

    def _run(self, check_result):
        with mock.patch.object(self.worker, "agent_order_check", return_value=check_result), \
             mock.patch.object(self.worker, "complete_job", return_value=(200, {})), \
             self.assertLogs("guvfx.execution.lifecycle", level="INFO") as lc, \
             self.assertLogs("guvfx.execution.metrics", level="INFO") as mc:
            res = self.worker.handle_shadow_job(self._job())
        return res, _records(lc), _records(mc)

    def test_success_emits_all_shadow_stages_and_metrics(self):
        res, life, metrics = self._run(
            {"ok": True, "retcode": 0, "margin": 5.0, "free_margin": 9995.0, "comment": "Done"}
        )
        self.assertTrue(res["ok"])
        self.assertFalse(res["order_send_called"])  # no order placed
        stages = {r["stage"] for r in life}
        self.assertLessEqual(
            {"order_check_request", "order_check_response", "validation_outcome", "cleanup_complete"},
            stages,
        )
        # every lifecycle record carries the correlation id
        self.assertTrue(all(r["correlation_id"] == "corr-worker-9" for r in life))
        names = {m["metric"] for m in metrics}
        self.assertIn("mt5_response_latency", names)
        self.assertIn("validation_success", names)
        self.assertIn("execution_duration", names)

    def test_failed_validation_emits_failure_metric(self):
        res, life, metrics = self._run({"ok": False, "error": "order_rejected", "retcode": 10018})
        self.assertFalse(res["ok"])
        outcome = [r for r in life if r["stage"] == "validation_outcome"][0]
        self.assertFalse(outcome["ok"])
        self.assertIn("validation_failure", {m["metric"] for m in metrics})


class NextJobClaimObservabilityTests(TestCase):
    """Stage 5 is server-side (views.next_job), so it needs a real endpoint call.

    Confirms claiming a PLACE_ORDER_SHADOW job through the actual next_job endpoint
    emits the `worker_claimed` lifecycle stage (under the payload's correlation id)
    plus the `worker_claim_latency` and `shadow_queue_depth` metrics.
    """

    NEXT_URL = "/api/execution/jobs/next/"

    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True
        )
        self.job = ExecutionJob.objects.create(
            job_type=ExecutionJob.JobType.PLACE_ORDER_SHADOW,
            account=self.demo, status=ExecutionJob.Status.PENDING, terminal_node=None,
            payload={"symbol": "EURUSD", "side": "BUY", "lots": "0.01",
                     "execution_mode": "SHADOW", "comment": "WAY1L1",
                     "correlation_id": "corr-claim-5"},
        )

    def _shadow_worker(self):
        WorkerIdentity.objects.create(
            worker_id="obs-shadow", worker_secret_hash=WorkerIdentity.hash_secret("sek"),
            worker_permissions={"shadow_worker": True}, status=WorkerIdentity.Status.ACTIVE,
        )
        return APIClient(), dict(HTTP_X_WORKER_ID="obs-shadow", HTTP_X_WORKER_SECRET="sek")

    def test_claiming_shadow_job_emits_stage5_and_claim_metrics(self):
        c, h = self._shadow_worker()
        with self.assertLogs("guvfx.execution.lifecycle", level="INFO") as lc, \
             self.assertLogs("guvfx.execution.metrics", level="INFO") as mc:
            resp = c.get(self.NEXT_URL + "?job_type=PLACE_ORDER_SHADOW", **h)
        self.assertEqual(resp.status_code, 200)  # claim behaviour unchanged
        self.assertEqual(resp.data["id"], self.job.id)

        life = _records(lc)
        claimed = [r for r in life if r["stage"] == "worker_claimed"]
        self.assertTrue(claimed, "worker_claimed stage not emitted")
        self.assertEqual(claimed[0]["correlation_id"], "corr-claim-5")

        names = {m["metric"] for m in _records(mc)}
        self.assertIn("worker_claim_latency", names)
        self.assertIn("shadow_queue_depth", names)
