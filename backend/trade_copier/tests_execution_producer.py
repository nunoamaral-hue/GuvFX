"""B-exec producer certification: flag gate, TOCTOU re-validation, payload contract, protection facts,
SHADOW semantics, idempotency, inherited gates. DEMO-only; no order_send; no LIVE."""
import threading
from unittest import mock
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from execution.models import BrokerInstrument, ExecutionJob
from execution import signal_proposals
from trade_copier import diff, services
from trade_copier.execution_producer import admit_open, produce_one, produce_pending
from trade_copier.models import (
    CopierMaster,
    CopierReplication,
    MasterEvent,
    MasterObservationPoll,
    MasterPositionSnapshot,
    ReplicationStatus,
)
from trading.models import BrokerServer, TradingAccount

User = get_user_model()

ARMED = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True, COPIER_EXECUTE_DEMO_ARMED=True)
ENABLED = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True)


class ProducerFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner", email="o@x.invalid", password="x")
        self.bs = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6Technologies-Demo",
                                              environment="demo")
        self.master_acct = TradingAccount.objects.create(
            user=self.user, name="m", broker_name="IS6", account_number="1000001",
            is_demo=True, is_active=True, broker_server=self.bs)
        self.master = CopierMaster.objects.create(trading_account=self.master_acct, observe_enabled=True)
        self.poll = MasterObservationPoll.objects.create(master=self.master, observed_at=timezone.now(),
                                                         read_at=timezone.now(), position_count=1)
        self.event = MasterEvent.objects.create(
            master=self.master, sequence=1, from_poll=self.poll, event_type=MasterEvent.EventType.OPEN,
            position_ticket=1, symbol="XAUUSD", side="BUY", volume_before=Decimal("0"),
            volume_after=Decimal("0.10"), volume_delta=Decimal("0.10"), observed_at=timezone.now())
        # Authoritative same-poll master snapshot: complete, directionally-valid SL+TP for a BUY.
        self.snap = MasterPositionSnapshot.objects.create(
            poll=self.poll, position_ticket=1, symbol="XAUUSD", side="BUY", volume=Decimal("0.10"),
            sl=Decimal("1990"), tp=Decimal("2100"), price_open=Decimal("2000"))

    def _follower(self, number="2000001", *, live=False, cross_broker=False, **cfg):
        # DEMO followers share the master's broker server (same-broker -> symbol resolves to identity),
        # so the F2 produce-time symbol-map re-verification passes without a CopierSymbolMap. A LIVE or
        # explicit cross-broker follower gets its own broker server.
        if live or cross_broker:
            fbs = BrokerServer.objects.create(
                broker_display_name="IS6", server_name=("TradersWay-Live" if live else f"Pepperstone-{number}"),
                environment=("live" if live else "demo"))
        else:
            fbs = self.bs
        facct = TradingAccount.objects.create(
            user=self.user, name=f"f{number}", broker_name="IS6", account_number=number,
            is_demo=(not live), is_active=True, broker_server=fbs)
        cfg.setdefault("multiplier", Decimal("0.5"))
        f = services.add_follower(self.master, facct, **cfg)
        services.set_follower_enabled(f, True)
        return f

    def _instrument(self, account, symbol="XAUUSD", authoritative=True, enabled=True):
        meta = {"volume_min": 0.01, "volume_step": 0.01, "volume_max": 100} if authoritative else {"digits": 2}
        return BrokerInstrument.objects.create(account=account, broker_symbol=symbol, base_symbol=symbol,
                                               enabled=enabled, metadata=meta)

    def _replication(self, follower, *, op="OPEN", sltp_ok=True, symbol="XAUUSD", vol="0.050",
                     sl="1990", tp="2100"):
        return CopierReplication.objects.create(
            master_event=self.event, follower=follower, requested_op=op,
            status=ReplicationStatus.SHADOW_RECORDED, target_volume=Decimal(vol), follower_symbol=symbol,
            translated_sl=(Decimal(sl) if sl else None), translated_tp=(Decimal(tp) if tp else None),
            constraints_authoritative=False, sltp_translation_ok=sltp_ok)

    def _ready(self, **cfg):
        """A fully-admissible DEMO follower + instrument + OPEN intent."""
        f = self._follower(**cfg)
        self._instrument(f.follower_account)
        return f, self._replication(f)


