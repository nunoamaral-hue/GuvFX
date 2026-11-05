"""ADR-0074 Phase-10 — capacity/latency measurement across the complete synthetic emitter→dispatch path.

Records p50/p95/p99 for detection/emission, ingestion, lifecycle processing, and TOTAL emitter→SHADOW-dispatch
latency. These are EVIDENCE (printed), not a production promise: the default cadence is unchanged and sub-second
production performance is NOT asserted. The assertions are deliberately loose (finite + a generous ceiling) so
the suite never flakes on a shared CI box. No broker, no ExecutionJob.
"""
import json
import os
import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from execution.models import ExecutionJob
from trade_copier import bridge_emitter as be, services
from trade_copier.event_projection import run_event_projection_pass
from trade_copier.lifecycle import LifecyclePolicy, run_lifecycle_pass
from trade_copier.lifecycle_dispatch import run_lifecycle_dispatch_pass
from trade_copier.models import CopierLifecycleDispatch, CopierMaster
from trade_copier.replication import replicate_pending
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"
POLICY = LifecyclePolicy()
N = 20
ALL_ON = dict(
    COPIER_ENABLED=True, COPIER_EVENT_INGESTION_ENABLED=True, COPIER_EVENT_PROJECTION_ENABLED=True,
    COPIER_LIFECYCLE_ENABLED=True, COPIER_LIFECYCLE_DISPATCH_ENABLED=True, SECURE_SSL_REDIRECT=False,
)


def _pct(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


@override_settings(**ALL_ON)
class HybridCapacityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.bs = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6Technologies-Demo",
                                              environment="demo")
        self.macct = TradingAccount.objects.create(user=self.user, name="m", broker_name="IS6",
                                                   account_number="1000001", is_demo=True, is_active=True,
                                                   broker_server=self.bs)
        self.master = CopierMaster.objects.create(trading_account=self.macct, observe_enabled=True)
        facct = TradingAccount.objects.create(user=self.user, name="f", broker_name="IS6",
                                              account_number="2000001", is_demo=True, is_active=True,
                                              broker_server=self.bs)
        services.set_follower_enabled(services.add_follower(self.master, facct, multiplier="0.5"), True)
        self.emitter = be.BridgeEmitter(
            account_id=self.macct.id, spool=be.MemorySpool(), key_provider=lambda: SECRET,
            transport=self._transport(), enabled=True,
            broker_login="1000001", broker_server="IS6Technologies-Demo")

    def _transport(self):
        def send(env):
            r = self.client.post("/api/copy-trading/event-ingest/", data=json.dumps(env),
                                 content_type="application/json")
            return be.SendResult(ok=(r.status_code == 200), status=r.status_code)
        return send

    def test_emitter_to_dispatch_latency_profile(self):
        emit, ingest, lifecycle, total = [], [], [], []
        with mock.patch.dict(os.environ, {"COPIER_INGEST_HMAC_KEY": SECRET}):
            for i in range(N):
                pos = be.ObservedMasterPosition(ticket=1000 + i, symbol="XAUUSD", side="BUY", volume=0.1)
                t0 = time.perf_counter()
                self.emitter.observe([pos])
                t1 = time.perf_counter()
                self.emitter.flush()                 # detection/emission + ingestion (HTTP verify+persist)
                t2 = time.perf_counter()
                run_event_projection_pass()
                replicate_pending()
                run_lifecycle_pass(policy=POLICY)
                run_lifecycle_dispatch_pass()        # → SHADOW dispatch rows
                t3 = time.perf_counter()
                emit.append((t1 - t0) * 1000.0)
                ingest.append((t2 - t1) * 1000.0)
                lifecycle.append((t3 - t2) * 1000.0)
                total.append((t3 - t0) * 1000.0)

        profile = {k: {"p50": round(_pct(v, 50), 2), "p95": round(_pct(v, 95), 2), "p99": round(_pct(v, 99), 2)}
                   for k, v in (("detection_emission_ms", emit), ("ingestion_ms", ingest),
                                ("lifecycle_ms", lifecycle), ("total_emitter_to_dispatch_ms", total))}
        print("\nADR-0074 hybrid capacity profile (n={}): {}".format(N, json.dumps(profile)))

        # Evidence, not a production promise: just prove the path completes and the numbers are finite/bounded.
        self.assertGreater(CopierLifecycleDispatch.objects.count(), 0)
        self.assertEqual(ExecutionJob.objects.count(), 0)
        for v in (emit, ingest, lifecycle, total):
            self.assertTrue(all(x >= 0 for x in v))
        self.assertLess(_pct(total, 99), 5000.0)     # generous ceiling so CI never flakes
