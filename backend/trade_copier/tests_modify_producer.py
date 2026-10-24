"""SL/TP MODIFY producer certification (ADR-0053). DEMO-only; SHADOW-by-default; verified-ticket-only.

Discriminating negative controls: an SL REMOVAL or WIDENING is NEVER auto-replicated (stops never weakened
silently); a tightening SL / any TP change IS producible; a MODIFY is never targeted by symbol; the kill
switch blocks an OPEN but NOT a (risk-reducing) MODIFY."""
from decimal import Decimal
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from execution import signal_proposals
from execution.models import ExecutionJob
from trade_copier import diff, reconcile, services
from trade_copier.execution_producer import admit_modify, produce_modify_one, produce_pending_modifies
from trade_copier.models import CopierReplication, MasterEvent, ReplicationStatus
from trade_copier.tests_close_producer import CloseFixture

ARMED = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True, COPIER_EXECUTE_DEMO_ARMED=True)
ENABLED = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True)


class ModifyFixture(CloseFixture):
    def _modify_event(self, event_type, *, prev_sl=None, new_sl=None, ticket=1, seq=3, side="BUY"):
        return MasterEvent.objects.create(
            master=self.master, sequence=seq, from_poll=self.poll, event_type=event_type,
            position_ticket=ticket, symbol="XAUUSD", side=side, volume_before=Decimal("0.10"),
            volume_after=Decimal("0.10"), volume_delta=Decimal("0"),
            prev_sl=(Decimal(str(prev_sl)) if prev_sl is not None else None),
            new_sl=(Decimal(str(new_sl)) if new_sl is not None else None), observed_at=timezone.now())

    def _modify_rep(self, follower, event, *, tsl="1995", ttp="2100", sltp_ok=True):
        return CopierReplication.objects.create(
            master_event=event, follower=follower, requested_op="MODIFY_SLTP",
            status=ReplicationStatus.SHADOW_RECORDED, follower_symbol="XAUUSD",
            translated_sl=(Decimal(tsl) if tsl else None), translated_tp=(Decimal(ttp) if ttp else None),
            sltp_translation_ok=sltp_ok)

    def _ready_modify(self, follower=None, *, event_type=None, prev_sl="1990", new_sl="1995", ticket=777):
        """A follower with a MATCHED open (ticket) + a pending MODIFY_SLTP intent for the same position."""
        f = follower or self._follower()
        self._matched_open(f, ticket=ticket)
        ev = self._modify_event(event_type or diff.SL_CHANGE, prev_sl=prev_sl, new_sl=new_sl)
        return f, self._modify_rep(f, ev)


class WeakeningGuardTests(ModifyFixture):
    def test_sl_removal_fails_closed(self):
        f, rep = self._ready_modify(event_type=diff.SL_REMOVE, prev_sl="1990", new_sl=None)
        self.assertEqual(admit_modify(rep)[2], "sl_removal_not_auto_replicated")

    def test_sl_change_to_none_is_removal(self):
        f, rep = self._ready_modify(event_type=diff.SL_CHANGE, prev_sl="1990", new_sl=None)
        self.assertEqual(admit_modify(rep)[2], "sl_removal_not_auto_replicated")

    def test_sl_widening_fails_closed(self):
        # BUY, entry 2000: prev dist |2000-1990|=10, new dist |2000-1980|=20 -> further = weaken.
        f, rep = self._ready_modify(event_type=diff.SL_CHANGE, prev_sl="1990", new_sl="1980")
        self.assertEqual(admit_modify(rep)[2], "sl_widening_not_auto_replicated")

    def test_sl_tightening_admitted(self):
        # prev dist 10, new dist |2000-1995|=5 -> closer = tighten = OK.
        f, rep = self._ready_modify(event_type=diff.SL_CHANGE, prev_sl="1990", new_sl="1995")
        self.assertTrue(admit_modify(rep)[0])

    def test_sl_set_admitted(self):
        f, rep = self._ready_modify(event_type=diff.SL_SET, prev_sl=None, new_sl="1990")
        self.assertTrue(admit_modify(rep)[0])

    def test_tp_change_admitted(self):
        f, rep = self._ready_modify(event_type=diff.TP_CHANGE, prev_sl="1990", new_sl="1990")
        self.assertTrue(admit_modify(rep)[0])

    def test_sl_change_unverifiable_fails_closed(self):
        # No prior SL recorded on the event -> cannot prove the change isn't a weaken -> fail closed.
        f, rep = self._ready_modify(event_type=diff.SL_CHANGE, prev_sl=None, new_sl="1995")
        self.assertEqual(admit_modify(rep)[2], "sl_change_unverifiable")


