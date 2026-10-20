"""B-exec producer certification: DEMO-only, SHADOW-by-default, fresh admission, idempotent, gate-inheriting."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution.models import BrokerInstrument, ExecutionJob
from execution import signal_proposals
from trade_copier import services
from trade_copier.execution_producer import admit_open, produce_one, produce_pending
from trade_copier.models import (
    CopierMaster,
    CopierReplication,
    MasterEvent,
    MasterObservationPoll,
    ReplicationStatus,
)
from trading.models import BrokerServer, TradingAccount

User = get_user_model()

ARMED = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True, COPIER_EXECUTE_DEMO_ARMED=True)
ENABLED = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True)


class ProducerFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner", email="o@x.invalid", password="x")
        bs = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6Technologies-Demo",
                                         environment="demo")
        self.master_acct = TradingAccount.objects.create(
            user=self.user, name="m", broker_name="IS6", account_number="1000001",
            is_demo=True, is_active=True, broker_server=bs)
        self.master = CopierMaster.objects.create(trading_account=self.master_acct, observe_enabled=True)
        self.poll = MasterObservationPoll.objects.create(master=self.master, observed_at=timezone.now(),
                                                         read_at=timezone.now(), position_count=1)
        self.event = MasterEvent.objects.create(
            master=self.master, sequence=1, from_poll=self.poll, event_type=MasterEvent.EventType.OPEN,
            position_ticket=1, symbol="XAUUSD", side="BUY", volume_before=Decimal("0"),
            volume_after=Decimal("0.10"), volume_delta=Decimal("0.10"), observed_at=timezone.now())

    def _follower(self, number="2000001", *, live=False):
        env = "live" if live else "demo"
        srv = "TradersWay-Live" if live else f"IS6Technologies-Demo{number}"
        fbs = BrokerServer.objects.create(broker_display_name="IS6", server_name=srv, environment=env)
        facct = TradingAccount.objects.create(
            user=self.user, name=f"f{number}", broker_name="IS6", account_number=number,
            is_demo=(not live), is_active=True, broker_server=fbs)
        f = services.add_follower(self.master, facct, multiplier=Decimal("0.5"))
        services.set_follower_enabled(f, True)
        return f

    def _instrument(self, account, symbol="XAUUSD", authoritative=True):
        meta = {"volume_min": 0.01, "volume_step": 0.01, "volume_max": 100} if authoritative else {"digits": 2}
        return BrokerInstrument.objects.create(account=account, broker_symbol=symbol, base_symbol=symbol,
                                               enabled=True, metadata=meta)

    def _replication(self, follower, *, op="OPEN", sltp_ok=True, symbol="XAUUSD", vol="0.050"):
        return CopierReplication.objects.create(
            master_event=self.event, follower=follower, requested_op=op,
            status=ReplicationStatus.SHADOW_RECORDED, target_volume=Decimal(vol), follower_symbol=symbol,
            translated_sl=Decimal("1990"), translated_tp=Decimal("2100"),
            constraints_authoritative=False, sltp_translation_ok=sltp_ok)


class AdmissionTests(ProducerFixture):
    def test_live_follower_rejected(self):
        f = self._follower(live=True)
        self._instrument(f.follower_account)
        rep = self._replication(f)
        ok, reason = admit_open(rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "follower_not_demo")

    def test_non_authoritative_constraints_rejected(self):
        f = self._follower()
        self._instrument(f.follower_account, authoritative=False)   # metadata lacks volume bounds
        rep = self._replication(f)
        ok, reason = admit_open(rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "constraints_not_authoritative")

    def test_sltp_incomplete_rejected(self):
        f = self._follower()
        self._instrument(f.follower_account)
        rep = self._replication(f, sltp_ok=False)
        ok, reason = admit_open(rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "sltp_incomplete")

    def test_non_open_op_rejected(self):
        f = self._follower()
        self._instrument(f.follower_account)
        rep = self._replication(f, op="MODIFY_SLTP")
        ok, reason = admit_open(rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "op_not_producible")

    def test_disabled_follower_rejected(self):
        f = self._follower()
        services.set_follower_enabled(f, False)
        self._instrument(f.follower_account)
        rep = self._replication(f)
        ok, reason = admit_open(rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "follower_disabled")


class ProductionTests(ProducerFixture):
    def test_flag_off_produces_nothing(self):
        f = self._follower()
        self._instrument(f.follower_account)
        self._replication(f)
        summary = produce_pending()
        self.assertFalse(summary["enabled"])
        self.assertEqual(ExecutionJob.objects.count(), 0)

    @override_settings(**ENABLED)
    def test_enabled_not_armed_produces_shadow_job(self):
        f = self._follower()
        self._instrument(f.follower_account)
        rep = self._replication(f)
        out = produce_one(rep)
        self.assertTrue(out["produced"], out)
        self.assertEqual(out["job_type"], ExecutionJob.JobType.PLACE_ORDER_SHADOW)   # suppressed, no order
        self.assertFalse(out["armed"])
        job = ExecutionJob.objects.get()
        self.assertEqual(job.job_type, ExecutionJob.JobType.PLACE_ORDER_SHADOW)
        self.assertEqual(job.payload["execution_mode"], "SHADOW")
        self.assertTrue(job.payload["is_demo"])
        rep.refresh_from_db()
        self.assertEqual(rep.status, ReplicationStatus.EXECUTION_REQUESTED)
        self.assertEqual(rep.execution_job_id, job.id)

    @override_settings(**ARMED)
    def test_armed_produces_real_demo_place_order(self):
        f = self._follower()
        self._instrument(f.follower_account)
        rep = self._replication(f)
        out = produce_one(rep)
        self.assertTrue(out["produced"], out)
        self.assertEqual(out["job_type"], ExecutionJob.JobType.PLACE_ORDER)
        self.assertEqual(ExecutionJob.objects.get().payload["execution_mode"], "DEMO")

    @override_settings(**ENABLED)
    def test_idempotent_no_double_job(self):
        f = self._follower()
        self._instrument(f.follower_account)
        rep = self._replication(f)
        produce_one(rep)
        produce_one(rep)
        self.assertEqual(ExecutionJob.objects.count(), 1)

    @override_settings(**ARMED)
    def test_kill_switch_blocks_production_inherited(self):
        f = self._follower()
        self._instrument(f.follower_account)
        rep = self._replication(f)
        signal_proposals.engage_kill_switch(actor=self.user, reason="test")
        out = produce_one(rep)
        self.assertFalse(out["produced"])
        self.assertIn("gate:", out["reason"])
        self.assertEqual(ExecutionJob.objects.count(), 0)   # inherited kill-switch gate blocked creation
        rep.refresh_from_db()
        self.assertIsNone(rep.execution_job_id)

    @override_settings(**ENABLED)
    def test_live_follower_not_produced_via_pending(self):
        f = self._follower(live=True)
        self._instrument(f.follower_account)
        self._replication(f)
        summary = produce_pending()
        self.assertEqual(summary["produced"], 0)
        self.assertEqual(ExecutionJob.objects.count(), 0)
