"""ADR-0103 — monitoring ENROLMENT projection + reconciler (GFX-DASHBOARD-AUTO-MONITORING-ENROLMENT-087).

Covers the packet's required regression matrix: automatic discovery, idempotent route creation, no-duplicate
reconcile, the #58-shaped LIVE account becoming dashboard-eligible WITHOUT a hard-coded id, fail-closed on
capacity/identity, per-account snapshot isolation (no cross-account substitution), truthful partial coverage,
the login-required-is-not-transient contract, bounded query count (no N+1), Copy-Trading isolation, and the
two-level-dark apply gate. Synthetic fixtures only; no PII, no host contact."""
from __future__ import annotations

import inspect
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from analytics.models import AccountEquitySnapshot
from execution import monitoring_enrolment as E
from execution.models import HostedExecutionEndpoint
from execution.monitoring_models import MonitoringRoute
from execution.monitoring_testkit import FLAGS_ON, gateway_for, make_endpoint, make_node, make_tenant
from trading.models import Trade, TradingAccount

FLAGS_ENROL = {"MONITORING_GATEWAY_ENABLED": "1", "MONITORING_ENROLMENT_RECONCILE_ENABLED": "1"}


def _snapshot(acct, *, age_s=0, balance="100.00", equity="100.00", currency="USD"):
    return AccountEquitySnapshot.objects.create(
        trading_account=acct, observed_at=timezone.now() - timedelta(seconds=age_s),
        balance=balance, equity=equity, account_currency=currency)


def _connect(ws, *, connected=True, matched=True):
    ws.proj_connected = connected
    ws.proj_account_match = matched
    ws.canonical_state = "CONNECTED" if connected else "WAITING_FOR_LOGIN"
    ws.save(update_fields=["proj_connected", "proj_account_match", "canonical_state"])


def _confirm(acct):
    acct.workspace_confirmed_at = timezone.now()
    acct.save(update_fields=["workspace_confirmed_at"])


