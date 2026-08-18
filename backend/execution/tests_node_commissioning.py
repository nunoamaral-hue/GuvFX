"""ADR-0048 — NODE COMMISSIONING + provisioning execution-path gate + read-model reasons.

Proves, end-to-end at the provisioning level, that a future beta customer CANNOT reproduce the Node-2
"missing worker" silent failure:

  * a fresh node with no worker is NOT execution-operational;
  * with the (DARK) execution-path allocation gate ON, an automated hosted account cannot be allocated
    to a non-operational node — allocation fails closed;
  * ``commission_execution_node`` (server-derived, deterministic, idempotent, no account-specific code)
    registers a DEDICATED node-aware worker and makes the node operational;
  * allocation then proceeds and a hypothetical node-bound order would have an eligible claimant;
  * NOTHING in commissioning creates an ExecutionJob, sends an order, or arms a customer.

Isolation is fail-closed and lives in ``tests_execution_path_readiness`` (node-1 worker can't claim
node-2, revoked/legacy/stale can't, bridge health is node-scoped). Here we add the COMMISSION-side
isolation: Customer Zero nodes are refused, the legacy identity is refused, cross-node identity reuse
is refused, and commissioning is blocked while stale pre-activation orders remain (hard ordering).
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from execution.auth import LEGACY_WORKER_ID
from execution.models import ExecutionJob, TerminalNode, WorkerIdentity
from execution.node_commission import NODE_WORKER_SECRET_ENV, commission_execution_node
from execution.node_execution import (
    eligible_order_claimant,
    execution_path_state,
    node_execution_operational,
)
from trading.models import TradingAccount

User = get_user_model()
ACTIVE = WorkerIdentity.Status.ACTIVE
REVOKED = WorkerIdentity.Status.REVOKED
N2 = "guvfx-beta-node-1"
SECRET = "s3cr3t-node-worker"


def _node(hostname=N2, *, bridge="http://10.60.0.9:8789", status=TerminalNode.Status.ACTIVE):
    return TerminalNode.objects.create(hostname=hostname, order_bridge_base_url=bridge, status=status)


def _acct(pk, node=None):
    u = User.objects.create_user(username=f"u{pk}", email=f"u{pk}@x.invalid", password="x")
    return TradingAccount.objects.create(
        id=pk, user=u, name=f"A{pk}", account_number=f"A{pk}", broker_name="B",
        is_demo=True, is_active=True, password_enc="e", terminal_node=node)


def _no_cz():
    """Patch the live-derived CZ node set to EMPTY (this node is not Customer Zero)."""
    return mock.patch("hosted_workspace.tenant_isolation.forbidden_execution_node_ids", return_value=set())


class CommissionExecutionNodeTests(TestCase):
    def test_dry_run_default_registers_nothing(self):
        node = _node()
        with _no_cz():
            r = commission_execution_node(node_hostname=N2, worker_id="mt5-node2-order-1")
        self.assertFalse(r["apply"])
        self.assertFalse(r["applied"])
        self.assertFalse(r["operational"])                 # no worker yet
        self.assertEqual(r["reason"], "NODE_NO_ELIGIBLE_WORKER")
        self.assertEqual(WorkerIdentity.objects.count(), 0)  # dry-run mutated nothing

    def test_apply_commissions_node_operational(self):
        node = _node()
        with _no_cz(), mock.patch.dict("os.environ", {NODE_WORKER_SECRET_ENV: SECRET}):
            r = commission_execution_node(node_hostname=N2, worker_id="mt5-node2-order-1", apply=True)
        self.assertTrue(r["applied"])
        self.assertTrue(r["operational"])
        self.assertEqual(r["reason"], "NODE_OPERATIONAL")
        wi = WorkerIdentity.objects.get(worker_id="mt5-node2-order-1")
        self.assertEqual(wi.status, ACTIVE)
        self.assertEqual((wi.worker_permissions or {}).get("authorized_nodes"), [N2])
        self.assertEqual(wi.worker_secret_hash, WorkerIdentity.hash_secret(SECRET))
        self.assertTrue(node_execution_operational(node).operational)
        self.assertEqual(ExecutionJob.objects.count(), 0)   # commissioning places NO order

    def test_apply_is_idempotent(self):
        node = _node()
        with _no_cz(), mock.patch.dict("os.environ", {NODE_WORKER_SECRET_ENV: SECRET}):
            commission_execution_node(node_hostname=N2, worker_id="w-n2", apply=True)
            r2 = commission_execution_node(node_hostname=N2, worker_id="w-n2", apply=True)
        self.assertTrue(r2["operational"])
        self.assertEqual(WorkerIdentity.objects.filter(worker_id="w-n2").count(), 1)  # not duplicated

    def test_recommission_reactivates_revoked_worker(self):
        node = _node()
        WorkerIdentity.objects.create(worker_id="w-n2", worker_secret_hash="x", status=REVOKED,
                                      worker_permissions={"authorized_nodes": [N2]})
        with _no_cz(), mock.patch.dict("os.environ", {NODE_WORKER_SECRET_ENV: SECRET}):
            r = commission_execution_node(node_hostname=N2, worker_id="w-n2", apply=True)
        self.assertTrue(r["operational"])
        self.assertEqual(WorkerIdentity.objects.get(worker_id="w-n2").status, ACTIVE)

    def test_new_worker_without_secret_refused(self):
        _node()
        with _no_cz(), mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop(NODE_WORKER_SECRET_ENV, None)
            r = commission_execution_node(node_hostname=N2, worker_id="w-n2", apply=True)
        self.assertEqual(r["reason"], "SECRET_REQUIRED")
        self.assertFalse(r["applied"])
        self.assertEqual(WorkerIdentity.objects.count(), 0)

    def test_customer_zero_node_refused(self):
        node = _node()
        with mock.patch("hosted_workspace.tenant_isolation.forbidden_execution_node_ids",
                        return_value={node.id}):
            r = commission_execution_node(node_hostname=N2, worker_id="w-n2", apply=True)
        self.assertEqual(r["reason"], "NODE_IS_CUSTOMER_ZERO")
        self.assertEqual(WorkerIdentity.objects.count(), 0)

    def test_legacy_worker_id_refused(self):
        _node()
        with _no_cz():
            r = commission_execution_node(node_hostname=N2, worker_id=LEGACY_WORKER_ID, apply=True)
        self.assertEqual(r["reason"], "WORKER_IS_LEGACY_OR_EMPTY")

    def test_cross_node_identity_reuse_refused(self):
        _node(N2)
        _node("guvfx-beta-node-2", bridge="http://10.60.0.10:8789")
        WorkerIdentity.objects.create(worker_id="w-shared", worker_secret_hash="x", status=ACTIVE,
                                      worker_permissions={"authorized_nodes": ["guvfx-beta-node-2"]})
        with _no_cz():
            r = commission_execution_node(node_hostname=N2, worker_id="w-shared", apply=True)
        self.assertEqual(r["reason"], "WORKER_AUTHORIZED_FOR_OTHER_NODE")
        # the other node's grant is untouched
        self.assertEqual(
            WorkerIdentity.objects.get(worker_id="w-shared").worker_permissions["authorized_nodes"],
            ["guvfx-beta-node-2"])

    def test_stale_orders_block_commission_hard_ordering(self):
        node = _node()
        acct = _acct(200, node)
        job = ExecutionJob.objects.create(job_type=ExecutionJob.JobType.PLACE_ORDER, account=acct,
                                          terminal_node=node, status=ExecutionJob.Status.PENDING,
                                          payload={})
        old = timezone.now() - timezone.timedelta(seconds=3600)
        ExecutionJob.objects.filter(pk=job.pk).update(created_at=old)
        with _no_cz(), mock.patch.dict("os.environ", {NODE_WORKER_SECRET_ENV: SECRET}):
            r = commission_execution_node(node_hostname=N2, worker_id="w-n2", apply=True)
        self.assertEqual(r["reason"], "STALE_ORDERS_PRESENT")   # reconcile-first invariant, in code
        self.assertEqual(r["checks"]["stale_pending_orders"], 1)
        self.assertEqual(WorkerIdentity.objects.count(), 0)     # refused before any registration

    def test_bridge_url_conflict_refused(self):
        _node(bridge="http://10.60.0.9:8789")
        with _no_cz(), mock.patch.dict("os.environ", {NODE_WORKER_SECRET_ENV: SECRET}):
            r = commission_execution_node(node_hostname=N2, worker_id="w-n2", apply=True,
                                          bridge_url="http://10.60.0.9:9999")   # different endpoint
        self.assertEqual(r["reason"], "BRIDGE_URL_CONFLICT")
        self.assertEqual(WorkerIdentity.objects.count(), 0)

    def test_unknown_node_reported(self):
        with _no_cz():
            r = commission_execution_node(node_hostname="does-not-exist", worker_id="w-n2")
        self.assertEqual(r["reason"], "NODE_NOT_FOUND")

    def test_command_dry_run_json(self):
        _node()
        with _no_cz():
            call_command("commission_execution_node", "--node-hostname", N2,
                         "--worker-id", "w-n2", "--json")   # smoke: no exception, no mutation
        self.assertEqual(WorkerIdentity.objects.count(), 0)


class ExecutionPathStateReadModelTests(TestCase):
    """The stable internal read-model surface: execution_path_ready + a bounded reason vocabulary."""

    def test_dark_account_is_expected_dark(self):
        acct = _acct(300)                       # pin subsystem OFF in tests ⇒ DARK
        st = execution_path_state(acct)
        self.assertFalse(st["execution_path_ready"])
        self.assertIn(st["execution_path_reason"], ("expected_dark", "not_hosted"))

    def test_no_worker_reason_is_stable(self):
        acct = _acct(301)
        with mock.patch("execution.node_execution.evaluate_execution_path_readiness") as m:
            m.return_value = mock.Mock(ready=False, reason_code="EP_NO_ELIGIBLE_WORKER")
            st = execution_path_state(acct)
        self.assertEqual(st["execution_path_reason"], "no_worker")

    def test_no_worker_refines_to_worker_revoked(self):
        node = _node()
        acct = _acct(302, node)
        WorkerIdentity.objects.create(worker_id="w-rev", worker_secret_hash="x", status=REVOKED,
                                      worker_permissions={"authorized_nodes": [N2]})
        with mock.patch("execution.node_execution.evaluate_execution_path_readiness") as m:
            m.return_value = mock.Mock(ready=False, reason_code="EP_NO_ELIGIBLE_WORKER")
            st = execution_path_state(acct)
        self.assertEqual(st["execution_path_reason"], "worker_revoked")

    def test_reason_vocabulary_is_bounded(self):
        from execution.node_execution import EXECUTION_PATH_REASONS
        for ep, expect in [("EP_READY", "ready"), ("EP_WORKER_STALE", "worker_stale"),
                           ("EP_BRIDGE_UNHEALTHY", "bridge_unhealthy"),
                           ("EP_NODE_NOT_ACTIVE", "node_inactive"), ("EP_ROUTE_INVALID", "route_invalid"),
                           ("EP_SOMETHING_NEW", "indeterminate")]:
            acct = object()
            with mock.patch("execution.node_execution.evaluate_execution_path_readiness") as m:
                m.return_value = mock.Mock(ready=(ep == "EP_READY"), reason_code=ep)
                st = execution_path_state(acct)
            self.assertIn(st["execution_path_reason"], EXECUTION_PATH_REASONS)
            self.assertEqual(st["execution_path_reason"], expect)


# Allocation-gate E2E: reuse the certified beta-journey flag set and add the DARK execution-path gate.
_GATE_FLAGS = dict(
    HOSTED_PERSISTENT_MT5_ENABLED=True, HOSTED_WORKSPACE_ONBOARDING_ENABLED=True,
    HOSTED_MT5_EXECUTION_ENABLED=True, SUPERVISED_SINGLE_TENANT_BETA_ENABLED=True,
    BETA_MAX_TESTERS=1000,
)


class AllocationExecutionPathGateTests(TestCase):
    """request_hosted_workspace → allocate_workspace_node with the execution-path gate flag OFF (legacy,
    unchanged) and ON (fail-closed to an execution-operational node)."""

    def _request_ws(self, email):
        from billing.models import BetaTester
        from hosted_workspace import provisioning as P
        user = User.objects.create_user(username=email, email=email, password="x")
        BetaTester.objects.create(email=user.email, is_active=True)
        req = P.request_hosted_workspace(user, expected_login="60001", expected_server="GuvFX-Demo",
                                         broker_name="GuvFX Beta", is_demo=True)
        self.assertTrue(req.ok, req.reason)
        return req.workspace

    @override_settings(**_GATE_FLAGS)   # gate flag NOT set ⇒ OFF
    def test_gate_off_allocates_to_non_operational_node_unchanged(self):
        from hosted_workspace import provisioning as P
        _node(bridge="")   # no bridge, no worker ⇒ NOT execution-operational
        with mock.patch("hosted_workspace.tenant_isolation.customer_zero_account_ids",
                        return_value=frozenset()):
            ws = self._request_ws("gate.off@example.invalid")
            TerminalNode.objects.filter(hostname=N2).update(rdp_host="10.60.0.9", max_accounts=1)
            alloc = P.allocate_workspace_node(ws)
        self.assertTrue(alloc.ok, alloc.reason)          # legacy behaviour preserved (gate OFF)
        self.assertEqual(alloc.reason, P.ALLOC_OK)

    @override_settings(HOSTED_EXECUTION_PATH_GATE_ENABLED=True, **_GATE_FLAGS)
    def test_gate_on_fails_closed_without_operational_node(self):
        from hosted_workspace import provisioning as P
        _node(bridge="")   # not operational (no bridge, no worker)
        with mock.patch("hosted_workspace.tenant_isolation.customer_zero_account_ids",
                        return_value=frozenset()):
            ws = self._request_ws("gate.on@example.invalid")
            TerminalNode.objects.filter(hostname=N2).update(rdp_host="10.60.0.9", max_accounts=1)
            alloc = P.allocate_workspace_node(ws)
        self.assertFalse(alloc.ok)                       # fail closed — no lie in the read-model
        self.assertEqual(alloc.reason, P.ALLOC_NODE_NOT_EXECUTION_OPERATIONAL)
        ws.refresh_from_db()
        self.assertIsNone(ws.execution_node_id)          # never bound to an unready node

    @override_settings(HOSTED_EXECUTION_PATH_GATE_ENABLED=True, **_GATE_FLAGS)
    def test_gate_on_allocates_after_commissioning(self):
        from hosted_workspace import provisioning as P
        node = _node(bridge="http://10.60.0.9:8789")
        with mock.patch("hosted_workspace.tenant_isolation.customer_zero_account_ids",
                        return_value=frozenset()), \
             mock.patch("hosted_workspace.tenant_isolation.forbidden_execution_node_ids",
                        return_value=set()), \
             mock.patch.dict("os.environ", {NODE_WORKER_SECRET_ENV: SECRET}):
            # COMMISSION the node first (server-derived; registers the dedicated worker).
            r = commission_execution_node(node_hostname=N2, worker_id="mt5-node2-order-1", apply=True)
            self.assertTrue(r["operational"])
            # Now the automated hosted account allocates onto the operational node.
            ws = self._request_ws("gate.after@example.invalid")
            TerminalNode.objects.filter(hostname=N2).update(rdp_host="10.60.0.9", max_accounts=1)
            alloc = P.allocate_workspace_node(ws)
        self.assertTrue(alloc.ok, alloc.reason)
        self.assertEqual(alloc.reason, P.ALLOC_OK)
        ws.refresh_from_db()
        self.assertEqual(ws.execution_node_id, node.pk)
        # Before its first heartbeat the commissioned worker is registered but NOT yet a live claimant
        # (fail-closed — never a false positive). This is the honest interim the read-model must show.
        self.assertFalse(eligible_order_claimant(node).ok)
        self.assertEqual(eligible_order_claimant(node).reason, "EP_WORKER_STALE")
        # Once it comes online (polls once → last_seen stamped by the claim seam), a hypothetical
        # node-bound order would have an eligible claimant — the exact opposite of the Node-2 gap.
        WorkerIdentity.objects.filter(worker_id="mt5-node2-order-1").update(last_seen=timezone.now())
        self.assertTrue(eligible_order_claimant(node).ok)
        # … and NOTHING in this whole flow created a job or placed an order.
        self.assertEqual(ExecutionJob.objects.count(), 0)
