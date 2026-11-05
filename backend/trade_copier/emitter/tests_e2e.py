"""ADR-0076 — emitter end-to-end (synthetic): EmitterApp.observe_once → signed envelope → HTTP ingest →
immutable revision → event projection → authoritative MasterEvent → lifecycle. Proves a stale OPEN (old
broker open_time carried by the emitter) is ruled ENTRY_FAILED_FINAL (no late entry), and ZERO ExecutionJob is
created throughout. No broker, no real MT5 — the transport posts to the Django test client."""
import json
import os
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution.models import ExecutionJob
from trade_copier import bridge_emitter as be, services
from trade_copier.emitter.app import EmitterApp
from trade_copier.emitter.config import load_config
from trade_copier.emitter.mt5_adapter import FakeSnapshotAdapter
from trade_copier.event_projection import run_event_projection_pass
from trade_copier.lifecycle import LifecyclePolicy, LifecycleState, run_lifecycle_pass
from trade_copier.models import CopierIngestedRevision, CopierMaster, CopierPositionLifecycle, MasterEvent
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"
ON = dict(COPIER_ENABLED=True, COPIER_EVENT_INGESTION_ENABLED=True, COPIER_EVENT_PROJECTION_ENABLED=True,
          COPIER_LIFECYCLE_ENABLED=True, SECURE_SSL_REDIRECT=False)


@override_settings(**ON)
class EmitterEndToEndTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.bs = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6Technologies-Demo",
                                              environment="demo")
        # The emitter config refuses reserved account ids (<= 1); ensure the master's PK is > 1 regardless of
        # the test-DB sequence by consuming an id with a filler account first.
        TradingAccount.objects.create(user=self.user, name="filler", broker_name="IS6", account_number="999001",
                                      is_demo=True, is_active=False, broker_server=self.bs)
        self.macct = TradingAccount.objects.create(user=self.user, name="m", broker_name="IS6",
                                                   account_number="1000001", is_demo=True, is_active=True,
                                                   broker_server=self.bs)
        self.master = CopierMaster.objects.create(trading_account=self.macct, observe_enabled=True)
        facct = TradingAccount.objects.create(user=self.user, name="f", broker_name="IS6",
                                              account_number="2000001", is_demo=True, is_active=True,
                                              broker_server=self.bs)
        services.set_follower_enabled(services.add_follower(self.master, facct, multiplier="0.5"), True)

    def _transport(self):
        def send(env):
            r = self.client.post("/api/copy-trading/event-ingest/", data=json.dumps(env),
                                 content_type="application/json")
            return be.SendResult(ok=(r.status_code == 200), status=r.status_code)
        return send

    def _app(self, positions):
        cfg = load_config(dict(account_id=self.macct.id, broker_login="1000001",
                               broker_server="IS6Technologies-Demo", host_id="host-A",
                               bridge_base_url="http://127.0.0.1:8806",
                               ingest_url="http://testserver/api/copy-trading/event-ingest/",
                               build_sha="fb4ee402", terminal_fingerprint="tfp", bridge_id="127.0.0.1:8806",
                               observation_allowed="1", delivery_allowed="1"))
        return EmitterApp(cfg, adapter=FakeSnapshotAdapter(positions=positions, account_server="IS6Technologies-Demo"),
                          transport=self._transport(), env={"COPIER_INGEST_HMAC_KEY": SECRET}, spool=be.MemorySpool())

    def test_stale_open_no_late_entry_and_zero_execution_job(self):
        old = timezone.now() - timedelta(seconds=10_000)
        pos = be.ObservedMasterPosition(ticket=777, symbol="XAUUSD", side="BUY", volume=0.1, open_time=old)
        with mock.patch.dict(os.environ, {"COPIER_INGEST_HMAC_KEY": SECRET}):
            out = self._app([pos]).observe_once()
        self.assertTrue(out["ok"], out)
        self.assertEqual(CopierIngestedRevision.objects.filter(ticket=777).count(), 1)
        run_event_projection_pass()
        ev = MasterEvent.objects.get(position_ticket=777)
        self.assertEqual(ev.source, "EVENT")
        self.assertEqual(ev.broker_event_time, old)                 # true broker open time carried end to end
        run_lifecycle_pass(policy=LifecyclePolicy())
        lc = CopierPositionLifecycle.objects.get(master=self.master, master_position_ticket=777)
        self.assertEqual(lc.state, LifecycleState.ENTRY_FAILED_FINAL.value)   # no late entry
        self.assertEqual(ExecutionJob.objects.count(), 0)           # zero execution surface throughout

    def test_fresh_open_is_observed_open_still_zero_jobs(self):
        pos = be.ObservedMasterPosition(ticket=778, symbol="XAUUSD", side="BUY", volume=0.1,
                                        open_time=timezone.now())
        with mock.patch.dict(os.environ, {"COPIER_INGEST_HMAC_KEY": SECRET}):
            self._app([pos]).observe_once()
        run_event_projection_pass()
        run_lifecycle_pass(policy=LifecyclePolicy())
        lc = CopierPositionLifecycle.objects.get(master=self.master, master_position_ticket=778)
        self.assertEqual(lc.state, LifecycleState.OBSERVED_OPEN.value)
        self.assertEqual(ExecutionJob.objects.count(), 0)