class EnrolmentProjectionTests(TestCase):
    """Layer-1 pure projection: stage derivation from durable records (no gateway, production-active path)."""

    def setUp(self):
        self.node = make_node()

    def _ready(self, login, port, *, is_demo=False):
        """A fully-enrolled account: hosted, connected+matched+confirmed, endpoint READY, fresh snapshot."""
        acct, ws = make_tenant(login, node=self.node, is_demo=is_demo)
        make_endpoint(acct, ws, self.node, port=port, state=HostedExecutionEndpoint.State.READY)
        _connect(ws)
        _confirm(acct)
        _snapshot(acct)
        return acct, ws

    def _status(self, acct, **kw):
        ctxs = E._build_contexts([acct], [acct.id])
        return E.evaluate_account_enrolment(acct, ctx=ctxs[acct.id], **kw)

    # 5 + 12: the #58-shaped LIVE account becomes dashboard-eligible, no hard-coded id.
    def test_live_account_fully_enrolled_is_ready_and_monitored(self):
        acct, _ = self._ready("4109", 8800, is_demo=False)   # DominionMarkets-Live shape: LIVE, connected, data
        st = self._status(acct)
        self.assertEqual(st.stage, E.READY)
        self.assertTrue(st.monitored)
        self.assertEqual(st.pipeline_step, E.PIPE_DASHBOARD_INCLUDED)
        # No account id/login appears in the secret-free projection output.
        self.assertNotIn("4109", str(st.as_dict()))

    # 14: endpoint READY but the latest snapshot is older than the freshness window ⇒ stale, not monitored.
    def test_ready_endpoint_stale_snapshot_is_snapshot_stale(self):
        acct, ws = make_tenant("5001", node=self.node)
        make_endpoint(acct, ws, self.node, port=8801)
        _connect(ws); _confirm(acct)
        _snapshot(acct, age_s=E.SNAPSHOT_FRESHNESS_S + 60)
        st = self._status(acct)
        self.assertEqual(st.stage, E.SNAPSHOT_STALE)
        self.assertFalse(st.monitored)

    # Review HIGH #1: a FRESH but balance-only (equity=None) latest snapshot must NOT read as monitored — the
    # equity curve only counts equity-bearing rows, so counting a balance-only row would overstate coverage.
    def test_balance_only_fresh_snapshot_is_not_monitored(self):
        acct, ws = make_tenant("5020", node=self.node)
        make_endpoint(acct, ws, self.node, port=8820)
        _connect(ws); _confirm(acct)
        _snapshot(acct, equity=None)                                   # fresh, but balance-only (no equity)
        _snapshot(acct, age_s=E.SNAPSHOT_FRESHNESS_S + 300)            # the only equity-bearing row is stale
        st = self._status(acct)
        self.assertEqual(st.stage, E.SNAPSHOT_STALE)
        self.assertFalse(st.monitored)
        # A fresh equity-bearing row then flips it to monitored (parity with the equity curve).
        _snapshot(acct, equity="100.00")
        self.assertEqual(self._status(acct).stage, E.READY)

    # Review LOW #2: the local freshness constant is pinned against the equity-curve ledger window (no drift).
    def test_freshness_matches_equity_curve_window(self):
        from analytics.equity_snapshots import DEFAULT_FRESHNESS_S
        self.assertEqual(E.SNAPSHOT_FRESHNESS_S, DEFAULT_FRESHNESS_S)

    # endpoint allocated but not READY ⇒ setting up monitoring (transient, not "reconnecting").
    def test_endpoint_allocated_not_ready_is_provisioning(self):
        acct, ws = make_tenant("5002", node=self.node)
        make_endpoint(acct, ws, self.node, port=8802, state=HostedExecutionEndpoint.State.ALLOCATED)
        _connect(ws); _confirm(acct)
        st = self._status(acct)
        self.assertEqual(st.stage, E.PROVISIONING)
        self.assertEqual(st.ui_state, "Setting up monitoring")
        self.assertFalse(st.structural)

    def test_connected_matched_no_endpoint_is_endpoint_missing(self):
        acct, ws = make_tenant("5003", node=self.node)
        _connect(ws); _confirm(acct)
        st = self._status(acct)
        self.assertEqual(st.stage, E.ENDPOINT_MISSING)

    # 11: login-required is NOT a transient "reconnecting" state.
    def test_not_connected_with_endpoint_is_login_required_structural(self):
        acct, ws = make_tenant("5004", node=self.node)
        make_endpoint(acct, ws, self.node, port=8804)
        _connect(ws, connected=False)       # WAITING_FOR_LOGIN
        st = self._status(acct)
        self.assertEqual(st.stage, E.LOGIN_REQUIRED)
        self.assertEqual(st.ui_state, "Login required")
        self.assertTrue(st.structural)      # UI must not imply "reconnecting"

    # STAGE-ACCURACY-FIX-095: a connected+matched account with a READY endpoint + FRESH equity is MONITORED even
    # when the ADR-0034 customer-confirmation ACK is absent — the monitoring stage is decoupled from
    # ``workspace_confirmed_at`` (which still gates EXECUTION, proven in test_execution_confirmation_gate_preserved).
    def test_fresh_ready_endpoint_unconfirmed_is_monitored(self):
        acct, ws = make_tenant("5005", node=self.node)
        make_endpoint(acct, ws, self.node, port=8805)        # READY
        _connect(ws)                                         # connected+matched, NOT confirmed
        _snapshot(acct)                                      # fresh equity ⇒ data is flowing
        self.assertIsNone(acct.workspace_confirmed_at)
        st = self._status(acct)
        self.assertEqual(st.stage, E.READY)
        self.assertTrue(st.monitored)
        self.assertEqual(st.detail["coverage"]["connection"], "connected")
        self.assertEqual(st.detail["coverage"]["equity"], "fresh")

    # STAGE-ACCURACY-FIX-095: a connected+matched account with a READY endpoint but STALE equity, still unconfirmed,
    # is SNAPSHOT_STALE (the truthful monitoring blocker) — NOT a false LOGIN_REQUIRED from the confirmation gate.
    def test_stale_ready_endpoint_unconfirmed_is_snapshot_stale_not_login(self):
        acct, ws = make_tenant("5005b", node=self.node)
        make_endpoint(acct, ws, self.node, port=8825)        # READY
        _connect(ws)                                         # connected+matched, NOT confirmed
        _snapshot(acct, age_s=E.SNAPSHOT_FRESHNESS_S + 600)  # stale equity
        self.assertIsNone(acct.workspace_confirmed_at)
        st = self._status(acct)
        self.assertEqual(st.stage, E.SNAPSHOT_STALE)
        self.assertNotEqual(st.stage, E.LOGIN_REQUIRED)
        self.assertFalse(st.monitored)
        self.assertEqual(st.detail["coverage"]["equity"], "stale")

    # 8: wrong account identity (matcher says not matched) ⇒ structural identity mismatch.
    def test_connected_account_mismatch_is_identity_mismatch(self):
        acct, ws = make_tenant("5006", node=self.node)
        make_endpoint(acct, ws, self.node, port=8806)
        _connect(ws, matched=False); _confirm(acct)
        st = self._status(acct)
        self.assertEqual(st.stage, E.IDENTITY_MISMATCH)
        self.assertTrue(st.structural)

    # 10: unreachable endpoint remains VISIBLE but unavailable (transient), from the live read reason.
    def test_ready_endpoint_live_reason_unreachable(self):
        acct, _ = self._ready("5007", 8807)
        st = self._status(acct, live_reason="bridge_unreachable")
        self.assertEqual(st.stage, E.UNREACHABLE)
        self.assertFalse(st.structural)
        self.assertFalse(st.monitored)

    def test_ready_endpoint_live_reason_identity_refused(self):
        acct, _ = self._ready("5008", 8808)
        st = self._status(acct, live_reason="identity_refused")
        self.assertEqual(st.stage, E.IDENTITY_MISMATCH)

    # A LIVE terminal's account_not_demo positions gate does NOT demote the account below READY (equity flows).
    def test_account_not_demo_does_not_demote_ready(self):
        acct, _ = self._ready("5009", 8809, is_demo=False)
        st = self._status(acct, live_reason="account_not_demo")
        self.assertEqual(st.stage, E.READY)
        self.assertTrue(st.monitored)
        self.assertTrue(st.detail.get("positions_unsupported"))

    # 23: a legacy (non-hosted) account monitors via the global agent — never STRUCTURALLY_EXCLUDED.
    def test_legacy_account_fresh_snapshot_is_ready(self):
        user = get_user_model().objects.create_user(username="leg", email="leg@x.invalid", password="x")
        acct = TradingAccount.objects.create(user=user, name="leg", broker_name="X", account_number="9001")
        _snapshot(acct)
        st = self._status(acct)
        self.assertEqual(st.stage, E.READY)
        self.assertEqual(st.detail.get("path"), "legacy")

    def test_legacy_account_no_snapshot_is_stale_not_excluded(self):
        user = get_user_model().objects.create_user(username="leg2", email="leg2@x.invalid", password="x")
        acct = TradingAccount.objects.create(user=user, name="leg2", broker_name="X", account_number="9002")
        st = self._status(acct)
        self.assertEqual(st.stage, E.SNAPSHOT_STALE)
        self.assertNotEqual(st.stage, E.STRUCTURALLY_EXCLUDED)

    # 23: a tombstoned (disconnected) account is excluded with an explicit reason (defensive).
    def test_disconnected_account_is_structurally_excluded(self):
        acct, ws = make_tenant("5010", node=self.node)
        acct.disconnected_at = timezone.now(); acct.save(update_fields=["disconnected_at"])
        st = self._status(acct)
        self.assertEqual(st.stage, E.STRUCTURALLY_EXCLUDED)
        self.assertTrue(st.reason_code)

    # 15: per-account snapshot isolation — one account's stale snapshot never makes another READY.
    def test_no_cross_account_snapshot_substitution(self):
        fresh, _ = self._ready("5011", 8811)
        stale, ws2 = make_tenant("5012", node=self.node)
        make_endpoint(stale, ws2, self.node, port=8812)
        _connect(ws2); _confirm(stale)
        _snapshot(stale, age_s=E.SNAPSHOT_FRESHNESS_S + 120)
        by_id = E.evaluate_enrolment_for_accounts([fresh, stale])
        self.assertEqual(by_id[fresh.id].stage, E.READY)
        self.assertEqual(by_id[stale.id].stage, E.SNAPSHOT_STALE)

    # 20/21: coverage rollup numerator/denominator correct; partial coverage labelled by stage.
    def test_coverage_rollup_truthful_partial(self):
        a_ready, _ = self._ready("5013", 8813)
        a_missing, ws = make_tenant("5014", node=self.node)
        _connect(ws); _confirm(a_missing)                    # connected, no endpoint ⇒ ENDPOINT_MISSING
        by_id = E.evaluate_enrolment_for_accounts([a_ready, a_missing])
        cov = E.coverage_rollup(by_id.values())
        self.assertEqual(cov["total"], 2)
        self.assertEqual(cov["monitored"], 1)
        self.assertEqual(cov["not_monitored"], 1)
        self.assertEqual(cov["by_stage"].get(E.ENDPOINT_MISSING), 1)
        self.assertEqual(cov["by_stage"].get(E.READY), 1)

    # 16: each account appears exactly once in a projection.
    def test_account_appears_once(self):
        a1, _ = self._ready("5015", 8815)
        a2, _ = self._ready("5016", 8816)
        by_id = E.evaluate_enrolment_for_accounts([a1, a2])
        self.assertEqual(sorted(by_id.keys()), sorted([a1.id, a2.id]))

    # 25: bounded query count — independent of the number of accounts (no N+1).
    def test_query_count_bounded_no_n_plus_1(self):
        small = [self._ready(str(6000 + i), 8700 + i)[0] for i in range(3)]
        big = [self._ready(str(6100 + i), 8720 + i)[0] for i in range(12)]
        with CaptureQueriesContext(connection) as q_small:
            E.evaluate_enrolment_for_accounts(small)
        with CaptureQueriesContext(connection) as q_big:
            E.evaluate_enrolment_for_accounts(big)
        self.assertEqual(len(q_small.captured_queries), len(q_big.captured_queries))
        self.assertLessEqual(len(q_big.captured_queries), 6)


