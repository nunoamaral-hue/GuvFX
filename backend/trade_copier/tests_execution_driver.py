"""GFX-COPIER-PRODUCTION-ACTUATORS-PIPELINE-153 (ADR-0117) — tests for the governed copier EXECUTION DRIVER
(bounded fallback pass, DARK gating, single-flight lease) and the governed-copier mutation OWNERSHIP split.

No real host, broker or secret. The driver pass is exercised with injected fake stages; the lease uses a temp file.
"""
import json
import os
import tempfile
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from execution.models import ExecutionJob
from trade_copier import execution_driver as ed
from trade_copier import mutation_dispatch as md
from trade_copier.models import CopierReplication, ReplicationStatus
from trade_copier.tests_execution_producer import ProducerFixture

_PASS_GATE = lambda rep, op, now: (True, "ok")   # noqa: E731 — injected passing producer-parity gate


# ───────────────────────────── driver pass logic (no DB; injected stages) ─────────────────────────────
class DriverPassTests(SimpleTestCase):
    def test_pass_runs_gated_stages_skips_off_and_captures_errors(self):
        calls = []

        def ok_stage(limit):
            calls.append(("ok", limit))
            return {"did": "ok"}

        def boom_stage(limit):
            calls.append(("boom", limit))
            raise RuntimeError("x")

        stages = [("on", ok_stage, lambda: True),
                  ("off", ok_stage, lambda: False),
                  ("err", boom_stage, lambda: True)]
        out = ed.run_driver_pass(limit=7, stages=stages)
        s = {d["stage"]: d for d in out["stages"]}
        self.assertEqual(s["on"].get("summary"), "{'did': 'ok'}")
        self.assertEqual(s["off"].get("skipped"), "gate_off")      # gate-off stage never ran
        self.assertEqual(s["err"].get("error"), "RuntimeError")    # error captured, pass not sunk
        self.assertEqual(calls, [("ok", 7), ("boom", 7)])          # the off stage's fn was NOT called

    def test_default_stages_are_the_full_ordered_pipeline(self):
        names = [n for (n, _f, _g) in ed.default_stages()]
        self.assertEqual(names, ["replicate", "execute_open", "reconcile", "execute_modify",
                                 "execute_close", "mutation_dispatch", "lifecycle_dispatch"])


# ───────────────────────────── driver command: gate + single-flight lease ─────────────────────────────
class DriverCommandTests(TestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("run_copier_execution_driver", *args, stdout=out)
        return json.loads(out.getvalue())

    def test_status_is_read_only_and_dark_by_default(self):
        res = self._run("--status")
        self.assertFalse(res["driver_enabled"])
        self.assertEqual(res["execution_banner"], "SHADOW — EXECUTION OFF")
        self.assertIn("dead_letter", res)

    def test_disabled_driver_is_a_noop(self):
        res = self._run()
        self.assertFalse(res["ran"])
        self.assertEqual(res["reason"], "driver_disabled_dark")

    @override_settings(COPIER_ENABLED=True, COPIER_EXECUTION_DRIVER_ENABLED=True)
    def test_runs_a_bounded_pass_under_a_released_lease(self):
        with tempfile.TemporaryDirectory() as d:
            lp = os.path.join(d, "drv.json")
            res = self._run("--lease-path", lp, "--iterations", "1")
            self.assertTrue(res["ran"])
            self.assertEqual(res["iterations"], 1)
            # with every stage gate off, each stage is a no-op
            self.assertTrue(all(st.get("skipped") == "gate_off" for st in res["passes"][0]["stages"]))
            self.assertFalse(os.path.exists(lp))   # lease released in the finally

    @override_settings(COPIER_ENABLED=True, COPIER_EXECUTION_DRIVER_ENABLED=True)
    def test_single_flight_lease_blocks_a_second_driver(self):
        from hosted_workspace.recovery_lease import KIND_COPIER_DRIVER, acquire, release
        with tempfile.TemporaryDirectory() as d:
            lp = os.path.join(d, "drv.json")
            ok, _ = acquire(KIND_COPIER_DRIVER, purpose="held", owner="other", correlation_id="held-1",
                            ttl_s=300, now=timezone.now(), path=lp)
            self.assertTrue(ok)
            try:
                res = self._run("--lease-path", lp)
                self.assertFalse(res["ran"])
                self.assertEqual(res["reason"], "lease_unavailable")
            finally:
                release("held-1", timezone.now(), path=lp)


# ───────────────────────────── governed-copier mutation OWNERSHIP ─────────────────────────────
@override_settings(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True, COPIER_LIVE_EXECUTE_ENABLED=True,
                   COPIER_MUTATION_DISPATCH_ENABLED=True)
class MutationOwnershipTests(ProducerFixture):
    def _rep_job(self, *, governed, ticket):
        f = self._follower(number=str(2500000 + ticket))
        rep = CopierReplication.objects.create(
            master_event=self.event, follower=f, requested_op="CLOSE", follower_symbol="XAUUSD",
            target_volume=Decimal("0.05"), follower_ticket=ticket, status=ReplicationStatus.EXECUTION_REQUESTED)
        payload = {"copier_operation": "FULL_CLOSE", "ticket": ticket, "require_identity_pin": True}
        if governed:
            payload["copier_governed"] = True
        job = ExecutionJob.objects.create(
            account=f.follower_account, job_type=ExecutionJob.JobType.CLOSE_TRADE,
            status=ExecutionJob.Status.PENDING, payload=payload, hosted_idempotency_key=f"own-test:{rep.pk}")
        rep.execution_job = job
        rep.save(update_fields=["execution_job"])
        return job, rep

    def test_governed_path_scans_only_marked_jobs(self):
        self._rep_job(governed=True, ticket=6001)
        ungov, _ = self._rep_job(governed=False, ticket=6002)
        out = md.run_mutation_dispatch_pass(gate_check=_PASS_GATE, limit=50)   # default INERT transport
        self.assertTrue(out["enabled"])
        self.assertEqual(out["scanned"], 1)   # ONLY the copier_governed job is scanned
        ungov.refresh_from_db()
        self.assertEqual(ungov.status, ExecutionJob.Status.PENDING)   # non-governed job untouched by this path
