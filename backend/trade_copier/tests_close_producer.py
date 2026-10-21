"""Full-CLOSE producer certification (ADR-0052). DEMO-only; SHADOW-by-default; verified-ticket-only.

Discriminating negative controls: a CLOSE is NEVER produced without a MATCHED copier-owned follower
ticket; a same-symbol position on a DIFFERENT master ticket is NEVER closed (no symbol fallback); the
kill switch blocks the paired OPEN but NOT a (risk-reducing) CLOSE; a duplicate produces no second job."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from execution import signal_proposals
from execution.models import ExecutionJob
from trade_copier import reconcile, services
from trade_copier.execution_producer import admit_close, produce_close_one, produce_pending_closes
from trade_copier.models import CopierReplication, MasterEvent, ReplicationStatus
from trade_copier.tests_execution_producer import ProducerFixture

User = get_user_model()
ARMED = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True, COPIER_EXECUTE_DEMO_ARMED=True)
ENABLED = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True)


class CloseFixture(ProducerFixture):
    def _matched_open(self, follower, *, event=None, ticket=777):
        return CopierReplication.objects.create(
            master_event=(event or self.event), follower=follower, requested_op="OPEN",
            status=ReplicationStatus.EXECUTION_REQUESTED, follower_ticket=ticket,
            reconciliation_status=reconcile.MATCHED, follower_symbol="XAUUSD",
            target_volume=Decimal("0.05"), sltp_translation_ok=True)

    def _close_event(self, ticket=1, seq=2):
        return MasterEvent.objects.create(
            master=self.master, sequence=seq, from_poll=self.poll,
            event_type=MasterEvent.EventType.FULL_CLOSE, position_ticket=ticket, symbol="XAUUSD",
            side="BUY", volume_before=Decimal("0.10"), volume_after=Decimal("0"),
            volume_delta=Decimal("-0.10"), observed_at=timezone.now())

    def _close_rep(self, follower, event):
        return CopierReplication.objects.create(
            master_event=event, follower=follower, requested_op="CLOSE",
            status=ReplicationStatus.SHADOW_RECORDED, follower_symbol="XAUUSD", sltp_translation_ok=True)

    def _ready_close(self, follower=None, ticket=777):
        """A follower with a MATCHED open (ticket) + a pending FULL_CLOSE intent for the same position."""
        f = follower or self._follower()
        self._matched_open(f, ticket=ticket)
        return f, self._close_rep(f, self._close_event())


class AdmitCloseTests(CloseFixture):
    def test_rejected_without_reconciled_open(self):
        f = self._follower()
        rep = self._close_rep(f, self._close_event())      # no matched OPEN at all
        self.assertEqual(admit_close(rep)[2], "no_verified_follower_ticket")

    def test_rejected_when_open_unreconciled(self):
        f = self._follower()
        # OPEN exists but not yet matched (ticket null, status UNMATCHED) -> fail closed.
        CopierReplication.objects.create(
            master_event=self.event, follower=f, requested_op="OPEN",
            status=ReplicationStatus.EXECUTION_REQUESTED, reconciliation_status=reconcile.UNMATCHED,
            follower_symbol="XAUUSD", target_volume=Decimal("0.05"), sltp_translation_ok=True)
        rep = self._close_rep(f, self._close_event())
        self.assertEqual(admit_close(rep)[2], "no_verified_follower_ticket")

    def test_never_closes_by_symbol_resemblance(self):
        """A MATCHED open for a DIFFERENT master position (same symbol) must NOT satisfy this close."""
        f = self._follower()
        other_open_event = MasterEvent.objects.create(
            master=self.master, sequence=5, from_poll=self.poll, event_type=MasterEvent.EventType.OPEN,
            position_ticket=2, symbol="XAUUSD", side="BUY", volume_before=Decimal("0"),
            volume_after=Decimal("0.10"), volume_delta=Decimal("0.10"), observed_at=timezone.now())
        self._matched_open(f, event=other_open_event, ticket=888)   # matched, but for ticket 2
        rep = self._close_rep(f, self._close_event(ticket=1))         # close is for ticket 1
        ok, ticket, reason = admit_close(rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_verified_follower_ticket")

    def test_disabled_follower_rejected(self):
        f, rep = self._ready_close()
        services.set_follower_enabled(f, False)
        self.assertEqual(admit_close(rep)[2], "follower_disabled")

    def test_live_follower_rejected(self):
        f = self._follower(number="2000077", live=True)
        f, rep = self._ready_close(follower=f)
        self.assertEqual(admit_close(rep)[2], "follower_not_demo")


class ProduceCloseTests(CloseFixture):
    def test_flag_off_no_close(self):
        f, rep = self._ready_close()
        out = produce_close_one(rep)
        self.assertFalse(out["produced"])
        self.assertEqual(out["reason"], "producer_disabled")
        self.assertEqual(ExecutionJob.objects.count(), 0)

    @override_settings(**ENABLED)
    def test_shadow_records_no_job(self):
        f, rep = self._ready_close()
        out = produce_close_one(rep)
        self.assertTrue(out["produced"], out)
        self.assertIsNone(out["job_id"])                              # SHADOW: no ExecutionJob
        self.assertEqual(ExecutionJob.objects.count(), 0)
        rep.refresh_from_db()
        self.assertEqual(rep.status, ReplicationStatus.SHADOW_JOB_RECORDED)
        self.assertEqual(rep.follower_ticket, 777)                    # intended ticket recorded

    @override_settings(**ARMED)
    def test_armed_creates_one_close_trade_targeting_ticket(self):
        f, rep = self._ready_close()
        out = produce_close_one(rep)
        self.assertTrue(out["produced"], out)
        job = ExecutionJob.objects.get()
        self.assertEqual(job.job_type, ExecutionJob.JobType.CLOSE_TRADE)
        self.assertEqual(job.payload["ticket"], 777)                  # the VERIFIED follower ticket
        self.assertEqual(job.payload["signal_source"], "copier")
        rep.refresh_from_db()
        self.assertEqual(rep.status, ReplicationStatus.EXECUTION_REQUESTED)
        self.assertEqual(rep.execution_job_id, job.id)

    @override_settings(**ARMED)
    def test_idempotent_no_double_close(self):
        f, rep = self._ready_close()
        produce_close_one(rep)
        produce_close_one(rep)
        self.assertEqual(ExecutionJob.objects.count(), 1)

    @override_settings(**ARMED)
    def test_dedup_preexisting_close_trade(self):
        f, rep = self._ready_close()
        ExecutionJob.objects.create(account=f.follower_account, job_type=ExecutionJob.JobType.CLOSE_TRADE,
                                    status=ExecutionJob.Status.PENDING, payload={"ticket": 777})
        out = produce_close_one(rep)
        self.assertFalse(out["produced"])
        self.assertEqual(out["reason"], "close_already_in_flight")
        self.assertEqual(ExecutionJob.objects.filter(job_type=ExecutionJob.JobType.CLOSE_TRADE).count(), 1)

    @override_settings(**ARMED)
    def test_kill_switch_does_not_block_close(self):
        """Closing reduces risk: CLOSE_TRADE is NOT in KILL_SWITCH_BLOCKED_JOB_TYPES (unlike the OPEN)."""
        f, rep = self._ready_close()
        signal_proposals.engage_kill_switch(actor=self.user, reason="test")
        out = produce_close_one(rep)
        self.assertTrue(out["produced"], out)                         # close STILL produced under kill switch
        self.assertEqual(ExecutionJob.objects.filter(job_type=ExecutionJob.JobType.CLOSE_TRADE).count(), 1)

    @override_settings(**ENABLED)
    def test_produce_pending_closes_batch(self):
        self._ready_close()
        summary = produce_pending_closes()
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["produced"], 1)