@override_settings(**FLAGS_ON)
class EnrolmentRouteProjectionTests(TestCase):
    """When the gateway subsystem is ON and a durable MonitoringRoute exists, the projection prefers it."""

    def setUp(self):
        self.node = make_node()
        self.gw = gateway_for(self.node)

    def _route(self, login, port, *, readiness, reason):
        acct, ws = make_tenant(login, node=self.node)
        ep = make_endpoint(acct, ws, self.node, port=port)
        from execution import monitoring_allocation as alloc
        res = alloc.reserve_route(acct, gateway=self.gw, execution_endpoint=ep)
        r = MonitoringRoute.objects.get(pk=res.route_id)
        r.readiness = readiness
        r.last_reason_code = reason
        r.save(update_fields=["readiness", "last_reason_code"])
        return acct, r

    def test_route_ready_maps_to_ready(self):
        import execution.monitoring_models as mm
        acct, _ = self._route("7001", 8800, readiness=mm.MonitoringRoute.Readiness.MONITORING_READY, reason=mm.RC_OK)
        ctxs = E._build_contexts([acct], [acct.id])
        st = E.evaluate_account_enrolment(acct, ctx=ctxs[acct.id])
        self.assertEqual(st.stage, E.READY)

    def test_route_unavailable_identity_mismatch(self):
        import execution.monitoring_models as mm
        acct, _ = self._route("7002", 8801,
                              readiness=mm.MonitoringRoute.Readiness.MONITORING_UNAVAILABLE,
                              reason=mm.RC_IDENTITY_MISMATCH)
        ctxs = E._build_contexts([acct], [acct.id])
        st = E.evaluate_account_enrolment(acct, ctx=ctxs[acct.id])
        self.assertEqual(st.stage, E.IDENTITY_MISMATCH)

    def test_route_partial_feature_disabled_is_ready_positions_unsupported(self):
        import execution.monitoring_models as mm
        acct, _ = self._route("7003", 8802,
                              readiness=mm.MonitoringRoute.Readiness.MONITORING_PARTIAL,
                              reason=mm.RC_FEATURE_DISABLED)
        ctxs = E._build_contexts([acct], [acct.id])
        st = E.evaluate_account_enrolment(acct, ctx=ctxs[acct.id])
        self.assertEqual(st.stage, E.READY)
        self.assertTrue(st.detail.get("positions_unsupported"))

    # Review HIGH #2: a RESERVED route whose readiness is still UNKNOWN (the gateway probe has not run — exactly
    # the reserve->verify window) must NOT override a genuinely-READY account; it falls back to the active
    # projection so the dashboard keeps the truthful READY until the gateway verifies the route.
    def test_reserved_route_unknown_falls_back_to_active_projection(self):
        acct, ws = make_tenant("7004", node=self.node)
        ep = make_endpoint(acct, ws, self.node, port=8803, state=HostedExecutionEndpoint.State.READY)
        _connect(ws); _confirm(acct); _snapshot(acct)                  # fully active + fresh equity
        from execution import monitoring_allocation as alloc
        res = alloc.reserve_route(acct, gateway=self.gw, execution_endpoint=ep)   # readiness stays UNKNOWN
        route = MonitoringRoute.objects.get(pk=res.route_id)
        self.assertEqual(route.readiness, MonitoringRoute.Readiness.UNKNOWN)
        ctxs = E._build_contexts([acct], [acct.id])
        st = E.evaluate_account_enrolment(acct, ctx=ctxs[acct.id])
        self.assertEqual(st.stage, E.READY)                           # active projection, NOT "Status unknown"
        self.assertTrue(st.monitored)


