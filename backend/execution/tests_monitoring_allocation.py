"""ADR-0075 Phase 3/4/10 — monitoring-route allocation + lifecycle (DB).

Deterministic lowest-free allocation, one ACTIVE route per account, physical-host-wide port uniqueness,
generation monotonicity (+1 per occupancy, never reused), pool-exhaustion + capacity-ceiling fail-closed,
occupancy-identity integrity quarantine, register/verify, decommission + port reuse, multi-host isolation,
and DARK fail-closed when the subsystem is off.
"""
from __future__ import annotations

from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from execution import monitoring_allocation as alloc
from execution.monitoring_models import MonitoringRoute, PhysicalHostGateway
from execution.monitoring_testkit import FLAGS_ON, gateway_for, make_endpoint, make_node, make_tenant


@override_settings(**FLAGS_ON)
class AllocationTests(TestCase):
    def setUp(self):
        self.node = make_node()
        self.gw = gateway_for(self.node)

    def _acct(self, login):
        acct, ws = make_tenant(login, node=self.node)
        return acct, ws

    def test_reserve_lowest_free_port(self):
        acct, _ws = self._acct(1001)
        res = alloc.reserve_route(acct, gateway=self.gw)
        self.assertTrue(res.ok)
        self.assertEqual(res.reason, alloc.ALLOC_OK)
        self.assertEqual(res.local_port, 8800)
        self.assertEqual(res.generation, 1)

    def test_idempotent_reserve_returns_same_route(self):
        acct, _ws = self._acct(1002)
        a = alloc.reserve_route(acct, gateway=self.gw)
        b = alloc.reserve_route(acct, gateway=self.gw)
        self.assertEqual(a.route_id, b.route_id)
        self.assertEqual(b.reason, alloc.ALLOC_REACTIVATED)
        self.assertEqual(MonitoringRoute.objects.filter(trading_account=acct).count(), 1)

    def test_host_wide_port_uniqueness(self):
        a1, _ = self._acct(1003)
        a2, _ = self._acct(1004)
        r1 = alloc.reserve_route(a1, gateway=self.gw)
        r2 = alloc.reserve_route(a2, gateway=self.gw)
        self.assertEqual({r1.local_port, r2.local_port}, {8800, 8801})

    def test_one_active_route_per_account_constraint(self):
        acct, _ = self._acct(1005)
        alloc.reserve_route(acct, gateway=self.gw)
        # A second ACTIVE row for the same account violates the partial unique constraint.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MonitoringRoute.objects.create(
                    trading_account=acct, gateway=self.gw, host_key=self.gw.host_key,
                    local_port=8899, allocation_generation=2, state=MonitoringRoute.State.RESERVED)

    def test_generation_monotonic_and_port_reclaimed(self):
        acct, _ = self._acct(1006)
        r1 = alloc.reserve_route(acct, gateway=self.gw)
        self.assertEqual((r1.generation, r1.local_port), (1, 8800))
        alloc.tombstone_route(MonitoringRoute.objects.get(pk=r1.route_id))
        r2 = alloc.reserve_route(acct, gateway=self.gw)
        self.assertEqual(r2.generation, 2)              # +1 on the next occupancy
        self.assertEqual(r2.local_port, 8800)           # reclaimed after tombstone
        alloc.tombstone_route(MonitoringRoute.objects.get(pk=r2.route_id))
        r3 = alloc.reserve_route(acct, gateway=self.gw)
        self.assertEqual(r3.generation, 3)              # never repeats / decreases
        # uniq(account, generation) holds across all three rows.
        gens = sorted(MonitoringRoute.objects.filter(trading_account=acct)
                      .values_list("allocation_generation", flat=True))
        self.assertEqual(gens, [1, 2, 3])

    def test_pool_exhaustion_fails_closed(self):
        with mock.patch.object(alloc, "PORT_RANGE_START", 8800), \
             mock.patch.object(alloc, "PORT_RANGE_END", 8800):
            a1, _ = self._acct(1007)
            a2, _ = self._acct(1008)
            alloc.reserve_route(a1, gateway=self.gw)         # takes the single port 8800
            with self.assertRaises(alloc.MonitoringError) as ctx:
                alloc.reserve_route(a2, gateway=self.gw)
            self.assertEqual(ctx.exception.reason, alloc.ALLOC_POOL_EXHAUSTED)

    def test_capacity_ceiling_fails_closed(self):
        self.gw.max_routes = 1
        self.gw.save(update_fields=["max_routes"])
        a1, _ = self._acct(1009)
        a2, _ = self._acct(1010)
        alloc.reserve_route(a1, gateway=self.gw)
        with self.assertRaises(alloc.MonitoringError) as ctx:
            alloc.reserve_route(a2, gateway=self.gw)
        self.assertEqual(ctx.exception.reason, alloc.ALLOC_CAPACITY_EXCEEDED)

    def test_register_then_verify_identity_match(self):
        acct, ws = self._acct(1011)
        ep = make_endpoint(acct, ws, self.node, port=8800)
        res = alloc.reserve_route(acct, gateway=self.gw)
        route = MonitoringRoute.objects.get(pk=res.route_id)
        alloc.register_route(route, execution_endpoint=ep, local_port=ep.port)
        route.refresh_from_db()
        self.assertEqual(route.state, MonitoringRoute.State.REGISTERED)
        out = alloc.verify_route(route, attested={
            "login": "1011", "server": "IS6Technologies-Live", "trade_mode": 2, "is_demo": False,
            "capabilities": ["positions", "account_snapshot"]})
        self.assertTrue(out.ok)
        route.refresh_from_db()
        self.assertEqual(route.state, MonitoringRoute.State.VERIFIED)
        self.assertEqual(route.capability_set, ["positions", "account_snapshot"])

    def test_verify_identity_mismatch_quarantines(self):
        acct, ws = self._acct(1012)
        res = alloc.reserve_route(acct, gateway=self.gw)
        route = MonitoringRoute.objects.get(pk=res.route_id)
        alloc.register_route(route)
        with self.assertRaises(alloc.MonitoringIntegrityError):
            alloc.verify_route(route, attested={"login": "9999", "server": "Evil-Server"})
        route.refresh_from_db()
        self.assertEqual(route.state, MonitoringRoute.State.QUARANTINED)

    def test_occupancy_identity_drift_quarantines(self):
        acct, _ = self._acct(1013)
        res = alloc.reserve_route(acct, gateway=self.gw)
        route = MonitoringRoute.objects.get(pk=res.route_id)
        # Corrupt the slot identity (host_key no longer matches the owning gateway's host).
        MonitoringRoute.objects.filter(pk=route.pk).update(host_key="DRIFTED")
        route.refresh_from_db()
        with self.assertRaises(alloc.MonitoringIntegrityError):
            alloc.drain_route(route)
        route.refresh_from_db()
        self.assertEqual(route.state, MonitoringRoute.State.QUARANTINED)

    def test_decommission_then_reuse_by_other_account(self):
        a1, _ = self._acct(1014)
        a2, _ = self._acct(1015)
        r1 = alloc.reserve_route(a1, gateway=self.gw)
        self.assertEqual(r1.local_port, 8800)
        alloc.tombstone_route(MonitoringRoute.objects.get(pk=r1.route_id))
        r2 = alloc.reserve_route(a2, gateway=self.gw)
        self.assertEqual(r2.local_port, 8800)            # freed port reused by a different account

    def test_multi_host_independent_pools(self):
        node2 = make_node(hostname="node-b", rdp_host="100.79.101.20")
        gw2 = gateway_for(node2)
        a1, _ = self._acct(1016)
        a2, _ = self._acct(1017)
        r1 = alloc.reserve_route(a1, gateway=self.gw)
        r2 = alloc.reserve_route(a2, gateway=gw2)
        self.assertEqual(r1.local_port, 8800)
        self.assertEqual(r2.local_port, 8800)            # different physical host, independent pool
        self.assertNotEqual(r1.route_id, r2.route_id)

    def test_ensure_gateway_idempotent(self):
        g1 = gateway_for(self.node)
        self.assertEqual(g1.pk, self.gw.pk)
        self.assertEqual(PhysicalHostGateway.objects.filter(host_key=self.node.rdp_host).count(), 1)

    def test_slot_generation_monotonic_across_accounts(self):
        """Generation is a per-SLOT counter: a DIFFERENT account reusing a freed port gets the slot's NEXT
        generation, so (host, port, generation) is never reused across occupants (the core invariant)."""
        a1, _ = self._acct(1101)
        a2, _ = self._acct(1102)
        r1 = alloc.reserve_route(a1, gateway=self.gw)
        self.assertEqual((r1.local_port, r1.generation), (8800, 1))
        alloc.tombstone_route(MonitoringRoute.objects.get(pk=r1.route_id))
        r2 = alloc.reserve_route(a2, gateway=self.gw)       # different account, reuses port 8800
        self.assertEqual(r2.local_port, 8800)
        self.assertEqual(r2.generation, 2)                  # slot generation bumped, NOT reset to 1
        gens = sorted(MonitoringRoute.objects.filter(host_key=self.gw.host_key, local_port=8800)
                      .values_list("allocation_generation", flat=True))
        self.assertEqual(gens, [1, 2])                      # unique per slot

    def test_quarantined_route_blocks_reserve_fail_closed(self):
        acct, _ = self._acct(1103)
        res = alloc.reserve_route(acct, gateway=self.gw)
        route = MonitoringRoute.objects.get(pk=res.route_id)
        MonitoringRoute.objects.filter(pk=route.pk).update(host_key="DRIFTED")  # force a drift
        route.refresh_from_db()
        with self.assertRaises(alloc.MonitoringIntegrityError):
            alloc.drain_route(route)                        # quarantines (persists)
        # Reserving again for the same account must FAIL CLOSED, not silently reuse/return the quarantined
        # route as a success.
        with self.assertRaises(alloc.MonitoringError) as ctx:
            alloc.reserve_route(acct, gateway=self.gw)
        self.assertEqual(ctx.exception.reason, alloc.ALLOC_QUARANTINED)


class SubsystemDisabledTests(TestCase):
    """With the master flag OFF (the production DARK default), the allocator does nothing (fail-closed)."""

    def test_reserve_refused_when_disabled(self):
        node = make_node()
        with self.assertRaises(alloc.MonitoringError) as ctx:
            alloc.ensure_gateway(node.rdp_host)
        self.assertEqual(ctx.exception.reason, alloc.ALLOC_SUBSYSTEM_DISABLED)
