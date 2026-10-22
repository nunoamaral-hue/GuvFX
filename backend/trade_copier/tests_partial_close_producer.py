"""Partial-CLOSE producer certification (ADR-0055). DEMO-only; SHADOW-by-default; verified-ticket-only;
proportional reduction from authoritative current follower volume; fail closed on zero/below-min/sub-min
remainder; NEVER by symbol; NEVER a full close. (Full-close-after-rounding and the single-send no-retry are
enforced + tested at the bridge layer — see tests_bridge_partial_close.py.)"""
from decimal import Decimal
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from execution import signal_proposals
from execution.models import ExecutionJob
from trade_copier import reconcile, services
from trade_copier.diff import ObservedPosition
from trade_copier.execution_producer import admit_partial_close, produce_partial_close_one, produce_pending_partial_closes
from trade_copier.models import CopierReplication, MasterEvent, ReplicationStatus
from trade_copier.observer import MasterReadResult
from trade_copier.tests_close_producer import CloseFixture

ARMED = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True, COPIER_EXECUTE_DEMO_ARMED=True)
ENABLED = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True)


def _fetch_pos(ticket, volume, symbol="XAUUSD"):
    def fetch(_account):
        return MasterReadResult(True, "ok", positions=(
            ObservedPosition(ticket=ticket, symbol=symbol, side="BUY", volume=volume),))
    return fetch


def _fetch_empty():
    def fetch(_account):
        return MasterReadResult(True, "ok", positions=())
    return fetch


class PartialFixture(CloseFixture):
    def _partial_event(self, *, before="1.00", after="0.60", ticket=1, seq=4):
        return MasterEvent.objects.create(
            master=self.master, sequence=seq, from_poll=self.poll,
            event_type=MasterEvent.EventType.PARTIAL_CLOSE, position_ticket=ticket, symbol="XAUUSD",
            side="BUY", volume_before=Decimal(before), volume_after=Decimal(after),
            volume_delta=Decimal(after) - Decimal(before), observed_at=timezone.now())

    def _partial_rep(self, follower, event):
        return CopierReplication.objects.create(
            master_event=event, follower=follower, requested_op="PARTIAL_CLOSE",
            status=ReplicationStatus.SHADOW_RECORDED, follower_symbol="XAUUSD", sltp_translation_ok=True)

    def _ready_partial(self, follower=None, *, ticket=777, before="1.00", after="0.60", instrument=True,
                       vmin=0.01, vstep=0.01):
        f = follower or self._follower()
        if instrument:
            from execution.models import BrokerInstrument
            BrokerInstrument.objects.create(account=f.follower_account, broker_symbol="XAUUSD",
                                            base_symbol="XAUUSD", enabled=True,
                                            metadata={"volume_min": vmin, "volume_step": vstep, "volume_max": 100})
        self._matched_open(f, ticket=ticket)
        return f, self._partial_rep(f, self._partial_event(before=before, after=after))