class EnrolmentReconcilerDarkTests(TestCase):
    """Dry-run default + dark-apply gating (flags OFF): report only, mutate nothing."""

    def setUp(self):
        self.node = make_node()

    # 1: a fresh connected account is discovered by the universe selector.
    def test_discovery_includes_fresh_account(self):
        acct, ws = make_tenant("8001", node=self.node)
        _connect(ws); _confirm(acct)
        ids = [a.id for a in E.select_accounts()]
        self.assertIn(acct.id, ids)

    def test_disconnected_account_not_selected(self):
        acct, ws = make_tenant("8002", node=self.node)
        acct.disconnected_at = timezone.now(); acct.save(update_fields=["disconnected_at"])
        self.assertNotIn(acct.id, [a.id for a in E.select_accounts()])

    # Dry-run reports stages and writes NOTHING.
    def test_dry_run_reports_without_mutation(self):
        acct, ws = make_tenant("8003", node=self.node)
        make_endpoint(acct, ws, self.node, port=8800); _connect(ws); _confirm(acct); _snapshot(acct)
        report = E.reconcile_enrolment()
        self.assertEqual(report["considered"], 1)
        self.assertFalse(report["apply"])
        self.assertEqual(MonitoringRoute.objects.count(), 0)   # no route created in dry-run
        self.assertEqual(report["coverage"]["monitored"], 1)

    # apply requested but flags OFF ⇒ documented no-op (two-level darkness).
    def test_apply_dark_is_noop(self):
        acct, ws = make_tenant("8004", node=self.node)
        make_endpoint(acct, ws, self.node, port=8800); _connect(ws); _confirm(acct)
        report = E.reconcile_enrolment(apply=True)
        self.assertTrue(report["apply"])
        self.assertFalse(report["apply_effective"])
        self.assertEqual(report.get("apply_skipped"), "subsystem_disabled")
        self.assertEqual(MonitoringRoute.objects.count(), 0)

    # 24: Copy Trading isolation — the enrolment module never imports or touches trade_copier.
    def test_no_copy_trading_coupling(self):
        src = inspect.getsource(E)
        self.assertNotIn("trade_copier", src)
        self.assertNotIn("copier", src.lower())

    # Review MEDIUM #1: alert management runs in --apply INDEPENDENT of the dark route-mutation flags, so a stuck
    # account is surfaced even before the flags are flipped (route mutation itself stays dark — no route created).
    def test_alerts_flow_in_apply_when_route_mutation_dark(self):
        from reliability.models import AlertEvent
        acct, ws = make_tenant("8005", node=self.node)
        _connect(ws); _confirm(acct)                           # ENDPOINT_MISSING (actionable, WARN)
        report = E.reconcile_enrolment(apply=True)             # flags OFF (two-level dark)
        self.assertFalse(report["apply_effective"])
        self.assertEqual(MonitoringRoute.objects.count(), 0)   # route mutation stayed dark
        self.assertTrue(AlertEvent.objects.filter(
            trading_account=acct, status=AlertEvent.Status.OPEN,
            dedup_key=f"monenrol:{E.ENDPOINT_MISSING}:{acct.id}").exists())


