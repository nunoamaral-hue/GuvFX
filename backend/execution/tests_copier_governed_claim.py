"""GFX-COPIER-PRODUCTION-ACTUATORS-PIPELINE-153 (ADR-0117) — the generic ``jobs/next`` claim excludes GOVERNED
copier MODIFY/CLOSE jobs (``payload.copier_governed=True``), which are served ONLY by the governed
``run_copier_mutation_dispatch``. Copier OPEN (PLACE_ORDER, no marker) and every non-copier MODIFY/CLOSE are
unaffected — so a generic worker can never dispatch a governed copier mutation without the governed guards, and
nothing else changes.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from execution.models import ExecutionJob, WorkerIdentity
from trading.models import TradingAccount

User = get_user_model()
NEXT = "/api/execution/jobs/next/"


class GovernedCopierClaimTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="gc", email="gc@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="A", account_number="GC1", is_demo=True, broker_name="DemoBroker")
        WorkerIdentity.objects.create(
            worker_id="gcw", worker_secret_hash=WorkerIdentity.hash_secret("s1"),
            status=WorkerIdentity.Status.ACTIVE)

    def _job(self, jt, payload):
        return ExecutionJob.objects.create(account=self.acct, job_type=jt, status="PENDING", payload=payload)

    def _claim(self, job_types):
        return APIClient().get(f"{NEXT}?worker_id=gcw&job_types={job_types}",
                               HTTP_X_WORKER_ID="gcw", HTTP_X_WORKER_SECRET="s1")

    def test_governed_modify_excluded_but_ordinary_modify_served(self):
        gov = self._job("MODIFY_POSITION", {"copier_governed": True, "ticket": 1})
        ordinary = self._job("MODIFY_POSITION", {"ticket": 2})
        r = self._claim("MODIFY_POSITION")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], ordinary.id)   # the governed copier MODIFY is excluded; the ordinary one flows
        gov.refresh_from_db()
        ordinary.refresh_from_db()
        self.assertEqual(gov.status, "PENDING")          # never handed to a generic worker
        self.assertEqual(ordinary.status, "RUNNING")

    def test_governed_close_excluded_from_generic_claim(self):
        gov = self._job("CLOSE_TRADE", {"copier_governed": True, "ticket": 1})
        r = self._claim("CLOSE_TRADE")
        self.assertEqual(r.status_code, 204)             # only a governed CLOSE exists -> nothing claimable here
        gov.refresh_from_db()
        self.assertEqual(gov.status, "PENDING")

    def test_copier_open_place_order_still_served(self):
        # a copier OPEN carries NO copier_governed marker, so it stays claimable by the generic worker (as today).
        job = self._job("PLACE_ORDER", {"signal_source": "copier", "ticket": 3})
        r = self._claim("PLACE_ORDER")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], job.id)