class AdmitPartialTests(PartialFixture):
    def test_no_verified_ticket_rejected(self):
        f = self._follower()
        from execution.models import BrokerInstrument
        BrokerInstrument.objects.create(account=f.follower_account, broker_symbol="XAUUSD", base_symbol="XAUUSD",
                                        enabled=True, metadata={"volume_min": 0.01, "volume_step": 0.01})
        rep = self._partial_rep(f, self._partial_event())      # no matched OPEN
        ok, ticket, red, rem, reason = admit_partial_close(rep, fetch=_fetch_pos(777, 1.0))
        self.assertEqual(reason, "no_verified_follower_ticket")

    def test_never_by_symbol(self):
        f = self._follower()
        from execution.models import BrokerInstrument
        BrokerInstrument.objects.create(account=f.follower_account, broker_symbol="XAUUSD", base_symbol="XAUUSD",
                                        enabled=True, metadata={"volume_min": 0.01, "volume_step": 0.01})
        other_open = MasterEvent.objects.create(
            master=self.master, sequence=9, from_poll=self.poll, event_type=MasterEvent.EventType.OPEN,
            position_ticket=2, symbol="XAUUSD", side="BUY", volume_before=Decimal("0"),
            volume_after=Decimal("1.0"), volume_delta=Decimal("1.0"), observed_at=timezone.now())
        self._matched_open(f, event=other_open, ticket=888)   # matched for ticket 2, same symbol
        rep = self._partial_rep(f, self._partial_event(ticket=1))
        ok, ticket, red, rem, reason = admit_partial_close(rep, fetch=_fetch_pos(888, 1.0))
        self.assertFalse(ok)
        self.assertEqual(reason, "no_verified_follower_ticket")

    def test_ticket_not_open_rejected(self):
        f, rep = self._ready_partial()
        ok, ticket, red, rem, reason = admit_partial_close(rep, fetch=_fetch_empty())
        self.assertEqual(reason, "follower_ticket_not_open")

    def test_symbol_changed_rejected(self):
        f, rep = self._ready_partial()
        ok, ticket, red, rem, reason = admit_partial_close(rep, fetch=_fetch_pos(777, 1.0, symbol="EURUSD"))
        self.assertEqual(reason, "symbol_changed")

    def test_constraints_not_authoritative_rejected(self):
        f, rep = self._ready_partial(instrument=False)         # no BrokerInstrument -> non-authoritative
        ok, ticket, red, rem, reason = admit_partial_close(rep, fetch=_fetch_pos(777, 1.0))
        self.assertEqual(reason, "constraints_not_authoritative")

    def test_zero_after_rounding_rejected(self):
        # current 0.01, ratio 0.4 -> raw 0.004 -> floor to 0
        f, rep = self._ready_partial()
        ok, ticket, red, rem, reason = admit_partial_close(rep, fetch=_fetch_pos(777, 0.01))
        self.assertEqual(reason, "rounds_to_zero")

    def test_below_min_reduction_rejected(self):
        # vmin 0.02, current 0.03, ratio 0.4 -> raw 0.012 -> floor 0.01 < vmin
        f, rep = self._ready_partial(vmin=0.02, vstep=0.01)
        ok, ticket, red, rem, reason = admit_partial_close(rep, fetch=_fetch_pos(777, 0.03))
        self.assertEqual(reason, "below_min_reduction")

    def test_remainder_below_min_rejected(self):
        # vmin 0.03, step 0.01, current 0.05, ratio 0.9 -> raw 0.045 -> floor 0.04; remaining 0.01 < vmin
        f, rep = self._ready_partial(before="1.00", after="0.10", vmin=0.03, vstep=0.01)
        ok, ticket, red, rem, reason = admit_partial_close(rep, fetch=_fetch_pos(777, 0.05))
        self.assertEqual(reason, "remainder_below_min")

    def test_disabled_follower_rejected(self):
        f, rep = self._ready_partial()
        services.set_follower_enabled(f, False)
        ok, ticket, red, rem, reason = admit_partial_close(rep, fetch=_fetch_pos(777, 1.0))
        self.assertEqual(reason, "follower_disabled")

    def test_live_follower_rejected(self):
        f = self._follower(number="2000091", live=True)
        f, rep = self._ready_partial(follower=f)
        ok, ticket, red, rem, reason = admit_partial_close(rep, fetch=_fetch_pos(777, 1.0))
        self.assertEqual(reason, "follower_not_demo")

    def test_happy_admit_proportional(self):
        f, rep = self._ready_partial(before="1.00", after="0.60")   # ratio 0.4
        ok, ticket, red, rem, reason = admit_partial_close(rep, fetch=_fetch_pos(777, 1.0))
        self.assertTrue(ok, reason)
        self.assertEqual(ticket, 777)
        self.assertAlmostEqual(red, 0.40)
        self.assertAlmostEqual(rem, 0.60)