@override_settings(**FLAGS_ENROL)
class EnrolmentReconcilerApplyTests(TransactionTestCase):
    """Apply path with the gateway + enrolment flags ON: idempotent route reservation, no duplicates,
    capacity fail-closed, cross-tenant correctness. Routes are reserved/registered, never marked READY."""
    reset_sequences = True

    def setUp(self):
        self.node = make_node(max_accounts=50)

    def _tenant(self, login, port):
        acct, ws = make_tenant(login, node=self.node)
        make_endpoint(acct, ws, self.node, port=port)
        _connect(ws); _confirm(acct); _snapshot(acct)
        return acct, ws

    # 2: a missing monitoring route is created once.
    def test_apply_creates_route_once(self):
        acct, _ = self._tenant("8100", 8800)
        report = E.reconcile_enrolment(apply=True, emit_alerts=False)
        self.assertTrue(report["apply_effective"])
        self.assertEqual(MonitoringRoute.objects.filter(trading_account=acct)
                         .exclude(state=MonitoringRoute.State.TOMBSTONED).count(), 1)
        self.assertEqual(report["created"], 1)
        # Second apply is idempotent: the existing active route is returned, counted as unchanged (not created).
        report2 = E.reconcile_enrolment(apply=True, emit_alerts=False)
        self.assertEqual(report2["created"], 0)
        self.assertEqual(report2["unchanged"], 1)

    # 3 + 4: repeated reconciliation creates no duplicate; a healthy account stays single-routed.
    def test_apply_idempotent_no_duplicate(self):
        acct, _ = self._tenant("8101", 8801)
        E.reconcile_enrolment(apply=True, emit_alerts=False)
        E.reconcile_enrolment(apply=True, emit_alerts=False)
        self.assertEqual(MonitoringRoute.objects.filter(trading_account=acct)
                         .exclude(state=MonitoringRoute.State.TOMBSTONED).count(), 1)

    # Routes are NEVER marked READY by the reconciler (that needs the gateway's live probe).
    def test_apply_never_marks_route_ready(self):
        self._tenant("8102", 8802)
        E.reconcile_enrolment(apply=True, emit_alerts=False)
        self.assertEqual(MonitoringRoute.objects.filter(state=MonitoringRoute.State.READY).count(), 0)

    # 7: cross-tenant correctness — each route is bound to its OWN account/endpoint, distinct ports.
    def test_apply_cross_tenant_distinct_routes(self):
        a1, _ = self._tenant("8103", 8803)
        a2, _ = self._tenant("8104", 8804)
        E.reconcile_enrolment(apply=True, emit_alerts=False)
        r1 = MonitoringRoute.objects.get(trading_account=a1)
        r2 = MonitoringRoute.objects.get(trading_account=a2)
        self.assertNotEqual(r1.local_port, r2.local_port)
        self.assertEqual(r1.execution_endpoint.trading_account_id, a1.id)
        self.assertEqual(r2.execution_endpoint.trading_account_id, a2.id)

    # 6: capacity fail-closed — a host route ceiling blocks the overflow account without crashing.
    def test_apply_capacity_fail_closed_blocks(self):
        gw = gateway_for(self.node)
        gw.max_routes = 1
        gw.save(update_fields=["max_routes"])
        self._tenant("8105", 8805)
        self._tenant("8106", 8806)
        report = E.reconcile_enrolment(apply=True, emit_alerts=False)
        created = MonitoringRoute.objects.exclude(state=MonitoringRoute.State.TOMBSTONED).count()
        self.assertEqual(created, 1)            # ceiling honoured
        self.assertGreaterEqual(report["blocked"], 1)

    # Review MEDIUM #2: a stuck actionable stage opens a governed alert, resolved only on a POSITIVE READY.
    def test_alert_opened_for_stuck_then_resolved_on_ready(self):
        from reliability.models import AlertEvent
        acct, ws = make_tenant("8110", node=self.node)
        _connect(ws); _confirm(acct)                           # connected+confirmed, NO endpoint -> ENDPOINT_MISSING
        E.reconcile_enrolment(account_id=acct.id, apply=True)
        self.assertTrue(AlertEvent.objects.filter(
            trading_account=acct, status=AlertEvent.Status.OPEN,
            dedup_key=f"monenrol:{E.ENDPOINT_MISSING}:{acct.id}").exists())
        make_endpoint(acct, ws, self.node, port=8810); _snapshot(acct)   # becomes READY
        E.reconcile_enrolment(account_id=acct.id, apply=True)
        self.assertFalse(AlertEvent.objects.filter(
            trading_account=acct, status=AlertEvent.Status.OPEN, dedup_key__startswith="monenrol:").exists())

    # Review MEDIUM #2: a non-actionable/degraded stage must NOT auto-resolve a genuine OPEN alert (anti-flap).
    def test_degraded_stage_does_not_resolve_genuine_alert(self):
        from reliability.models import AlertEvent
        acct, ws = make_tenant("8111", node=self.node)
        make_endpoint(acct, ws, self.node, port=8811); _connect(ws); _confirm(acct)   # no snapshot -> SNAPSHOT_STALE
        AlertEvent.objects.create(
            severity="CRITICAL", component="MONITORING_ENROL", trading_account=acct, title="x",
            dedup_key=f"monenrol:{E.IDENTITY_MISMATCH}:{acct.id}", status=AlertEvent.Status.OPEN)
        E.reconcile_enrolment(account_id=acct.id, apply=True)
        # The account currently projects SNAPSHOT_STALE (non-actionable) — the genuine alert must remain OPEN.
        self.assertTrue(AlertEvent.objects.filter(
            trading_account=acct, status=AlertEvent.Status.OPEN,
            dedup_key=f"monenrol:{E.IDENTITY_MISMATCH}:{acct.id}").exists())

    # 22: a transition to eligible is reflected by the reconcile (idempotent re-run).
    def test_reconcile_reflects_transition(self):
        acct, ws = make_tenant("8107", node=self.node)
        make_endpoint(acct, ws, self.node, port=8807, state=HostedExecutionEndpoint.State.ALLOCATED)
        _connect(ws); _confirm(acct)
        before = E.reconcile_enrolment(account_id=acct.id)
        self.assertEqual(before["accounts"][0]["stage"], E.PROVISIONING)
        # Account becomes ready: endpoint READY + fresh snapshot.
        ep = HostedExecutionEndpoint.objects.get(trading_account=acct)
        ep.state = HostedExecutionEndpoint.State.READY; ep.save(update_fields=["state"])
        _snapshot(acct)
        after = E.reconcile_enrolment(account_id=acct.id)
        self.assertEqual(after["accounts"][0]["stage"], E.READY)