class AdmissionTests(ProducerFixture):
    def test_live_follower_rejected(self):
        f = self._follower(live=True)
        self._instrument(f.follower_account)
        self.assertEqual(admit_open(self._replication(f))[1], "follower_not_demo")

    def test_non_authoritative_constraints_rejected(self):
        f = self._follower()
        self._instrument(f.follower_account, authoritative=False)
        self.assertEqual(admit_open(self._replication(f))[1], "constraints_not_authoritative")

    def test_sltp_incomplete_rejected(self):
        f, rep = self._ready()
        rep.sltp_translation_ok = False
        rep.save(update_fields=["sltp_translation_ok"])
        self.assertEqual(admit_open(rep)[1], "sltp_incomplete")

    def test_non_open_op_rejected(self):
        f = self._follower()
        self._instrument(f.follower_account)
        self.assertEqual(admit_open(self._replication(f, op="MODIFY_SLTP"))[1], "op_not_producible")

    def test_disabled_follower_rejected(self):
        f, rep = self._ready()
        services.set_follower_enabled(f, False)
        self.assertEqual(admit_open(rep)[1], "follower_disabled")

    # P0-D — protection facts
    def test_incomplete_protection_rejected(self):
        f, rep = self._ready()
        self.snap.tp = None
        MasterPositionSnapshot.objects.filter(pk=self.snap.pk).update(tp=None)  # SL only, no TP
        self.assertEqual(admit_open(rep)[1], "incomplete_protection")

    def test_non_directional_protection_rejected(self):
        f, rep = self._ready()
        MasterPositionSnapshot.objects.filter(pk=self.snap.pk).update(sl=Decimal("2100"), tp=Decimal("1990"))
        self.assertEqual(admit_open(rep)[1], "protection_not_directional")   # BUY with SL above entry

    # P0-B — staleness
    def test_open_superseded_by_close_rejected(self):
        f, rep = self._ready()
        MasterEvent.objects.create(
            master=self.master, sequence=2, from_poll=self.poll, event_type=MasterEvent.EventType.FULL_CLOSE,
            position_ticket=1, symbol="XAUUSD", side="BUY", volume_before=Decimal("0.10"),
            volume_after=Decimal("0"), volume_delta=Decimal("-0.10"), observed_at=timezone.now())
        self.assertEqual(admit_open(rep)[1], "open_superseded_by_close")

    def test_stale_open_outside_window_rejected(self):
        f, rep = self._ready()
        MasterEvent.objects.filter(pk=self.event.pk).update(observed_at=timezone.now() - timedelta(hours=2))
        rep = CopierReplication.objects.select_related("master_event").get(pk=rep.pk)  # reload the event
        self.assertEqual(admit_open(rep)[1], "stale_open_outside_window")

    # F3 — instrument readiness
    def test_instrument_not_ready_rejected(self):
        f = self._follower()
        self._instrument(f.follower_account, enabled=False)   # authoritative metadata but DISABLED
        self.assertEqual(admit_open(self._replication(f))[1], "instrument_not_ready")

    # F2 — symbol-map revocation / cross-broker re-verification at produce time
    def test_cross_broker_without_verified_map_rejected_at_produce(self):
        f = self._follower(number="2000050", cross_broker=True)   # different broker, no CopierSymbolMap
        self._instrument(f.follower_account)
        self.assertEqual(admit_open(self._replication(f))[1], "symbol_map_changed")