class AdmitModifyTicketTests(ModifyFixture):
    def test_no_verified_ticket_rejected(self):
        f = self._follower()
        ev = self._modify_event(diff.SL_CHANGE, prev_sl="1990", new_sl="1995")
        rep = self._modify_rep(f, ev)                      # no matched OPEN
        self.assertEqual(admit_modify(rep)[2], "no_verified_follower_ticket")

    def test_never_by_symbol(self):
        f = self._follower()
        other_open_event = MasterEvent.objects.create(
            master=self.master, sequence=9, from_poll=self.poll, event_type=MasterEvent.EventType.OPEN,
            position_ticket=2, symbol="XAUUSD", side="BUY", volume_before=Decimal("0"),
            volume_after=Decimal("0.10"), volume_delta=Decimal("0.10"), observed_at=timezone.now())
        self._matched_open(f, event=other_open_event, ticket=888)   # matched, but for ticket 2
        ev = self._modify_event(diff.SL_CHANGE, prev_sl="1990", new_sl="1995", ticket=1)
        rep = self._modify_rep(f, ev)
        self.assertEqual(admit_modify(rep)[2], "no_verified_follower_ticket")

    def test_sltp_incomplete_rejected(self):
        f, rep = self._ready_modify()
        rep.sltp_translation_ok = False
        rep.save(update_fields=["sltp_translation_ok"])
        self.assertEqual(admit_modify(rep)[2], "sltp_incomplete")

    def test_disabled_follower_rejected(self):
        f, rep = self._ready_modify()
        services.set_follower_enabled(f, False)
        self.assertEqual(admit_modify(rep)[2], "follower_disabled")

    def test_live_follower_rejected(self):
        f = self._follower(number="2000088", live=True)
        f, rep = self._ready_modify(follower=f)
        self.assertEqual(admit_modify(rep)[2], "follower_not_demo")


class ProduceModifyTests(ModifyFixture):
    def test_flag_off_no_modify(self):
        f, rep = self._ready_modify()
        out = produce_modify_one(rep)
        self.assertFalse(out["produced"])
        self.assertEqual(out["reason"], "producer_disabled")
        self.assertEqual(ExecutionJob.objects.count(), 0)

    @override_settings(**ENABLED)
    def test_shadow_records_no_job(self):
        f, rep = self._ready_modify()
        out = produce_modify_one(rep)
        self.assertTrue(out["produced"], out)
        self.assertIsNone(out["job_id"])
        self.assertEqual(ExecutionJob.objects.count(), 0)
        rep.refresh_from_db()
        self.assertEqual(rep.status, ReplicationStatus.SHADOW_JOB_RECORDED)
        self.assertEqual(rep.follower_ticket, 777)

    @override_settings(**ARMED)
    def test_armed_creates_one_modify_targeting_ticket(self):
        f, rep = self._ready_modify()
        out = produce_modify_one(rep)
        self.assertTrue(out["produced"], out)
        job = ExecutionJob.objects.get()
        self.assertEqual(job.job_type, ExecutionJob.JobType.MODIFY_POSITION)
        self.assertEqual(job.payload["ticket"], 777)
        self.assertEqual(float(job.payload["sl"]), 1995.0)   # carried as str(Decimal), bridge floats it
        self.assertEqual(job.payload["signal_source"], "copier")
        rep.refresh_from_db()
        self.assertEqual(rep.status, ReplicationStatus.EXECUTION_REQUESTED)

    @override_settings(**ARMED)
    def test_idempotent_no_double_modify(self):
        f, rep = self._ready_modify()
        produce_modify_one(rep)
        produce_modify_one(rep)
        self.assertEqual(ExecutionJob.objects.count(), 1)

    @override_settings(**ARMED)
    def test_dedup_preexisting_modify(self):
        f, rep = self._ready_modify()
        ExecutionJob.objects.create(account=f.follower_account, job_type=ExecutionJob.JobType.MODIFY_POSITION,
                                    status=ExecutionJob.Status.PENDING, payload={"ticket": 777})
        out = produce_modify_one(rep)
        self.assertFalse(out["produced"])
        self.assertEqual(out["reason"], "modify_already_in_flight")
        self.assertEqual(ExecutionJob.objects.filter(job_type=ExecutionJob.JobType.MODIFY_POSITION).count(), 1)

    @override_settings(**ARMED)
    def test_stale_arming_flip_records_shadow_no_job(self):
        """Cross-producer hardening — arming TRUE at preflight, FALSE at the in-lock decision => SHADOW, no job."""
        f, rep = self._ready_modify()
        with mock.patch("trade_copier.execution_producer.copier_execute_demo_armed",
                        side_effect=[True, False]) as m:
            out = produce_modify_one(rep)
        self.assertEqual(m.call_count, 2)
        self.assertTrue(out["produced"], out)
        self.assertIsNone(out["job_id"])
        self.assertEqual(ExecutionJob.objects.count(), 0)
        rep.refresh_from_db()
        self.assertEqual(rep.status, ReplicationStatus.SHADOW_JOB_RECORDED)

    @override_settings(**ARMED)
    def test_kill_switch_does_not_block_modify(self):
        f, rep = self._ready_modify()
        signal_proposals.engage_kill_switch(actor=self.user, reason="test")
        out = produce_modify_one(rep)
        self.assertTrue(out["produced"], out)                         # risk-reducing modify not blocked
        self.assertEqual(ExecutionJob.objects.filter(job_type=ExecutionJob.JobType.MODIFY_POSITION).count(), 1)

    @override_settings(**ARMED)
    def test_widening_not_produced(self):
        f, rep = self._ready_modify(event_type=diff.SL_CHANGE, prev_sl="1990", new_sl="1980")
        out = produce_modify_one(rep)
        self.assertFalse(out["produced"])
        self.assertEqual(out["reason"], "sl_widening_not_auto_replicated")
        self.assertEqual(ExecutionJob.objects.count(), 0)

    @override_settings(**ENABLED)
    def test_produce_pending_modifies_batch(self):
        self._ready_modify()
        summary = produce_pending_modifies()
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["produced"], 1)