# ── STAGE-ACCURACY-FIX-095 — the production-shaped cohort, dimensions, and the execution-gate preservation ────────

def _trade(acct, *, ticket="t1", age_days=6):
    """A closed trade (deal evidence) for an account — minimal required fields, no PII."""
    t = timezone.now() - timedelta(days=age_days)
    return Trade.objects.create(
        account=acct, ticket=ticket, symbol="XAUUSD", side=Trade.BUY, volume="0.01",
        open_time=t, close_time=t, open_price="2400.00", close_price="2410.00", profit="5.00")


class EnrolmentStageAccuracyCohortTests(TestCase):
    """The exact production-shaped support@ cohort from report-094 (12 LIVE, UNCONFIRMED accounts) must resolve to
    5 monitored / 6 snapshot-stale / 1 not-connected — proving the monitoring stage follows data evidence, not the
    ADR-0034 confirmation ACK. Account ids are TEST EVIDENCE ONLY (fixture labels); no id is in product logic."""

    def setUp(self):
        self.node = make_node()
        self.owner = get_user_model().objects.create_user(username="supportish", email="s@x.invalid", password="x")

    def _own(self, acct):
        acct.user = self.owner
        acct.save(update_fields=["user"])
        return acct

    def _hosted(self, login, port, *, connected=True, matched=True, fresh=None, deals=False, is_demo=False):
        """A hosted, UNCONFIRMED account (workspace_confirmed_at stays None, exactly like the prod cohort)."""
        acct, ws = make_tenant(login, node=self.node, is_demo=is_demo)
        make_endpoint(acct, ws, self.node, port=port, state=HostedExecutionEndpoint.State.READY)
        _connect(ws, connected=connected, matched=matched)
        if fresh is True:
            _snapshot(acct)
        elif fresh is False:
            _snapshot(acct, age_s=E.SNAPSHOT_FRESHNESS_S + 5000)       # stale (days old, like #58)
        if deals:
            _trade(acct)
        self.assertIsNone(acct.workspace_confirmed_at)                 # cohort invariant: NONE confirmed
        return self._own(acct)

    def _build_cohort(self):
        # 5 monitored (fresh equity): the report-094 "46,48,49,51,52" shape.
        monitored = [self._hosted(f"46{i}", 8900 + i, fresh=True, deals=(i == 0)) for i in range(5)]
        # 6 snapshot-stale (READY endpoint, equity stale/never): the "50,53,54,56,57,58" shape. #58 carries deals.
        stale = [self._hosted(f"50{i}", 8910 + i, fresh=False, deals=(i == 5)) for i in range(6)]
        # 1 not-connected: the "59" shape (endpoint present but workspace not connected ⇒ login required).
        notconn = [self._hosted("59", 8920, connected=False, matched=False, fresh=None)]
        return monitored, stale, notconn

    def test_production_shaped_cohort_resolves_5_6_1(self):
        self._build_cohort()
        accts = E.select_accounts(owner=self.owner)
        self.assertEqual(len(accts), 12)
        cov = E.coverage_rollup(E.evaluate_enrolment_for_accounts(accts).values())
        self.assertEqual(cov["total"], 12)
        self.assertEqual(cov["monitored"], 5)                          # was 0 before the fix (all LOGIN_REQUIRED)
        self.assertEqual(cov["not_monitored"], 7)
        self.assertEqual(cov["by_stage"].get(E.READY), 5)
        self.assertEqual(cov["by_stage"].get(E.SNAPSHOT_STALE), 6)
        self.assertEqual(cov["by_stage"].get(E.LOGIN_REQUIRED), 1)
        self.assertIsNone(cov["by_stage"].get(E.ENDPOINT_MISSING))     # none are endpoint-missing (all READY)

    def test_account58_shape_is_snapshot_stale_not_login_required(self):
        # #58: READY endpoint + base_url + connected + matched + STALE equity + closed-deal history + no route,
        # UNCONFIRMED. The truthful stage is SNAPSHOT_STALE — never LOGIN_REQUIRED, never ENDPOINT_MISSING.
        acct = self._hosted("4109", 8800, fresh=False, deals=True, is_demo=False)
        ctxs = E._build_contexts([acct], [acct.id])
        st = E.evaluate_account_enrolment(acct, ctx=ctxs[acct.id])
        self.assertEqual(st.stage, E.SNAPSHOT_STALE)
        self.assertNotEqual(st.stage, E.LOGIN_REQUIRED)
        self.assertNotEqual(st.stage, E.ENDPOINT_MISSING)
        cov = st.detail["coverage"]
        self.assertEqual(cov["endpoint"], "ready")
        self.assertEqual(cov["connection"], "connected")
        self.assertEqual(cov["identity"], "matched")
        self.assertEqual(cov["equity"], "stale")
        self.assertEqual(cov["deals"], "present")                      # closed-deal history exists
        self.assertEqual(cov["route"], "absent")                       # gateway dark ⇒ no MonitoringRoute
        # Live positions reported SEPARATELY as unavailable (never a fabricated zero).
        self.assertEqual(cov["positions"], "structurally_unavailable")
        self.assertNotIn("4109", str(st.as_dict()))                    # secret-free

    def test_report_command_owner_scoped_read_only_5_6_1(self):
        # Phase 4: the governed report command reproduces 5/6/1 for the owner cohort and writes NOTHING.
        from io import StringIO
        import json
        from django.core.management import call_command
        from reliability.models import AlertEvent
        self._build_cohort()
        out = StringIO()
        call_command("reconcile_monitoring_enrolment", "report", "--owner", str(self.owner.id), stdout=out)
        report = json.loads(out.getvalue())
        self.assertFalse(report["apply"])
        self.assertEqual(report["coverage"]["monitored"], 5)
        self.assertEqual(report["coverage"]["by_stage"].get(E.SNAPSHOT_STALE), 6)
        self.assertEqual(report["coverage"]["by_stage"].get(E.LOGIN_REQUIRED), 1)
        self.assertEqual(MonitoringRoute.objects.count(), 0)           # read-only: no route written
        self.assertEqual(AlertEvent.objects.count(), 0)               # read-only: no alert written

    def test_execution_confirmation_gate_preserved_independent_of_projection(self):
        # The decoupling proof: the SAME unconfirmed account is READY/monitored for the dashboard, yet EXECUTION
        # readiness STILL fails closed on the ADR-0034 confirm ACK (RW_NOT_CONFIRMED) — the fix did not weaken it.
        import execution.readiness as R
        acct, ws = make_tenant("5099", node=self.node, is_demo=True, is_active=True)  # demo+active: reaches confirm gate
        make_endpoint(acct, ws, self.node, port=8899, state=HostedExecutionEndpoint.State.READY)
        _connect(ws)                                                   # connected + matched, NOT confirmed
        _snapshot(acct)                                                # fresh equity
        ws.execution_enabled = True                                    # armed flag set, but no confirm ACK
        ws.save(update_fields=["execution_enabled"])
        self.assertIsNone(acct.workspace_confirmed_at)
        ctxs = E._build_contexts([acct], [acct.id])
        st = E.evaluate_account_enrolment(acct, ctx=ctxs[acct.id])
        self.assertEqual(st.stage, E.READY)                            # monitoring: data is flowing
        self.assertTrue(st.monitored)
        # Execution readiness — with the subsystem live (flags ON), the gate still fails closed on the confirm ACK.
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1"):
            dec = R.PersistentWorkspaceProvider().evaluate(acct)
        self.assertFalse(dec.eligible)
        self.assertEqual(dec.reason_code, R.RW_NOT_CONFIRMED)


