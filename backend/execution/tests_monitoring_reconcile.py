"""ADR-0075 Phase 7/10 — monitoring-route reconciliation + governed alerting (DB).

Proves: each fault class updates the route's durable readiness/reason, operator alerts are opened via the
EXISTING reliability AlertEvent model (NO reliability migration, NO constants.py edit — a raw namespaced
component string), dedup prevents duplicate OPEN alerts, recovery resolves them, the host-wide
all-unreachable signal raises one CRITICAL, alert detail carries no raw host IP or login, and reconcile
mutates NO account/workspace/endpoint. Default-OFF: a no-op when flags are off."""
from __future__ import annotations

from django.test import TestCase, override_settings

from execution import monitoring_allocation as alloc
from execution import monitoring_models as mm
from execution import monitoring_reconcile as recon
from execution.monitoring_gateway_client import ProbeResult
from execution.monitoring_models import MonitoringRoute
from execution.monitoring_testkit import FLAGS_RECON, gateway_for, make_endpoint, make_node, make_tenant
from hosted_workspace.models import HostedMt5Workspace
from reliability.models import AlertEvent
from trading.models import TradingAccount


@override_settings(**FLAGS_RECON)
class ReconcileTests(TestCase):
    def setUp(self):
        self.node = make_node()
        self.gw = gateway_for(self.node)
        self.gw.status = mm.PhysicalHostGateway.Status.HEALTHY
        self.gw.last_health_ok = True
        self.gw.save()
        self.routes = {}
        for i, login in enumerate(["5001", "5053", "5050", "5003"]):
            acct, ws = make_tenant(login, node=self.node)
            ep = make_endpoint(acct, ws, self.node, port=8800 + i)
            res = alloc.reserve_route(acct, gateway=self.gw)
            route = MonitoringRoute.objects.get(pk=res.route_id)
            alloc.register_route(route, execution_endpoint=ep, local_port=ep.port)
            alloc.verify_route(route, attested={"login": login, "server": "IS6Technologies-Live",
                                                "trade_mode": 2, "capabilities": ["positions"]})
            self.routes[login] = route.trading_account_id

    def _probes(self):
        return {
            "5001": ProbeResult(reachable=True, identity_ok=True, positions_ok=True, n_positions=0,
                                reason_code=mm.RC_NO_POSITION),
            "5053": ProbeResult(reachable=True, identity_ok=True, positions_ok=False,
                                reason_code=mm.RC_FEATURE_DISABLED),
            "5050": ProbeResult(reachable=True, identity_ok=False, positions_ok=False,
                                reason_code=mm.RC_TERMINAL_INIT_FAILED),
            "5003": ProbeResult(reachable=True, identity_ok=False, reason_code=mm.RC_IDENTITY_MISMATCH),
        }

    def _probe_fn(self):
        by_acct = {self.routes[k]: v for k, v in self._probes().items()}
        return lambda route: by_acct[route.trading_account_id]

    def test_reconcile_classifies_and_persists_readiness(self):
        summary = recon.reconcile_routes(probe_fn=self._probe_fn())
        self.assertEqual(summary["scanned"], 4)
        self.assertEqual(summary["ready"], 1)       # 5001 flat
        self.assertEqual(summary["partial"], 1)     # 5053 feature-disabled
        self.assertEqual(summary["unavailable"], 2)  # 5050 init-failed + 5003 mismatch
        r5053 = MonitoringRoute.objects.get(trading_account_id=self.routes["5053"])
        self.assertEqual(r5053.readiness, MonitoringRoute.Readiness.MONITORING_PARTIAL)
        self.assertEqual(r5053.last_reason_code, mm.RC_FEATURE_DISABLED)
        self.assertEqual(r5053.health_state, MonitoringRoute.Health.DEGRADED)

    def test_alerts_opened_via_existing_reliability_model_and_deduped(self):
        recon.reconcile_routes(probe_fn=self._probe_fn())
        opened = AlertEvent.objects.filter(component=recon.ALERT_COMPONENT, status=AlertEvent.Status.OPEN)
        # identity mismatch (CRITICAL) + terminal init (WARN) alert; feature_disabled is INFO (no alert).
        reasons = set(AlertEvent.objects.filter(component=recon.ALERT_COMPONENT)
                      .values_list("dedup_key", flat=True))
        self.assertTrue(any("bridge_identity_mismatch" in k for k in reasons))
        self.assertTrue(any("terminal_init_failed" in k for k in reasons))
        n1 = opened.count()
        recon.reconcile_routes(probe_fn=self._probe_fn())  # second pass
        self.assertEqual(AlertEvent.objects.filter(component=recon.ALERT_COMPONENT,
                                                   status=AlertEvent.Status.OPEN).count(), n1)  # deduped

    def test_recovery_resolves_open_alerts(self):
        recon.reconcile_routes(probe_fn=self._probe_fn())
        self.assertTrue(AlertEvent.objects.filter(component=recon.ALERT_COMPONENT,
                                                  trading_account_id=self.routes["5050"],
                                                  status=AlertEvent.Status.OPEN).exists())
        # 5050 recovers (now reads fine) → its open alerts resolve.
        healthy = {self.routes[k]: ProbeResult(reachable=True, identity_ok=True, positions_ok=True,
                                               n_positions=0, reason_code=mm.RC_NO_POSITION)
                   for k in self.routes}
        recon.reconcile_routes(probe_fn=lambda r: healthy[r.trading_account_id])
        self.assertFalse(AlertEvent.objects.filter(component=recon.ALERT_COMPONENT,
                                                   trading_account_id=self.routes["5050"],
                                                   status=AlertEvent.Status.OPEN).exists())

    def test_host_wide_unreachable_raises_one_critical(self):
        unreachable = {aid: ProbeResult(reachable=False, reason_code=mm.RC_UNREACHABLE)
                       for aid in self.routes.values()}
        summary = recon.reconcile_routes(probe_fn=lambda r: unreachable[r.trading_account_id])
        self.assertIn(self.node.rdp_host, summary["host_unreachable"])
        host_alerts = AlertEvent.objects.filter(component=recon.ALERT_COMPONENT,
                                                dedup_key__startswith="monhost:unreachable:")
        self.assertEqual(host_alerts.count(), 1)
        self.assertEqual(host_alerts.first().severity, "CRITICAL")

    def test_alert_detail_has_no_raw_host_ip_or_login(self):
        recon.reconcile_routes(probe_fn=self._probe_fn())
        for a in AlertEvent.objects.filter(component=recon.ALERT_COMPONENT):
            blob = f"{a.title} {a.body} {a.detail}"
            self.assertNotIn(self.node.rdp_host, blob)      # raw Tailscale IP never present
            for login in ("5001", "5053", "5050", "5003"):
                self.assertNotIn(login, blob)               # raw broker login never present

    def test_reconcile_mutates_no_account_or_workspace(self):
        before_accts = list(TradingAccount.objects.values_list("id", "is_active").order_by("id"))
        before_ws = HostedMt5Workspace.objects.count()
        recon.reconcile_routes(probe_fn=self._probe_fn())
        after_accts = list(TradingAccount.objects.values_list("id", "is_active").order_by("id"))
        self.assertEqual(before_accts, after_accts)
        self.assertEqual(HostedMt5Workspace.objects.count(), before_ws)

    def test_integrity_failure_does_not_abort_pass(self):
        """A drifted route's set_readiness quarantines + raises mid-pass; the remaining routes are still
        processed, the quarantine persists, and an integrity alert is opened."""
        acct_id = self.routes["5001"]
        MonitoringRoute.objects.filter(trading_account_id=acct_id).update(host_key="DRIFTED")
        healthy = ProbeResult(reachable=True, identity_ok=True, positions_ok=True, n_positions=0,
                              reason_code=mm.RC_NO_POSITION)
        summary = recon.reconcile_routes(probe_fn=lambda r: healthy)
        self.assertEqual(summary["scanned"], 4)                 # pass completed despite the failure
        self.assertEqual(MonitoringRoute.objects.get(trading_account_id=acct_id).state,
                         MonitoringRoute.State.QUARANTINED)     # persisted
        self.assertTrue(AlertEvent.objects.filter(
            component=recon.ALERT_COMPONENT, trading_account_id=acct_id,
            dedup_key__contains="route_integrity_violation", status=AlertEvent.Status.OPEN).exists())
        others = MonitoringRoute.objects.exclude(trading_account_id=acct_id)
        self.assertTrue(all(o.readiness == MonitoringRoute.Readiness.MONITORING_READY for o in others))

    def test_host_wide_alert_resolved_on_recovery(self):
        unreachable = {aid: ProbeResult(reachable=False, reason_code=mm.RC_UNREACHABLE)
                       for aid in self.routes.values()}
        recon.reconcile_routes(probe_fn=lambda r: unreachable[r.trading_account_id])
        self.assertTrue(AlertEvent.objects.filter(dedup_key__startswith="monhost:unreachable:",
                                                  status=AlertEvent.Status.OPEN).exists())
        healthy = ProbeResult(reachable=True, identity_ok=True, positions_ok=True, n_positions=0,
                              reason_code=mm.RC_NO_POSITION)
        recon.reconcile_routes(probe_fn=lambda r: healthy)
        self.assertFalse(AlertEvent.objects.filter(dedup_key__startswith="monhost:unreachable:",
                                                   status=AlertEvent.Status.OPEN).exists())  # resolved

    def test_reason_transition_resolves_stale_alert(self):
        acct_id = self.routes["5050"]
        ok = ProbeResult(reachable=True, identity_ok=True, positions_ok=True, n_positions=0,
                         reason_code=mm.RC_NO_POSITION)

        def pf(reason, identity_ok=False):
            p = ProbeResult(reachable=True, identity_ok=identity_ok, positions_ok=False, reason_code=reason)
            return lambda r: (p if r.trading_account_id == acct_id else ok)

        recon.reconcile_routes(probe_fn=pf(mm.RC_TERMINAL_INIT_FAILED))
        a = f"monroute:{mm.RC_TERMINAL_INIT_FAILED}:{acct_id}"
        self.assertTrue(AlertEvent.objects.filter(dedup_key=a, status=AlertEvent.Status.OPEN).exists())
        recon.reconcile_routes(probe_fn=pf(mm.RC_IDENTITY_MISMATCH))
        b = f"monroute:{mm.RC_IDENTITY_MISMATCH}:{acct_id}"
        self.assertFalse(AlertEvent.objects.filter(dedup_key=a, status=AlertEvent.Status.OPEN).exists())  # A resolved
        self.assertTrue(AlertEvent.objects.filter(dedup_key=b, status=AlertEvent.Status.OPEN).exists())   # B open

    def test_orphan_alert_resolved_after_decommission(self):
        acct_id = self.routes["5050"]  # probe gives it terminal_init_failed → a WARN alert
        recon.reconcile_routes(probe_fn=self._probe_fn())
        self.assertTrue(AlertEvent.objects.filter(component=recon.ALERT_COMPONENT,
                                                  trading_account_id=acct_id,
                                                  status=AlertEvent.Status.OPEN).exists())
        alloc.tombstone_route(MonitoringRoute.objects.get(trading_account_id=acct_id))  # decommission
        healthy = ProbeResult(reachable=True, identity_ok=True, positions_ok=True, n_positions=0,
                              reason_code=mm.RC_NO_POSITION)
        recon.reconcile_routes(probe_fn=lambda r: healthy)  # 5050 no longer scanned → swept resolved
        self.assertFalse(AlertEvent.objects.filter(component=recon.ALERT_COMPONENT,
                                                   trading_account_id=acct_id,
                                                   status=AlertEvent.Status.OPEN).exists())

    def test_stale_alert_resolved_when_route_enters_drift(self):
        acct_id = self.routes["5003"]
        healthy = ProbeResult(reachable=True, identity_ok=True, positions_ok=True, n_positions=0,
                              reason_code=mm.RC_NO_POSITION)
        unreachable = ProbeResult(reachable=False, reason_code=mm.RC_UNREACHABLE)
        recon.reconcile_routes(probe_fn=lambda r: unreachable if r.trading_account_id == acct_id else healthy)
        a = f"monroute:{mm.RC_UNREACHABLE}:{acct_id}"
        self.assertTrue(AlertEvent.objects.filter(dedup_key=a, status=AlertEvent.Status.OPEN).exists())
        MonitoringRoute.objects.filter(trading_account_id=acct_id).update(host_key="DRIFTED")  # permanent drift
        recon.reconcile_routes(probe_fn=lambda r: unreachable if r.trading_account_id == acct_id else healthy)
        self.assertFalse(AlertEvent.objects.filter(dedup_key=a, status=AlertEvent.Status.OPEN).exists())  # stale resolved
        self.assertTrue(AlertEvent.objects.filter(
            dedup_key=f"monroute:route_integrity_violation:{acct_id}",
            status=AlertEvent.Status.OPEN).exists())  # integrity alert raised


class ReconcileDisabledTests(TestCase):
    def test_run_is_noop_when_flags_off(self):
        self.assertEqual(recon.run(), {"skipped": "disabled"})