class ProducePartialTests(PartialFixture):
    def test_flag_off_no_job(self):
        f, rep = self._ready_partial()
        out = produce_partial_close_one(rep, fetch=_fetch_pos(777, 1.0))
        self.assertFalse(out["produced"])
        self.assertEqual(out["reason"], "producer_disabled")
        self.assertEqual(ExecutionJob.objects.count(), 0)

    @override_settings(**ENABLED)
    def test_shadow_records_no_job(self):
        f, rep = self._ready_partial()
        out = produce_partial_close_one(rep, fetch=_fetch_pos(777, 1.0))
        self.assertTrue(out["produced"], out)
        self.assertIsNone(out["job_id"])
        self.assertEqual(ExecutionJob.objects.count(), 0)
        rep.refresh_from_db()
        self.assertEqual(rep.status, ReplicationStatus.SHADOW_JOB_RECORDED)
        self.assertEqual(rep.follower_ticket, 777)
        self.assertAlmostEqual(float(rep.target_volume), 0.40)

    @override_settings(**ARMED)
    def test_armed_creates_partial_close_trade(self):
        f, rep = self._ready_partial()
        out = produce_partial_close_one(rep, fetch=_fetch_pos(777, 1.0))
        self.assertTrue(out["produced"], out)
        job = ExecutionJob.objects.get()
        self.assertEqual(job.job_type, ExecutionJob.JobType.PARTIAL_CLOSE_TRADE)
        self.assertEqual(job.payload["ticket"], 777)
        self.assertAlmostEqual(float(job.payload["volume"]), 0.40)
        self.assertEqual(job.payload["signal_source"], "copier")
        rep.refresh_from_db()
        self.assertEqual(rep.status, ReplicationStatus.EXECUTION_REQUESTED)

    @override_settings(**ARMED)
    def test_idempotent_no_double_job(self):
        f, rep = self._ready_partial()
        produce_partial_close_one(rep, fetch=_fetch_pos(777, 1.0))
        produce_partial_close_one(rep, fetch=_fetch_pos(777, 1.0))
        self.assertEqual(ExecutionJob.objects.count(), 1)

    @override_settings(**ARMED)
    def test_dedup_preexisting_partial_close(self):
        f, rep = self._ready_partial()
        ExecutionJob.objects.create(account=f.follower_account,
                                    job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE,
                                    status=ExecutionJob.Status.PENDING, payload={"ticket": 777})
        out = produce_partial_close_one(rep, fetch=_fetch_pos(777, 1.0))
        self.assertFalse(out["produced"])
        self.assertEqual(out["reason"], "partial_close_already_in_flight")

    @override_settings(**ARMED)
    def test_kill_switch_does_not_block_partial_close(self):
        f, rep = self._ready_partial()
        signal_proposals.engage_kill_switch(actor=self.user, reason="test")
        out = produce_partial_close_one(rep, fetch=_fetch_pos(777, 1.0))
        self.assertTrue(out["produced"], out)             # risk-reducing -> not blocked
        self.assertEqual(ExecutionJob.objects.filter(
            job_type=ExecutionJob.JobType.PARTIAL_CLOSE_TRADE).count(), 1)

    @override_settings(**ENABLED)
    def test_produce_pending_partial_closes_batch(self):
        self._ready_partial()
        summary = produce_pending_partial_closes(fetch=_fetch_pos(777, 1.0))
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["produced"], 1)

    @override_settings(**ARMED)
    def test_stale_arming_flip_between_preflight_and_lock_creates_no_job(self):
        """P1 (discriminating) — arming reads TRUE at the preflight but FALSE at the in-lock decision
        (side_effect flip). The in-lock value must govern -> SHADOW + NO job. A producer that used the
        pre-lock value would create a mutation-capable job here and FAIL this test."""
        f, rep = self._ready_partial()
        with mock.patch("trade_copier.execution_producer.copier_execute_demo_armed",
                        side_effect=[True, False]) as m_armed:
            out = produce_partial_close_one(rep, fetch=_fetch_pos(777, 1.0))
        self.assertEqual(m_armed.call_count, 2)            # preflight hint + authoritative in-lock re-read
        self.assertTrue(out["produced"], out)
        self.assertIsNone(out["job_id"])
        self.assertFalse(out["armed"])
        self.assertEqual(ExecutionJob.objects.count(), 0)
        rep.refresh_from_db()
        self.assertEqual(rep.status, ReplicationStatus.SHADOW_JOB_RECORDED)


class ReconcilePartialTests(PartialFixture):
    def _produced_partial(self, follower, *, ticket=777):
        return CopierReplication.objects.create(
            master_event=self._partial_event(), follower=follower, requested_op="PARTIAL_CLOSE",
            status=ReplicationStatus.EXECUTION_REQUESTED, follower_symbol="XAUUSD",
            follower_ticket=ticket, target_volume=Decimal("0.40"))

    def test_reconciled_within_tolerance(self):
        f = self._follower()
        rep = self._produced_partial(f)
        out = reconcile.reconcile_partial_close(rep, 0.60, step=0.01, fetch=_fetch_pos(777, 0.60))
        self.assertEqual(out["status"], reconcile.PARTIAL_RECONCILED)
        rep.refresh_from_db()
        self.assertEqual(rep.reconciliation_status, reconcile.PARTIAL_RECONCILED)

    def test_divergent_volume(self):
        f = self._follower()
        rep = self._produced_partial(f)
        out = reconcile.reconcile_partial_close(rep, 0.60, step=0.01, fetch=_fetch_pos(777, 0.50))
        self.assertEqual(out["status"], reconcile.PARTIAL_DIVERGENT_VOLUME)
        self.assertEqual(out["actual_remaining"], 0.50)

    def test_ticket_gone_is_divergence_not_full_close(self):
        f = self._follower()
        rep = self._produced_partial(f)
        out = reconcile.reconcile_partial_close(rep, 0.60, step=0.01, fetch=_fetch_empty())
        self.assertEqual(out["status"], reconcile.PARTIAL_DIVERGENT_TICKET_GONE)   # NOT a silent full close
        rep.refresh_from_db()
        self.assertEqual(rep.reconciliation_status, reconcile.PARTIAL_DIVERGENT_TICKET_GONE)

    def test_read_refused_is_unavailable_nonterminal(self):
        def _refused(_a):
            from trade_copier.observer import MasterReadResult
            return MasterReadResult(False, "snapshot_identity_login_mismatch")
        f = self._follower()
        rep = self._produced_partial(f)
        out = reconcile.reconcile_partial_close(rep, 0.60, step=0.01, fetch=_refused)
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], reconcile.PARTIAL_RECONCILE_UNAVAILABLE)
