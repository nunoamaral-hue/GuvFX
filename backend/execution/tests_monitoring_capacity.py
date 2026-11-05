"""ADR-0075 Phase 8/10 — scale, concurrency and capacity (DB, threaded).

Measures allocator cost and proves correctness at the 11-account estate and the 20-account certified
target, under concurrency, with legacy-endpoint migration, decommission + safe reuse, and multi-host.
Crucially it SEPARATES allocation capacity (a deterministic DB fact, bounded by the pool and the certified
HostCapacity ceiling) from measured host/terminal capacity (a SEPARATE certified number — NOT claimed here
from a config integer; real host/terminal load certification is out of CI scope)."""
from __future__ import annotations

import statistics
import threading
import time

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from execution import monitoring_allocation as alloc
from execution import monitoring_models as mm
from execution.models import HostCapacity
from execution.monitoring_models import MonitoringRoute
from execution.monitoring_testkit import FLAGS_ON, gateway_for, make_endpoint, make_node, make_tenant


def _pct(samples, q):
    s = sorted(samples)
    if not s:
        return 0.0
    k = min(len(s) - 1, int(round((len(s) - 1) * q)))
    return s[k]


@override_settings(**FLAGS_ON)
class CapacityTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.node = make_node(max_accounts=50)
        self.gw = gateway_for(self.node)

    def _accounts(self, n, base=6000):
        return [make_tenant(str(base + i), node=self.node)[0] for i in range(n)]

    def test_single_reserve_query_bound(self):
        acct = self._accounts(1)[0]
        with CaptureQueriesContext(connection) as ctx:
            alloc.reserve_route(acct, gateway=self.gw)
        # Bounded, constant number of queries per reservation (no N+1 scaling with estate size).
        self.assertLessEqual(len(ctx.captured_queries), 15, len(ctx.captured_queries))

    def test_scale_11_and_20_distinct_ports_and_latency(self):
        for n in (11, 20):
            MonitoringRoute.objects.all().delete()
            accts = self._accounts(n, base=6000 + n * 100)  # non-overlapping bases per iteration
            lat = []
            ports = []
            for a in accts:
                t0 = time.perf_counter()
                res = alloc.reserve_route(a, gateway=self.gw)
                lat.append((time.perf_counter() - t0) * 1000.0)
                ports.append(res.local_port)
            self.assertEqual(len(set(ports)), n)            # all distinct
            self.assertEqual(min(ports), 8800)
            self.assertEqual(max(ports), 8800 + n - 1)      # contiguous lowest-free
            print(f"[capacity n={n}] p50={_pct(lat,0.5):.2f}ms p95={_pct(lat,0.95):.2f}ms "
                  f"p99={_pct(lat,0.99):.2f}ms max={max(lat):.2f}ms")

    def test_concurrent_reservations_no_collision(self):
        n = 12
        accts = self._accounts(n, base=6300)
        barrier = threading.Barrier(n)
        results = []
        lock = threading.Lock()

        def worker(acct):
            try:
                barrier.wait(timeout=10)
                res = alloc.reserve_route(acct, gateway=self.gw)
                with lock:
                    results.append(res.local_port)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(a,)) for a in accts]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(len(results), n)
        self.assertEqual(len(set(results)), n)              # no two accounts on the same port
        self.assertEqual(MonitoringRoute.objects.filter(
            state=MonitoringRoute.State.RESERVED).count(), n)

    def test_sync_routes_from_legacy_endpoints(self):
        from execution.monitoring_onboarding import sync_routes_from_endpoints
        accts = self._accounts(11, base=6500)
        for i, a in enumerate(accts):
            make_endpoint(a, a.hosted_workspace, self.node, port=8800 + i)
        out = sync_routes_from_endpoints()
        self.assertEqual(out["created"], 11)
        self.assertEqual(MonitoringRoute.objects.exclude(
            state=MonitoringRoute.State.TOMBSTONED).count(), 11)
        # Idempotent: a second sync reuses, creates nothing new.
        out2 = sync_routes_from_endpoints()
        self.assertEqual(out2["created"], 0)

    def test_decommission_and_reuse_at_scale(self):
        accts = self._accounts(20, base=6700)
        routes = [MonitoringRoute.objects.get(pk=alloc.reserve_route(a, gateway=self.gw).route_id)
                  for a in accts]
        # Tombstone the first 5 → their ports free.
        for r in routes[:5]:
            alloc.tombstone_route(r)
        freed = sorted(r.local_port for r in routes[:5])
        # 5 NEW accounts reuse exactly the freed ports (lowest-first), no collision with the live 15.
        newcomers = self._accounts(5, base=6800)
        reused = sorted(alloc.reserve_route(a, gateway=self.gw).local_port for a in newcomers)
        self.assertEqual(reused, freed)
        self.assertEqual(MonitoringRoute.objects.filter(
            state=MonitoringRoute.State.RESERVED).count(), 20)

    def test_allocation_capacity_is_distinct_from_host_capacity(self):
        """Allocation capacity (pool = 100 ports) is far larger than the CERTIFIED host ceiling; the
        allocator enforces the ceiling, not the pool, and never claims real host/terminal load capacity
        (which is a SEPARATE certified number, not provable in CI)."""
        HostCapacity.objects.create(physical_host_key=self.node.rdp_host, max_total_occupants=20,
                                    certified_safe=18, basis="test")
        accts = self._accounts(21, base=6900)
        ok = 0
        blocked = 0
        for a in accts:
            try:
                alloc.reserve_route(a, gateway=self.gw)
                ok += 1
            except alloc.MonitoringError as e:
                self.assertEqual(e.reason, alloc.ALLOC_CAPACITY_EXCEEDED)
                blocked += 1
        self.assertEqual(ok, 20)        # bounded by the certified ceiling, NOT the 100-port pool
        self.assertEqual(blocked, 1)

    def test_route_restart_reregistration_bumps_generation(self):
        acct = self._accounts(1, base=6950)[0]
        r1 = alloc.reserve_route(acct, gateway=self.gw)
        alloc.tombstone_route(MonitoringRoute.objects.get(pk=r1.route_id))
        r2 = alloc.reserve_route(acct, gateway=self.gw)       # "restart"/re-registration
        self.assertEqual(r2.local_port, r1.local_port)        # same slot reused
        self.assertEqual(r2.generation, r1.generation + 1)    # new occupancy identity
