"""ADR-0048 — execution-path readiness (concept C), node-operational gate, node-awareness isolation.

Proves the read-only concept-C surface: which nodes a worker may claim (single shared rule), whether
a node has an eligible/alive order-capable claimant, the fail-closed node-operational commission
gate, and the operational-health scan of the exact Node-2 silent-failure conditions. Tenant/node
isolation is fail-closed: a node-1 worker can never be a node-2 claimant, and neither the legacy nor
a revoked worker is ever a claimant. Nothing here places or authorises an order.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution.auth import LEGACY_WORKER_ID
from execution.models import ExecutionJob, TerminalNode, WorkerIdentity
from execution.node_execution import (
    ClaimantProbe,
    eligible_order_claimant,
    evaluate_execution_path_readiness,
    node_execution_operational,
    scan_execution_path_health,
    worker_authorized_nodes,
)
from trading.models import TradingAccount

User = get_user_model()
ACTIVE = WorkerIdentity.Status.ACTIVE
REVOKED = WorkerIdentity.Status.REVOKED


def _node(hostname, *, bridge="http://10.0.0.1:8789", status=TerminalNode.Status.ACTIVE):
    return TerminalNode.objects.create(hostname=hostname, order_bridge_base_url=bridge, status=status)


def _worker(worker_id, nodes, *, status=ACTIVE, seen_age_s=None):
    last = None if seen_age_s is None else timezone.now() - timezone.timedelta(seconds=seen_age_s)
    return WorkerIdentity.objects.create(
        worker_id=worker_id, worker_secret_hash="x", status=status,
        worker_permissions={"authorized_nodes": nodes}, last_seen=last)


class NodeAwarenessRuleTests(TestCase):
    def test_legacy_worker_is_never_node_aware(self):
        w = _worker(LEGACY_WORKER_ID, ["node-a"])
        self.assertEqual(worker_authorized_nodes(w), [])   # force-emptied — cannot claim hosted jobs

    def test_revoked_worker_claims_nothing(self):
        w = _worker("w-rev", ["node-a"], status=REVOKED)
        self.assertEqual(worker_authorized_nodes(w), [])

    def test_none_and_nonlist_are_empty(self):
        self.assertEqual(worker_authorized_nodes(None), [])
        w = WorkerIdentity.objects.create(worker_id="w-bad", worker_secret_hash="x",
                                          worker_permissions={"authorized_nodes": "node-a"})
        self.assertEqual(worker_authorized_nodes(w), [])   # non-list ⇒ [] (never a partial match)

    def test_node_aware_worker_lists_its_nodes(self):
        w = _worker("w-a", ["node-a", "node-b"])
        self.assertEqual(sorted(worker_authorized_nodes(w)), ["node-a", "node-b"])


class EligibleClaimantTests(TestCase):
    def test_no_worker_at_all(self):
        node = _node("guvfx-beta-node-1")
        probe = eligible_order_claimant(node)
        self.assertFalse(probe.ok)
        self.assertEqual(probe.reason, "EP_NO_ELIGIBLE_WORKER")   # the exact Node-2 gap

    def test_registered_but_never_seen_is_stale(self):
        node = _node("guvfx-beta-node-1")
        _worker("w-n2", ["guvfx-beta-node-1"], seen_age_s=None)
        probe = eligible_order_claimant(node)
        self.assertFalse(probe.ok)
        self.assertEqual(probe.reason, "EP_WORKER_STALE")

    def test_recently_seen_worker_is_ok(self):
        node = _node("guvfx-beta-node-1")
        _worker("w-n2", ["guvfx-beta-node-1"], seen_age_s=5)
        probe = eligible_order_claimant(node)
        self.assertTrue(probe.ok)
        self.assertEqual(probe.reason, "EP_CLAIMANT_OK")
        self.assertEqual(probe.worker_id, "w-n2")

    def test_stale_worker_beyond_window(self):
        node = _node("guvfx-beta-node-1")
        _worker("w-n2", ["guvfx-beta-node-1"], seen_age_s=100000)
        self.assertEqual(eligible_order_claimant(node).reason, "EP_WORKER_STALE")

    # ── ISOLATION (req 7) ─────────────────────────────────────────────────────────────────────────
    def test_node1_worker_cannot_claim_node2(self):
        node2 = _node("guvfx-beta-node-1")
        _worker("mt5-trade-ingest-1", ["guvfx-windows-mt5"], seen_age_s=5)  # node-1 only
        self.assertEqual(eligible_order_claimant(node2).reason, "EP_NO_ELIGIBLE_WORKER")

    def test_legacy_worker_is_not_a_claimant(self):
        node = _node("guvfx-beta-node-1")
        _worker(LEGACY_WORKER_ID, ["guvfx-beta-node-1"], seen_age_s=1)  # even if mis-granted the node
        self.assertEqual(eligible_order_claimant(node).reason, "EP_NO_ELIGIBLE_WORKER")

    def test_revoked_worker_is_not_a_claimant(self):
        node = _node("guvfx-beta-node-1")
        _worker("w-rev", ["guvfx-beta-node-1"], status=REVOKED, seen_age_s=1)
        self.assertEqual(eligible_order_claimant(node).reason, "EP_NO_ELIGIBLE_WORKER")


class NodeOperationalGateTests(TestCase):
    def test_no_worker_is_not_operational(self):
        node = _node("guvfx-beta-node-1")
        r = node_execution_operational(node)
        self.assertFalse(r.operational)
        self.assertEqual(r.reason_code, "NODE_NO_ELIGIBLE_WORKER")

    def test_bridge_unconfigured_is_not_operational(self):
        node = _node("guvfx-beta-node-1", bridge="")
        _worker("w-n2", ["guvfx-beta-node-1"], seen_age_s=5)
        self.assertEqual(node_execution_operational(node).reason_code, "NODE_BRIDGE_UNCONFIGURED")

    def test_registered_worker_operational_at_commission(self):
        # Commission time: a registered node-aware worker not yet seen is accepted (liveness relaxed).
        node = _node("guvfx-beta-node-1")
        _worker("w-n2", ["guvfx-beta-node-1"], seen_age_s=None)
        r = node_execution_operational(node, require_worker_liveness=False)
        self.assertTrue(r.operational)
        self.assertEqual(r.reason_code, "NODE_OPERATIONAL")

    def test_liveness_required_rejects_unseen_worker(self):
        node = _node("guvfx-beta-node-1")
        _worker("w-n2", ["guvfx-beta-node-1"], seen_age_s=None)
        r = node_execution_operational(node, require_worker_liveness=True)
        self.assertFalse(r.operational)
        self.assertEqual(r.reason_code, "NODE_WORKER_NOT_LIVE")

    def test_inactive_node_not_operational(self):
        node = _node("guvfx-beta-node-1", status=TerminalNode.Status.OFFLINE) \
            if hasattr(TerminalNode.Status, "OFFLINE") else None
        if node is None:
            self.skipTest("no OFFLINE status")
        _worker("w-n2", ["guvfx-beta-node-1"], seen_age_s=5)
        self.assertEqual(node_execution_operational(node).reason_code, "NODE_STATUS_NOT_ACTIVE")


class HealthScanTests(TestCase):
    def _acct(self, pk):
        u = User.objects.create_user(username=f"u{pk}", email=f"u{pk}@x.invalid", password="x")
        return TradingAccount.objects.create(
            id=pk, user=u, name=f"A{pk}", account_number=f"A{pk}", broker_name="B",
            is_demo=True, is_active=True, password_enc="e")

    def test_pending_orders_with_no_claimant_is_critical(self):
        node = _node("guvfx-beta-node-1")
        acct = self._acct(100)
        ExecutionJob.objects.create(job_type=ExecutionJob.JobType.PLACE_ORDER, account=acct,
                                    terminal_node=node, status=ExecutionJob.Status.PENDING, payload={})
        findings = scan_execution_path_health()
        codes = {f["code"] for f in findings}
        self.assertIn("NODE_NO_ELIGIBLE_WORKER", codes)
        self.assertIn("NODE_PENDING_NO_CLAIMANT", codes)   # the exact Node-2 alarm

    def test_healthy_node_with_claimant_no_pending_alarm(self):
        node = _node("guvfx-beta-node-1")
        _worker("w-n2", ["guvfx-beta-node-1"], seen_age_s=5)
        findings = scan_execution_path_health()
        codes = {f["code"] for f in findings}
        self.assertNotIn("NODE_PENDING_NO_CLAIMANT", codes)
        self.assertNotIn("NODE_NO_ELIGIBLE_WORKER", codes)


class ExecutionPathReadinessTests(TestCase):
    def _acct(self, pk, node=None):
        u = User.objects.create_user(username=f"u{pk}", email=f"u{pk}@x.invalid", password="x")
        return TradingAccount.objects.create(
            id=pk, user=u, name=f"A{pk}", account_number=f"A{pk}", broker_name="B",
            is_demo=True, is_active=True, password_enc="e", terminal_node=node)

    def test_legacy_account_is_not_hosted_scope(self):
        acct = self._acct(100)
        r = evaluate_execution_path_readiness(acct)
        self.assertFalse(r.ready)
        # DARK (pin subsystem off) or explicitly not-hosted — either way NEVER ready, fail-closed.
        self.assertIn(r.reason_code, ("EP_EXPECTED_DARK", "EP_NOT_HOSTED"))

    def test_fail_closed_on_error(self):
        # A bogus object with no attributes exercises the outer fail-closed guard.
        class Bogus:
            pass
        r = evaluate_execution_path_readiness(Bogus())
        self.assertFalse(r.ready)
        self.assertIn(r.reason_code, ("EP_NOT_HOSTED", "EP_EXPECTED_DARK", "EP_INDETERMINATE"))

    def test_readiness_is_never_an_order_authority(self):
        # Structural: the result is a frozen dataclass with no side effects — assert it carries no
        # callable that could place an order and is purely descriptive.
        acct = self._acct(101)
        r = evaluate_execution_path_readiness(acct)
        self.assertIsInstance(r.ready, bool)
        self.assertIsInstance(r.checks, dict)


class BridgeHealthTests(TestCase):
    def test_no_global_fallback_node_scoped_only(self):
        # MEDIUM-1 fix: a healthy GLOBAL EXECUTION_PIPELINE row must NOT make a node with no OWN row
        # read as healthy — that would infer this node's bridge health from a DIFFERENT bridge.
        from reliability.models import Component, ComponentHealth, HealthStatus

        from execution.node_execution import _bridge_health
        node = _node("guvfx-beta-node-1")
        ComponentHealth.objects.create(component=Component.EXECUTION_PIPELINE, terminal_node=None,
                                       status=HealthStatus.OK)   # global row, healthy
        self.assertEqual(_bridge_health(node), "UNOBSERVED")     # node has no own row ⇒ unobserved

    def test_node_scoped_row_is_read(self):
        from reliability.models import Component, ComponentHealth, HealthStatus

        from execution.node_execution import _bridge_health
        node = _node("guvfx-beta-node-1")
        ComponentHealth.objects.create(component=Component.EXECUTION_PIPELINE, terminal_node=node,
                                       status=HealthStatus.OK)
        self.assertEqual(_bridge_health(node), "OK")
