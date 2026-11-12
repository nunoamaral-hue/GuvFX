"""ADR-0096 — event-driven post-fill follower-ticket binding: deterministic, fail-closed tests.

Proves `trade_copier.open_result_binding.bind_open_result` binds `CopierReplication.follower_ticket` from the
OPEN order's fill RESULT (never a /mt5/positions read), fail-closed on UNKNOWN / FAILED / conflict / cross-bind,
idempotent on replay, and inert for a non-copier job. No host or broker contact.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution.models import ExecutionJob
from trade_copier import services
from trade_copier.open_result_binding import bind_open_result
from trade_copier.models import (CopierMaster, CopierReplication, MasterEvent, MasterObservationPoll,
                                 ReplicationStatus)
from trading.models import BrokerServer, TradingAccount

User = get_user_model()


class OpenResultBindingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("o", "o@x.invalid", "x")
        self.bs = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6-Demo",
                                              environment="demo")
        self.macct = TradingAccount.objects.create(user=self.user, name="m", broker_name="IS6",
                                                   account_number="100", is_demo=True, is_active=True,
                                                   broker_server=self.bs)
        self.facct = TradingAccount.objects.create(user=self.user, name="f", broker_name="IS6",
                                                   account_number="200", is_demo=True, is_active=True,
                                                   broker_server=self.bs)
        self.master = CopierMaster.objects.create(trading_account=self.macct, observe_enabled=True)
        self.f = services.add_follower(self.master, self.facct)
        services.set_follower_enabled(self.f, True)
        self._seq = 10

    def _open(self, *, ticket_result=None, status="SUCCESS", result=None):
        self._seq += 1
        poll = MasterObservationPoll.objects.create(master=self.master, observed_at=timezone.now(),
                                                    read_at=timezone.now(), position_count=1)
        ev = MasterEvent.objects.create(
            master=self.master, sequence=self._seq, from_poll=poll, event_type="OPEN",
            position_ticket=self._seq, symbol="XAUUSD", side="SELL", volume_before=Decimal("0"),
            volume_after=Decimal("0.01"), volume_delta=Decimal("0.01"), observed_at=timezone.now())
        rep = CopierReplication.objects.create(
            master_event=ev, follower=self.f, requested_op="OPEN",
            status=ReplicationStatus.EXECUTION_REQUESTED, follower_symbol="XAUUSD.std",
            target_volume=Decimal("0.01"))
        if result is None:
            result = {"ticket": ticket_result} if ticket_result is not None else {}
        job = ExecutionJob.objects.create(
            account=self.facct, job_type=ExecutionJob.JobType.PLACE_ORDER,
            status=getattr(ExecutionJob.Status, status), result=result,
            payload={"copier_replication_id": rep.id})
        rep.execution_job = job
        rep.save(update_fields=["execution_job"])
        return rep, job

    def test_success_binds_ticket_verified(self):
        rep, job = self._open(ticket_result=99001)
        out = bind_open_result(job)
        self.assertTrue(out["bound"])
        self.assertEqual(out["ticket"], 99001)
        rep.refresh_from_db()
        self.assertEqual(rep.follower_ticket, 99001)
        self.assertEqual(rep.reconciliation_status, "MATCHED")
        self.assertEqual(rep.status, ReplicationStatus.EXECUTION_VERIFIED)
        self.assertEqual((rep.broker_result or {}).get("ticket"), 99001)

    def test_unknown_success_without_ticket_does_not_bind(self):
        rep, job = self._open(ticket_result=None, status="SUCCESS")   # SUCCESS but no ticket → UNKNOWN
        out = bind_open_result(job)
        self.assertFalse(out["bound"])
        self.assertEqual(out["reason"], "unknown_no_ticket")
        rep.refresh_from_db()
        self.assertIsNone(rep.follower_ticket)
        self.assertNotEqual(rep.status, ReplicationStatus.EXECUTION_VERIFIED)

    def test_non_integer_ticket_is_unknown(self):
        rep, job = self._open(ticket_result="not-a-ticket")
        out = bind_open_result(job)
        self.assertFalse(out["bound"])
        self.assertEqual(out["reason"], "unknown_no_ticket")
        rep.refresh_from_db()
        self.assertIsNone(rep.follower_ticket)

    def test_prefers_position_ticket_over_order_ticket(self):
        # The bridge's OPEN result may carry both the opening ORDER ticket and, when a bridge surfaces it, the
        # POSITION ticket the governed MODIFY/CLOSE resolve by. Bind the POSITION ticket (MEDIUM-1).
        rep, job = self._open(result={"ticket": 205, "position": 100})
        out = bind_open_result(job)
        self.assertTrue(out["bound"])
        self.assertEqual(out["ticket"], 100)
        rep.refresh_from_db()
        self.assertEqual(rep.follower_ticket, 100)

    def test_bool_ticket_is_unknown(self):
        rep, job = self._open(result={"ticket": True})   # a JSON bool must never coerce to ticket 1
        out = bind_open_result(job)
        self.assertFalse(out["bound"])
        self.assertEqual(out["reason"], "unknown_no_ticket")
        rep.refresh_from_db()
        self.assertIsNone(rep.follower_ticket)

    def test_fractional_ticket_is_unknown(self):
        rep, job = self._open(result={"ticket": 99001.5})   # a fractional ticket is garbage, not truncated
        out = bind_open_result(job)
        self.assertFalse(out["bound"])
        self.assertEqual(out["reason"], "unknown_no_ticket")
        rep.refresh_from_db()
        self.assertIsNone(rep.follower_ticket)

    def test_failed_marks_execution_failed_no_ticket(self):
        rep, job = self._open(ticket_result=None, status="FAILED")
        out = bind_open_result(job)
        self.assertFalse(out["bound"])
        self.assertEqual(out["reason"], "open_failed")
        rep.refresh_from_db()
        self.assertEqual(rep.status, ReplicationStatus.EXECUTION_FAILED)
        self.assertIsNone(rep.follower_ticket)

    def test_idempotent_same_ticket_replay(self):
        rep, job = self._open(ticket_result=99002)
        bind_open_result(job)
        out2 = bind_open_result(job)
        self.assertTrue(out2["bound"])
        self.assertTrue(out2.get("idempotent"))
        rep.refresh_from_db()
        self.assertEqual(rep.follower_ticket, 99002)

    def test_conflicting_ticket_fails_closed_no_overwrite(self):
        rep, job = self._open(ticket_result=99003)
        bind_open_result(job)
        job.result = {"ticket": 88888}
        job.save(update_fields=["result"])
        out = bind_open_result(job)
        self.assertFalse(out["bound"])
        self.assertEqual(out["reason"], "ticket_conflict")
        rep.refresh_from_db()
        self.assertEqual(rep.follower_ticket, 99003)   # NOT overwritten

    def test_cross_bind_rejected(self):
        rep, _job = self._open(ticket_result=99004)
        other = ExecutionJob.objects.create(
            account=self.facct, job_type=ExecutionJob.JobType.PLACE_ORDER,
            status=ExecutionJob.Status.SUCCESS, result={"ticket": 77777},
            payload={"copier_replication_id": rep.id})
        out = bind_open_result(other)
        self.assertFalse(out["bound"])
        self.assertEqual(out["reason"], "job_replication_mismatch")
        rep.refresh_from_db()
        self.assertIsNone(rep.follower_ticket)

    def test_non_copier_job_is_inert(self):
        job = ExecutionJob.objects.create(
            account=self.facct, job_type=ExecutionJob.JobType.PLACE_ORDER,
            status=ExecutionJob.Status.SUCCESS, result={"ticket": 1}, payload={})
        out = bind_open_result(job)
        self.assertFalse(out["bound"])
        self.assertEqual(out["reason"], "not_copier_order")

    def test_non_place_order_is_inert(self):
        job = ExecutionJob.objects.create(
            account=self.facct, job_type=ExecutionJob.JobType.MODIFY_POSITION,
            status=ExecutionJob.Status.SUCCESS, result={"ticket": 1},
            payload={"copier_replication_id": 1})
        out = bind_open_result(job)
        self.assertFalse(out["bound"])
        self.assertEqual(out["reason"], "not_place_order")