class ProductionTests(ProducerFixture):
    # P0-A — flag bypass
    def test_flag_off_direct_produce_one_no_job(self):
        f, rep = self._ready()
        out = produce_one(rep)   # direct call, flags OFF
        self.assertFalse(out["produced"])
        self.assertEqual(out["reason"], "producer_disabled")
        self.assertEqual(ExecutionJob.objects.count(), 0)
        rep.refresh_from_db()
        self.assertEqual(rep.status, ReplicationStatus.SHADOW_RECORDED)   # unchanged
        self.assertIsNone(rep.execution_job_id)

    def test_flag_off_produce_pending_inert(self):
        self._ready()
        summary = produce_pending()
        self.assertFalse(summary["enabled"])
        self.assertEqual(ExecutionJob.objects.count(), 0)

    # P1 — SHADOW semantics
    @override_settings(**ENABLED)
    def test_enabled_not_armed_sets_shadow_job_recorded_not_execution_requested(self):
        f, rep = self._ready()
        out = produce_one(rep)
        self.assertTrue(out["produced"], out)
        self.assertEqual(out["job_type"], ExecutionJob.JobType.PLACE_ORDER_SHADOW)
        rep.refresh_from_db()
        self.assertEqual(rep.status, ReplicationStatus.SHADOW_JOB_RECORDED)   # NOT EXECUTION_REQUESTED
        job = ExecutionJob.objects.get()
        self.assertEqual(job.payload["execution_mode"], "SHADOW")
        self.assertTrue(job.payload["is_demo"])

    @override_settings(**ARMED)
    def test_armed_sets_execution_requested_real_place_order(self):
        f, rep = self._ready()
        out = produce_one(rep)
        self.assertEqual(out["job_type"], ExecutionJob.JobType.PLACE_ORDER)
        rep.refresh_from_db()
        self.assertEqual(rep.status, ReplicationStatus.EXECUTION_REQUESTED)
        self.assertEqual(ExecutionJob.objects.get().payload["execution_mode"], "DEMO")

    # P0-C — payload contract
    @override_settings(**ENABLED)
    def test_payload_contract_numeric_lots_cap_and_provenance(self):
        f, rep = self._ready(max_volume=Decimal("1.0"))
        produce_one(rep)
        p = ExecutionJob.objects.get().payload
        self.assertGreater(float(p["lots"]), 0)              # numeric, positive
        self.assertEqual(p["signal_source"], "copier")
        self.assertEqual(p["comment"], f"CPY{rep.id}")       # provenance
        self.assertIsNotNone(p["max_lot"])                   # follower/source cap present
        rep.refresh_from_db()
        self.assertEqual(float(p["lots"]), float(rep.target_volume))   # enqueued == recorded (no silent shrink)

    @override_settings(**ENABLED)
    def test_volume_clamp_rejected_not_silently_shrunk(self):
        # multiplier 100 on a 0.10 master = 10.0, but follower max_volume 0.5 -> would clamp -> REJECT.
        f, rep = self._ready(multiplier=Decimal("100"), max_volume=Decimal("0.5"))
        out = produce_one(rep)
        self.assertFalse(out["produced"])
        self.assertEqual(out["reason"], "volume_would_be_clamped")
        self.assertEqual(ExecutionJob.objects.count(), 0)

    # idempotency + inherited gate
    @override_settings(**ENABLED)
    def test_idempotent_no_double_job(self):
        f, rep = self._ready()
        produce_one(rep)
        produce_one(rep)
        self.assertEqual(ExecutionJob.objects.count(), 1)

    @override_settings(**ARMED)
    def test_stale_arming_flip_produces_shadow_not_real(self):
        """Cross-producer hardening — arming TRUE at preflight, FALSE at the in-lock decision => a SUPPRESSED
        PLACE_ORDER_SHADOW job, never a real PLACE_ORDER. A pre-lock-armed producer would fail this."""
        f, rep = self._ready()
        with mock.patch("trade_copier.execution_producer.copier_execute_demo_armed",
                        side_effect=[True, False]) as m:
            out = produce_one(rep)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(out["job_type"], ExecutionJob.JobType.PLACE_ORDER_SHADOW)
        self.assertEqual(ExecutionJob.objects.get().job_type, ExecutionJob.JobType.PLACE_ORDER_SHADOW)
        rep.refresh_from_db()
        self.assertEqual(rep.status, ReplicationStatus.SHADOW_JOB_RECORDED)

    @override_settings(**ARMED)
    def test_kill_switch_blocks_production_inherited(self):
        f, rep = self._ready()
        signal_proposals.engage_kill_switch(actor=self.user, reason="test")
        out = produce_one(rep)
        self.assertFalse(out["produced"])
        self.assertIn("gate:", out["reason"])
        self.assertEqual(ExecutionJob.objects.count(), 0)
        rep.refresh_from_db()
        self.assertIsNone(rep.execution_job_id)

    @override_settings(**ENABLED)
    def test_live_follower_not_produced_via_pending(self):
        f = self._follower(number="2000009", live=True)
        self._instrument(f.follower_account)
        self._replication(f)
        self.assertEqual(produce_pending()["produced"], 0)
        self.assertEqual(ExecutionJob.objects.count(), 0)

    # F4 — real serialization contract (ExecutionJobSerializer round-trip)
    @override_settings(**ENABLED)
    def test_job_serialization_contract(self):
        from execution.serializers import ExecutionJobSerializer
        f, rep = self._ready()
        produce_one(rep)
        data = ExecutionJobSerializer(ExecutionJob.objects.get()).data
        self.assertEqual(data["job_type"], ExecutionJob.JobType.PLACE_ORDER_SHADOW)
        p = data["payload"]
        self.assertGreater(float(p["lots"]), 0)        # serialized lots is a parseable positive number
        for key in ("symbol", "side", "is_demo", "max_lot", "comment", "signal_source"):
            self.assertIn(key, p)