class EnrolmentCoverageDimensionTests(TestCase):
    """Each monitoring dimension is reported INDEPENDENTLY — a single stage never collapses them, and fresh equity
    never implies live positions are available (they are demo-gated for a LIVE account, ADR-0072)."""

    def setUp(self):
        self.node = make_node()

    def _ctx_status(self, acct, **kw):
        ctxs = E._build_contexts([acct], [acct.id])
        return E.evaluate_account_enrolment(acct, ctx=ctxs[acct.id], **kw)

    def test_fresh_live_account_positions_separate_from_equity(self):
        acct, ws = make_tenant("6201", node=self.node, is_demo=False)  # LIVE
        make_endpoint(acct, ws, self.node, port=8921)
        _connect(ws); _snapshot(acct)                                  # fresh equity
        cov = self._ctx_status(acct).detail["coverage"]
        self.assertEqual(cov["equity"], "fresh")
        self.assertEqual(cov["positions"], "structurally_unavailable")  # fresh equity does NOT imply positions

    def test_demo_account_fresh_positions_available(self):
        acct, ws = make_tenant("6202", node=self.node, is_demo=True)   # DEMO: positions ARE supported
        make_endpoint(acct, ws, self.node, port=8922, is_demo=True)
        _connect(ws); _snapshot(acct)
        cov = self._ctx_status(acct).detail["coverage"]
        self.assertEqual(cov["positions"], "available")

    def test_equity_never_vs_stale_distinguished(self):
        never, ws = make_tenant("6203", node=self.node)
        make_endpoint(never, ws, self.node, port=8923); _connect(ws)   # READY, NO snapshot ever
        self.assertEqual(self._ctx_status(never).detail["coverage"]["equity"], "never")
        stale, ws2 = make_tenant("6204", node=self.node)
        make_endpoint(stale, ws2, self.node, port=8924); _connect(ws2)
        _snapshot(stale, age_s=E.SNAPSHOT_FRESHNESS_S + 300)           # observed once, now stale
        self.assertEqual(self._ctx_status(stale).detail["coverage"]["equity"], "stale")

    def test_deals_present_vs_absent(self):
        with_d, ws = make_tenant("6205", node=self.node)
        make_endpoint(with_d, ws, self.node, port=8925); _connect(ws); _snapshot(with_d); _trade(with_d)
        self.assertEqual(self._ctx_status(with_d).detail["coverage"]["deals"], "present")
        no_d, ws2 = make_tenant("6206", node=self.node)
        make_endpoint(no_d, ws2, self.node, port=8926); _connect(ws2); _snapshot(no_d)
        self.assertEqual(self._ctx_status(no_d).detail["coverage"]["deals"], "absent")

    def test_dimensions_present_on_every_status(self):
        # The coverage block is always present (never a missing key the dashboard must special-case).
        acct, ws = make_tenant("6207", node=self.node)
        _connect(ws, connected=False)                                  # LOGIN_REQUIRED
        cov = self._ctx_status(acct).detail["coverage"]
        for k in ("connection", "endpoint", "identity", "equity", "positions", "deals", "route"):
            self.assertIn(k, cov)
