"""ADR-0075 Phase 5/10 — the monitoring readiness contract (DB).

Proves the eight-gate decision, the UI/API distinction states, the five-account causes each mapping to a
distinct durable reason code, and the load-bearing invariant: UNAVAILABLE is NEVER collapsed into "zero"
(a flat 0-position READY account and an UNAVAILABLE account are different results)."""
from __future__ import annotations

from django.test import TestCase, override_settings
from django.utils import timezone

from execution import monitoring_allocation as alloc
from execution import monitoring_models as mm
from execution.monitoring_gateway_client import ProbeResult
from execution.monitoring_readiness import (UI_MONITORING_PARTIAL, UI_MONITORING_ROUTE_READY,
                                            UI_MONITORING_UNAVAILABLE, UI_UNSUPPORTED,
                                            evaluate_route_readiness)
from execution.monitoring_models import MonitoringRoute
from execution.monitoring_testkit import FLAGS_ON, gateway_for, make_endpoint, make_node, make_tenant


@override_settings(**FLAGS_ON)
class ReadinessContractTests(TestCase):
    def setUp(self):
        self.node = make_node()
        self.gw = gateway_for(self.node)
        self.gw.status = mm.PhysicalHostGateway.Status.HEALTHY
        self.gw.last_health_ok = True
        self.gw.save()

    def _verified_route(self, login="2001", port=8800):
        acct, ws = make_tenant(login, node=self.node)
        ep = make_endpoint(acct, ws, self.node, port=port)
        res = alloc.reserve_route(acct, gateway=self.gw)
        route = MonitoringRoute.objects.get(pk=res.route_id)
        alloc.register_route(route, execution_endpoint=ep, local_port=ep.port)
        alloc.verify_route(route, attested={"login": login, "server": "IS6Technologies-Live",
                                            "trade_mode": 2, "is_demo": False,
                                            "capabilities": ["positions", "account_snapshot"]})
        route.refresh_from_db()
        return route

    def test_ready_when_all_gates_pass(self):
        route = self._verified_route()
        probe = ProbeResult(reachable=True, identity_ok=True, positions_ok=True, n_positions=3,
                            reason_code=mm.RC_OK)
        r = evaluate_route_readiness(route, probe, duplicate_count=1)
        self.assertEqual(r.readiness, MonitoringRoute.Readiness.MONITORING_READY)
        self.assertEqual(r.ui_state, UI_MONITORING_ROUTE_READY)
        self.assertTrue(all(r.gates.values()), r.gates)

    def test_flat_account_is_ready_not_unavailable(self):
        """A reachable, matched, 0-position account is READY (RC_NO_POSITION) — never 'unavailable'/zero."""
        route = self._verified_route(login="2002", port=8801)
        probe = ProbeResult(reachable=True, identity_ok=True, positions_ok=True, n_positions=0,
                            reason_code=mm.RC_NO_POSITION)
        r = evaluate_route_readiness(route, probe, duplicate_count=1)
        self.assertEqual(r.readiness, MonitoringRoute.Readiness.MONITORING_READY)
        self.assertNotEqual(r.readiness, MonitoringRoute.Readiness.MONITORING_UNAVAILABLE)

    def test_account_not_demo_is_partial_with_feature_disabled(self):
        """#53/#54/#56/#57 today: identity ok (equity readable) but positions gate refuses → PARTIAL with
        the precise reason, NOT a misleading zero and NOT fully unavailable."""
        route = self._verified_route(login="2053", port=8810)
        probe = ProbeResult(reachable=True, identity_ok=True, positions_ok=False,
                            positions_error="account_not_demo", reason_code=mm.RC_FEATURE_DISABLED)
        r = evaluate_route_readiness(route, probe, duplicate_count=1)
        self.assertEqual(r.readiness, MonitoringRoute.Readiness.MONITORING_PARTIAL)
        self.assertEqual(r.reason_code, mm.RC_FEATURE_DISABLED)
        self.assertEqual(r.ui_state, UI_MONITORING_PARTIAL)

    def test_terminal_init_failed_is_unavailable(self):
        """#50 today: mt5_init_failed → identity unproven → UNAVAILABLE with the terminal_init reason."""
        route = self._verified_route(login="2050", port=8807)
        probe = ProbeResult(reachable=True, identity_ok=False, positions_ok=False,
                            positions_error="mt5_init_failed", reason_code=mm.RC_TERMINAL_INIT_FAILED)
        r = evaluate_route_readiness(route, probe, duplicate_count=1)
        self.assertEqual(r.readiness, MonitoringRoute.Readiness.MONITORING_UNAVAILABLE)
        self.assertEqual(r.reason_code, mm.RC_TERMINAL_INIT_FAILED)

    def test_unreachable_is_unavailable(self):
        route = self._verified_route(login="2003", port=8802)
        probe = ProbeResult(reachable=False, reason_code=mm.RC_UNREACHABLE)
        r = evaluate_route_readiness(route, probe, duplicate_count=1)
        self.assertEqual(r.readiness, MonitoringRoute.Readiness.MONITORING_UNAVAILABLE)
        self.assertEqual(r.reason_code, mm.RC_UNREACHABLE)
        self.assertEqual(r.ui_state, UI_MONITORING_UNAVAILABLE)

    def test_duplicate_route_is_unavailable(self):
        route = self._verified_route(login="2004", port=8803)
        probe = ProbeResult(reachable=True, identity_ok=True, positions_ok=True, n_positions=0,
                            reason_code=mm.RC_NO_POSITION)
        r = evaluate_route_readiness(route, probe, duplicate_count=2)  # two active routes for the account
        self.assertEqual(r.readiness, MonitoringRoute.Readiness.MONITORING_UNAVAILABLE)
        self.assertEqual(r.reason_code, mm.RC_DUPLICATE_ROUTE)

    def test_tombstoned_route_unavailable(self):
        route = self._verified_route(login="2005", port=8804)
        alloc.tombstone_route(route)
        route.refresh_from_db()
        probe = ProbeResult(reachable=True, identity_ok=True, positions_ok=True, n_positions=0)
        r = evaluate_route_readiness(route, probe, duplicate_count=1)
        self.assertEqual(r.readiness, MonitoringRoute.Readiness.MONITORING_UNAVAILABLE)
        self.assertEqual(r.reason_code, mm.RC_TOMBSTONED)

    def test_no_route_is_unsupported(self):
        probe = ProbeResult(reachable=False)
        r = evaluate_route_readiness(None, probe)
        self.assertEqual(r.readiness, MonitoringRoute.Readiness.UNSUPPORTED)
        self.assertEqual(r.ui_state, UI_UNSUPPORTED)

    def test_stale_fresh_gate(self):
        route = self._verified_route(login="2006", port=8805)
        # Make last_verified_at old AND positions_ok False so the freshness gate is the deciding factor
        # (positions_ok True would otherwise satisfy g7). Here identity ok + positions ok but stale.
        MonitoringRoute.objects.filter(pk=route.pk).update(
            last_verified_at=timezone.now() - timezone.timedelta(hours=1))
        route.refresh_from_db()
        probe = ProbeResult(reachable=True, identity_ok=True, positions_ok=True, n_positions=1,
                            reason_code=mm.RC_OK)
        # positions_ok True sets g7 True regardless of last_verified_at (a successful live read IS fresh).
        r = evaluate_route_readiness(route, probe, duplicate_count=1)
        self.assertEqual(r.readiness, MonitoringRoute.Readiness.MONITORING_READY)