class ConcurrentProducerTests(TransactionTestCase):
    """F1/P0-B — a genuine two-connection concurrent produce proves the row lock yields ONE job."""

    def _fixture(self):
        user = User.objects.create_user(username="c", email="c@x.invalid", password="x")
        bs = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6Technologies-Demo",
                                         environment="demo")
        macct = TradingAccount.objects.create(user=user, name="m", broker_name="IS6",
                                              account_number="1000001", is_demo=True, is_active=True,
                                              broker_server=bs)
        master = CopierMaster.objects.create(trading_account=macct, observe_enabled=True)
        poll = MasterObservationPoll.objects.create(master=master, observed_at=timezone.now(),
                                                    read_at=timezone.now(), position_count=1)
        event = MasterEvent.objects.create(
            master=master, sequence=1, from_poll=poll, event_type=MasterEvent.EventType.OPEN,
            position_ticket=1, symbol="XAUUSD", side="BUY", volume_before=Decimal("0"),
            volume_after=Decimal("0.10"), volume_delta=Decimal("0.10"), observed_at=timezone.now())
        MasterPositionSnapshot.objects.create(poll=poll, position_ticket=1, symbol="XAUUSD", side="BUY",
                                              volume=Decimal("0.10"), sl=Decimal("1990"), tp=Decimal("2100"),
                                              price_open=Decimal("2000"))
        facct = TradingAccount.objects.create(user=user, name="f", broker_name="IS6",
                                             account_number="2000001", is_demo=True, is_active=True,
                                             broker_server=bs)
        BrokerInstrument.objects.create(account=facct, broker_symbol="XAUUSD", base_symbol="XAUUSD",
                                        enabled=True, metadata={"volume_min": 0.01, "volume_step": 0.01})
        f = services.add_follower(master, facct, multiplier=Decimal("0.5"))
        services.set_follower_enabled(f, True)
        return CopierReplication.objects.create(
            master_event=event, follower=f, requested_op="OPEN", status=ReplicationStatus.SHADOW_RECORDED,
            target_volume=Decimal("0.050"), follower_symbol="XAUUSD", translated_sl=Decimal("1990"),
            translated_tp=Decimal("2100"), sltp_translation_ok=True)

    @override_settings(**ENABLED)
    def test_concurrent_produce_one_creates_single_job(self):
        rep_pk = self._fixture().pk
        results = []

        def worker():
            try:
                results.append(produce_one(CopierReplication.objects.get(pk=rep_pk)))
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(ExecutionJob.objects.count(), 1)              # row lock -> exactly one job
        self.assertEqual(sum(1 for r in results if r.get("produced")), 1)
