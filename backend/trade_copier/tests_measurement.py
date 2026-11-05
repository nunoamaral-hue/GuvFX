"""Phase-10 capacity / latency measurement (ADR-0074).

Synthetic, local, deterministic. Produces the p50/p95/p99 detection→apply latency figures and the per-batch
DB-query cost that the handoff records as evidence, and proves the sub-second cadence gate stays closed without
a measured capacity profile. Timing-based throughput is PRINTED as evidence only (never asserted — CI timing is
not a correctness signal). Nothing here enables a sub-second production cadence or changes the default cadence.
"""
import time
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone

from trade_copier import cadence, change_types
from trade_copier.cadence import CapacityProfile, ReconciliationCadence, measure_latency
from trade_copier.diff import ObservedPosition
from trade_copier.models import CopierMaster
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
INGEST_ON = dict(COPIER_ENABLED=True, COPIER_EVENT_INGESTION_ENABLED=True)


def _pct(samples, q):
    s = sorted(samples)
    if not s:
        return None
    idx = min(len(s) - 1, int(round((q / 100.0) * (len(s) - 1))))
    return s[idx]


class CadenceGateTests(TestCase):
    def test_subsecond_refused_without_measured_capacity(self):
        sub = ReconciliationCadence(base_interval_ms=600, min_interval_ms=500, max_interval_ms=5000)
        ok, reason = sub.validate_against_capacity(None)
        self.assertFalse(ok)
        self.assertEqual(reason, "subsecond_requires_measured_capacity")

    def test_subsecond_allowed_only_with_headroom(self):
        sub = ReconciliationCadence(base_interval_ms=600, min_interval_ms=500, max_interval_ms=5000)
        thin = CapacityProfile(sampled_reads=100, p95_read_latency_ms=400, max_safe_concurrent_reads=4)
        self.assertFalse(sub.validate_against_capacity(thin)[0])        # 500ms < 2*400ms → refused
        good = CapacityProfile(sampled_reads=100, p95_read_latency_ms=200, max_safe_concurrent_reads=8)
        self.assertTrue(sub.validate_against_capacity(good)[0])         # 500ms >= 2*200ms → allowed
        self.assertFalse(cadence.DEFAULT_CADENCE.is_subsecond())        # the shipped default is NOT sub-second

    def test_measure_latency_poll_vs_event(self):
        now = timezone.now()
        poll = measure_latency(source="POLL", observed_at=now - timedelta(milliseconds=120), applied_at=now)
        self.assertIsNone(poll.true_broker_latency_ms)                  # poll path invents no broker time
        self.assertGreaterEqual(poll.detect_to_apply_ms, 100)
        event = measure_latency(source="EVENT", observed_at=now - timedelta(milliseconds=120), applied_at=now,
                                broker_event_time=now - timedelta(milliseconds=300))
        self.assertIsNotNone(event.true_broker_latency_ms)


class LatencyPercentileTests(TestCase):
    def test_detect_to_apply_percentiles_recorded(self):
        now = timezone.now()
        # Deterministic spread of detection→apply latencies (ms): 10..2000.
        offsets = list(range(10, 2010, 20))
        samples = []
        for off in offsets:
            m = measure_latency(source="EVENT", observed_at=now - timedelta(milliseconds=off), applied_at=now)
            samples.append(m.detect_to_apply_ms)
        p50, p95, p99 = _pct(samples, 50), _pct(samples, 95), _pct(samples, 99)
        self.assertLessEqual(p50, p95)
        self.assertLessEqual(p95, p99)
        print(f"\n[ADR-0074 EVIDENCE] detection→apply latency (synthetic, n={len(samples)}): "
              f"p50={p50}ms p95={p95}ms p99={p99}ms")


@override_settings(**INGEST_ON)
class IngestCostTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.bs = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6Technologies-Demo",
                                              environment="demo")
        self.acct = TradingAccount.objects.create(user=self.user, name="m", broker_name="IS6",
                                                  account_number="1000001", is_demo=True, is_active=True,
                                                  broker_server=self.bs)
        self.master = CopierMaster.objects.create(trading_account=self.acct, observe_enabled=True)

    def test_batch_ingest_query_cost_is_bounded(self):
        from trade_copier import event_ingestion as ei
        n = 25
        now = timezone.now()
        revisions = [{"account_id": self.acct.id, "ticket": t, "revision": 1,
                      "observed_time": now.isoformat(), "side": "BUY", "volume": 0.1, "symbol": "XAUUSD"}
                     for t in range(1, n + 1)]
        payload = {"revisions": revisions}
        with CaptureQueriesContext(connection) as ctx:
            out = ei.ingest_revisions(account_id=self.acct.id, payload=payload,
                                      body_digest="d", correlation_id="c")
        self.assertEqual(out.accepted, n)
        # Linear/bounded: per revision a savepoint-wrapped insert (so one duplicate can't fail the batch) plus a
        # prior-state classify lookup (~4 queries/rev), plus a small fixed master/identity overhead. The exact
        # count is printed below as evidence; the assertion only pins the O(N) bound.
        self.assertLessEqual(len(ctx.captured_queries), 6 * n + 15)
        print(f"\n[ADR-0074 EVIDENCE] ingest batch n={n}: queries={len(ctx.captured_queries)}, "
              f"accepted={out.accepted}")

    def test_fold_throughput_evidence(self):
        from trade_copier.revisions import PositionRevision, fold_revisions
        now = timezone.now()
        revs = [PositionRevision(account_id=1, ticket=t, revision=1, observed_time=now,
                                 side="BUY", volume=0.1, symbol="XAUUSD") for t in range(1, 1001)]
        start = time.perf_counter()
        res = fold_revisions(revs)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        self.assertEqual(len(res.positions), 1000)
        # Also exercise the classifier over a non-trivial snapshot delta.
        prev = [ObservedPosition(ticket=1, symbol="XAUUSD", side="BUY", volume=0.1, sl=1990, tp=2100)]
        new = [ObservedPosition(ticket=1, symbol="XAUUSD", side="BUY", volume=0.05, sl=1995, tp=2110)]
        changes = change_types.classify_position_changes(prev, new)
        self.assertTrue(changes)
        print(f"\n[ADR-0074 EVIDENCE] pure fold of 1000 revisions: {elapsed_ms:.2f}ms "
              f"(NOT asserted — throughput evidence only)")
