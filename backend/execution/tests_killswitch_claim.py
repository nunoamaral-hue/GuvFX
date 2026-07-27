"""Phase 2 (Control 6) — the kill switch is honoured at the CLAIM endpoint.

Previously the kill switch (ExecutionControl / GUVFX_EXECUTION_DISABLED) blocked only order CREATION;
an already-created PLACE_ORDER could still be claimed + executed by a worker during a suspension.
next_job now filters the order-OPENING job types (KILL_SWITCH_BLOCKED_JOB_TYPES) out of the claim while
suspended, so exposure-opening jobs are never handed out — but risk-reducing types (CLOSE_TRADE,
MODIFY_POSITION, SYNC_POSITIONS) still flow so open positions can be flattened/managed.
"""
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from execution.models import ExecutionJob, WorkerIdentity
from trading.models import TradingAccount

User = get_user_model()
NEXT = "/api/execution/jobs/next/"
SUSPENDED = {"GUVFX_EXECUTION_DISABLED": "true"}


class KillSwitchClaimTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ks", email="ks@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="A", account_number="KS1", is_demo=True)
        WorkerIdentity.objects.create(
            worker_id="ksw", worker_secret_hash=WorkerIdentity.hash_secret("s1"),
            status=WorkerIdentity.Status.ACTIVE)

    def _job(self, jt):
        return ExecutionJob.objects.create(account=self.acct, job_type=jt, status="PENDING", payload={})

    def _claim(self, params):
        return APIClient().get(NEXT + params, HTTP_X_WORKER_ID="ksw", HTTP_X_WORKER_SECRET="s1")

    def test_place_order_filtered_but_close_flows_while_suspended(self):
        place = self._job("PLACE_ORDER")
        close = self._job("CLOSE_TRADE")
        with mock.patch.dict(os.environ, SUSPENDED):
            r = self._claim("?worker_id=ksw&job_types=PLACE_ORDER,CLOSE_TRADE")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], close.id)  # order-opening filtered; risk-reducing served
        place.refresh_from_db(); close.refresh_from_db()
        self.assertEqual(place.status, "PENDING")    # PLACE_ORDER NOT claimed during suspension
        self.assertEqual(close.status, "RUNNING")

    def test_only_order_opening_requested_returns_204_while_suspended(self):
        place = self._job("PLACE_ORDER")
        with mock.patch.dict(os.environ, SUSPENDED):
            r = self._claim("?worker_id=ksw&job_types=PLACE_ORDER")
        self.assertEqual(r.status_code, 204)
        place.refresh_from_db()
        self.assertEqual(place.status, "PENDING")

    def test_place_test_order_also_filtered_while_suspended(self):
        self._job("PLACE_TEST_ORDER")
        with mock.patch.dict(os.environ, SUSPENDED):
            r = self._claim("?worker_id=ksw&job_types=PLACE_TEST_ORDER")
        self.assertEqual(r.status_code, 204)

    def test_place_order_claimed_when_not_suspended(self):
        place = self._job("PLACE_ORDER")
        r = self._claim("?worker_id=ksw&job_types=PLACE_ORDER")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], place.id)

    def test_sync_still_flows_while_suspended(self):
        sync = self._job("SYNC_POSITIONS")
        with mock.patch.dict(os.environ, SUSPENDED):
            r = self._claim("?worker_id=ksw&job_types=PLACE_ORDER,SYNC_POSITIONS")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], sync.id)  # SYNC flows; PLACE_ORDER filtered

    def test_open_trade_also_filtered_while_suspended(self):
        # every order-opening type in KILL_SWITCH_BLOCKED_JOB_TYPES is filtered, incl OPEN_TRADE
        self._job("OPEN_TRADE")
        with mock.patch.dict(os.environ, SUSPENDED):
            r = self._claim("?worker_id=ksw&job_types=OPEN_TRADE")
        self.assertEqual(r.status_code, 204)

    def test_db_kill_switch_flag_also_filters(self):
        # the DB ExecutionControl.kill_switch_engaged branch (not just the env var) also filters.
        # The job is created BEFORE engaging (the exact Control-6 scenario: an already-created job
        # during a later suspension — the model-layer guard blocks NEW order-opening creation).
        place = self._job("PLACE_ORDER")
        from execution import signal_proposals
        signal_proposals.engage_kill_switch(actor=self.user, reason="test")
        r = self._claim("?worker_id=ksw&job_types=PLACE_ORDER")
        self.assertEqual(r.status_code, 204)
        place.refresh_from_db()
        self.assertEqual(place.status, "PENDING")
