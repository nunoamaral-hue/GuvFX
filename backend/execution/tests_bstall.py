"""GFX-PKT-MT5-BRIDGE-STALL — the 429 self-throttle-storm fix.

Root cause: the worker made 5 jobs/next/ calls per loop (~150/min) vs the backend's 100/min throttle
→ chronic HTTP 429 → the blanket except left the claimed job RUNNING (orphaned 'worker gone') and
tight-retried (a storm that also blocked protection MODIFY claims). Fixes proven here:
 * next_job accepts a PRIORITY-ORDERED job_types CSV → ONE prioritized claim per loop.
 * a pending low-priority SYNC never blocks a higher-priority MODIFY claim (isolation).
 * the worker maps 429 -> RateLimited (so the loop backs off, not tight-retries).
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from execution.models import ExecutionJob, WorkerIdentity
from trading.models import TradingAccount

User = get_user_model()
NEXT = "/api/execution/jobs/next/"


class PrioritizedClaimTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="w", email="w@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="A", account_number="N1", is_demo=True)
        WorkerIdentity.objects.create(
            worker_id="w1", worker_secret_hash=WorkerIdentity.hash_secret("s1"),
            status=WorkerIdentity.Status.ACTIVE)

    def _job(self, jt):
        return ExecutionJob.objects.create(account=self.acct, job_type=jt, status="PENDING", payload={})

    def _claim(self, params):
        return APIClient().get(NEXT + params, HTTP_X_WORKER_ID="w1", HTTP_X_WORKER_SECRET="s1")

    def test_job_types_claims_highest_priority(self):
        sync = self._job("SYNC_POSITIONS")
        modify = self._job("MODIFY_POSITION")
        r = self._claim("?worker_id=w1&job_types=MODIFY_POSITION,CLOSE_TRADE,SYNC_POSITIONS")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], modify.id)          # MODIFY beats SYNC in ONE request
        sync.refresh_from_db(); modify.refresh_from_db()
        self.assertEqual(sync.status, "PENDING")             # SYNC untouched
        self.assertEqual(modify.status, "RUNNING")

    def test_pending_sync_does_not_block_modify_claim(self):
        # Isolation: even with a SYNC already pending, a MODIFY is claimable in the same prioritized call.
        self._job("SYNC_POSITIONS")
        modify = self._job("MODIFY_POSITION")
        r = self._claim("?worker_id=w1&job_types=MODIFY_POSITION,SYNC_POSITIONS")
        self.assertEqual(r.json()["id"], modify.id)

    def test_falls_through_to_lower_priority_when_higher_absent(self):
        sync = self._job("SYNC_POSITIONS")
        r = self._claim("?worker_id=w1&job_types=MODIFY_POSITION,CLOSE_TRADE,SYNC_POSITIONS")
        self.assertEqual(r.json()["id"], sync.id)            # no MODIFY/CLOSE → claims the SYNC

    def test_default_still_sync_only(self):
        self._job("MODIFY_POSITION")                          # only a MODIFY pending
        r = self._claim("?worker_id=w1")                      # no job_type/job_types → SYNC-only default
        self.assertEqual(r.status_code, 204)                  # MODIFY NOT served on the default path

    def test_single_job_type_unchanged(self):
        m = self._job("MODIFY_POSITION")
        r = self._claim("?worker_id=w1&job_type=MODIFY_POSITION")
        self.assertEqual(r.json()["id"], m.id)

    def test_no_jobs_returns_204(self):
        r = self._claim("?worker_id=w1&job_types=MODIFY_POSITION,SYNC_POSITIONS")
        self.assertEqual(r.status_code, 204)


class WorkerRateLimitTests(TestCase):
    def test_claim_maps_429_to_ratelimited(self):
        import urllib.error
        from mt5_trade_ingest_worker import claim_next_job, RateLimited
        err = urllib.error.HTTPError("u", 429, "Too Many Requests", {"Retry-After": "3"}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RateLimited) as cm:
                claim_next_job(job_types="MODIFY_POSITION,SYNC_POSITIONS")
        self.assertEqual(cm.exception.retry_after, 3.0)

    def test_claim_204_returns_none(self):
        import urllib.error
        from mt5_trade_ingest_worker import claim_next_job
        err = urllib.error.HTTPError("u", 204, "No Content", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            self.assertIsNone(claim_next_job(job_types="SYNC_POSITIONS"))
