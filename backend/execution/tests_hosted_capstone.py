"""ADR-0034 Execution Engine CAPSTONE — durable workspace->node binding + provisioning contract + routing
invariants (PARTS 2/3/5/6/12).

Proves: the provisioning contract (assign/clear, versioned, idempotent, fail-closed); a workspace resolves
to exactly ONE authorised node (NULL/mismatch ⇒ not routable); the claim seam rejects a job whose node
drifted from the account's node; arming refuses an unbound/mismatched workspace; the expected broker
identity is SERVER-derived (a forged payload cannot authorise); and each job re-resolves from its own
durable server-side truth (no stale identity leaks from a previous job).
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from trading.models import BrokerServer, TradingAccount

from execution import hosted_provisioning as P
from execution import hosted_routing as HR
from execution.models import TerminalNode
from execution.readiness import PERSISTENT_WORKSPACE
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()


def _account(*, login="700900", server="IS6-Demo", node=True, ws=True, armed=True, bind=True):
    user = U.objects.create_user(username=f"c{login}", email=f"{login}@x.invalid", password="x")
    srv, _ = BrokerServer.objects.get_or_create(server_name=server)
    tn = TerminalNode.objects.create(hostname=f"node-{login}") if node else None
    acct = TradingAccount.objects.create(
        user=user, name="a", broker_name="B", account_number=login, is_demo=True,
        broker_server=srv, readiness_provider=PERSISTENT_WORKSPACE, terminal_node=tn)
    if ws:
        HostedMt5Workspace.objects.create(
            trading_account=acct, canonical_state=S.EXECUTION_READY, proj_connected=True,
            proj_trade_allowed=True, proj_account_match=True, proj_execution_ready=True,
            last_decision_at=timezone.now(), execution_enabled=armed,
            execution_node=(tn if bind else None))
    return acct


class ProvisioningContractTests(TestCase):
    def test_assign_sets_node_and_versions(self):
        acct = _account(bind=False)
        node = acct.terminal_node
        P.assign_workspace_execution_node(acct, node, actor="admin")
        ws = acct.hosted_workspace
        ws.refresh_from_db()
        self.assertEqual(ws.execution_node_id, node.pk)
        self.assertEqual(ws.execution_binding_generation, 1)

    def test_assign_is_idempotent_no_extra_generation(self):
        acct = _account(bind=False)
        P.assign_workspace_execution_node(acct, acct.terminal_node)
        P.assign_workspace_execution_node(acct, acct.terminal_node)  # idempotent
        acct.hosted_workspace.refresh_from_db()
        self.assertEqual(acct.hosted_workspace.execution_binding_generation, 1)

    def test_reassign_bumps_generation(self):
        acct = _account(bind=True)  # generation 0 (created with node directly)
        other = TerminalNode.objects.create(hostname="node-other")
        P.assign_workspace_execution_node(acct, other)
        acct.hosted_workspace.refresh_from_db()
        self.assertEqual(acct.hosted_workspace.execution_node_id, other.pk)
        self.assertEqual(acct.hosted_workspace.execution_binding_generation, 1)

    def test_clear_unbinds_and_versions(self):
        acct = _account(bind=True)
        P.clear_workspace_execution_node(acct)
        acct.hosted_workspace.refresh_from_db()
        self.assertIsNone(acct.hosted_workspace.execution_node_id)
        self.assertEqual(acct.hosted_workspace.execution_binding_generation, 1)

    def test_assign_none_node_fails_closed(self):
        acct = _account(bind=False)
        self.assertIsNone(P.assign_workspace_execution_node(acct, None))
        acct.hosted_workspace.refresh_from_db()
        self.assertIsNone(acct.hosted_workspace.execution_node_id)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class RoutingBindingTests(TestCase):
    def test_bound_armed_agree_routes_ok(self):
        acct = _account()  # armed + bound + agree
        r = HR.resolve_hosted_route(acct)
        self.assertTrue(r.ok, r.reason_code)
        self.assertEqual(r.expected_login, "700900")

    def test_armed_but_binding_cleared_not_routable(self):
        acct = _account()
        P.clear_workspace_execution_node(acct)   # cleared after arming (clear does not disarm)
        acct = TradingAccount.objects.get(pk=acct.pk)
        self.assertEqual(HR.resolve_hosted_route(acct).reason_code, HR.ER_NODE_UNBOUND)

    def test_binding_mismatch_not_routable(self):
        acct = _account()
        other = TerminalNode.objects.create(hostname="node-elsewhere")
        ws = acct.hosted_workspace
        ws.execution_node = other       # binding disagrees with account.terminal_node
        ws.save(update_fields=["execution_node"])
        acct = TradingAccount.objects.get(pk=acct.pk)
        self.assertEqual(HR.resolve_hosted_route(acct).reason_code, HR.ER_NODE_MISMATCH)

    def test_claim_rejects_job_node_drift(self):
        acct = _account()
        other = TerminalNode.objects.create(hostname="node-drift")

        class _J:
            def __init__(s):
                s.account = acct
                s.terminal_node_id = other.pk      # job node != account node
                s.job_type = "PLACE_ORDER"
        self.assertEqual(HR.authorize_hosted_claim(_J(), worker_is_node_aware=True).reason_code,
                         HR.ER_NODE_MISMATCH)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class ArmBindingPreconditionTests(TestCase):
    def test_arm_refuses_unbound(self):
        acct = _account(armed=False, bind=False)
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, P.ARM_NODE_UNBOUND)

    def test_arm_refuses_mismatch(self):
        acct = _account(armed=False, bind=True)
        other = TerminalNode.objects.create(hostname="node-mismatch")
        ws = acct.hosted_workspace
        ws.execution_node = other
        ws.save(update_fields=["execution_node"])
        acct = TradingAccount.objects.get(pk=acct.pk)
        self.assertEqual(P.arm_hosted_workspace_execution(acct).reason_code, P.ARM_NODE_MISMATCH)

    def test_arm_ok_when_bound_and_agree(self):
        acct = _account(armed=False, bind=True)
        self.assertTrue(P.arm_hosted_workspace_execution(acct).ok)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class ServerSideIdentityTests(TestCase):
    def test_expected_identity_is_server_derived_not_payload(self):
        """PART 6: the route's expected login/server come from the account's durable bindings, never from a
        client payload. A forged payload cannot change the authorised identity."""
        acct = _account(login="700900", server="IS6-Demo")
        r = HR.resolve_hosted_route(acct)  # takes only the account — no payload input exists to forge
        self.assertEqual(r.expected_login, "700900")
        self.assertEqual(r.expected_server, "IS6-Demo")

    def test_each_account_resolves_its_own_identity_no_stale_leak(self):
        """PART 5: two different accounts resolve independently from their OWN durable truth; account A's
        identity can never authorise account B (resolve is stateless per account)."""
        a = _account(login="700111", server="Srv-A")
        b = _account(login="800222", server="Srv-B")
        ra = HR.resolve_hosted_route(a)
        rb = HR.resolve_hosted_route(b)
        self.assertEqual((ra.expected_login, ra.expected_server), ("700111", "Srv-A"))
        self.assertEqual((rb.expected_login, rb.expected_server), ("800222", "Srv-B"))
        # B's route never inherits A's node/login even if evaluated right after A.
        self.assertNotEqual(rb.expected_login, ra.expected_login)


class SinglePathProofTests(TestCase):
    def test_no_broker_api_import_in_hosted_backend(self):
        """PART 15: there is NO alternative hosted execution path. The hosted BACKEND modules route / gate /
        record only — none of them imports the broker API (``MetaTrader5``), so none can call
        ``order_send``/``login``. The SINGLE order path is the certified bridge
        (``scripts/mt5_signal_bridge.py``), which alone imports MetaTrader5 and performs the gate + send."""
        import subprocess
        hosted_backend = [
            "execution/hosted_routing.py", "execution/hosted_pin.py", "execution/hosted_provisioning.py",
            "execution/hosted_execution.py", "execution/hosted_reconcile.py",
            "execution/hosted_switch_policy.py", "execution/hosted_idempotency.py",
            "execution/readiness.py", "execution/broker_gate.py",
            "hosted_workspace/persistence.py", "hosted_workspace/manager.py",
            "hosted_workspace/consumer.py", "hosted_workspace/observation_runner.py",
            "hosted_workspace/agent.py", "hosted_workspace/producer.py",
        ]
        # Match an actual import STATEMENT (line-start import/from), not a docstring mention.
        out = subprocess.run(
            ["grep", "-rElE", r"^[[:space:]]*(import|from)[[:space:]]+MetaTrader5", *hosted_backend],
            capture_output=True, text=True).stdout.split()
        self.assertEqual(out, [], f"hosted backend must not import the broker API: {out}")


class IdempotencyKeyCollisionTests(TestCase):
    """Mutation-adequacy for the pure ``hosted_idempotency_key``: it is deterministic AND every intended
    component (workspace / login / server / job / operation / strategy) genuinely participates — changing any
    one yields a different key, so the same logical order can never collide across users/workspaces/jobs/ops."""

    def test_deterministic_and_every_component_matters(self):
        from execution.hosted_idempotency import hosted_idempotency_key as K
        base = dict(workspace_uuid="ws-1", expected_login="700111", expected_server="Srv-A",
                    job_id=1, operation="PLACE_ORDER", strategy_id="5")
        baseline = K(**base)
        self.assertTrue(baseline.startswith("HWX-"))
        self.assertEqual(K(**base), baseline)  # deterministic
        for field, alt in [("workspace_uuid", "ws-2"), ("expected_login", "800222"),
                           ("expected_server", "Srv-B"), ("job_id", 2),
                           ("operation", "CLOSE_TRADE"), ("strategy_id", "6")]:
            mut = dict(base); mut[field] = alt
            self.assertNotEqual(K(**mut), baseline, f"key MUST change when {field} changes (collision risk)")


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_MT5_EXECUTION_ENABLED="1")
class FailClosedBranchTests(TestCase):
    def test_readiness_not_ready_when_trade_not_allowed(self):
        from execution.readiness import RW_WORKSPACE_NOT_READY, PersistentWorkspaceProvider
        acct = _account()  # armed + bound + connected + matched
        ws = acct.hosted_workspace
        ws.proj_trade_allowed = False       # connected+matched but trading halted → not ready
        ws.canonical_state = S.CONNECTED    # so canonical_execution_ready is False too
        ws.save(update_fields=["proj_trade_allowed", "canonical_state"])
        d = PersistentWorkspaceProvider().evaluate(TradingAccount.objects.get(pk=acct.pk))
        self.assertFalse(d.eligible)
        self.assertEqual(d.reason_code, RW_WORKSPACE_NOT_READY)

    def test_route_binding_mismatch_on_empty_login(self):
        # Armed + node-bound + agree, but the account carries no bound login → the route cannot be safely
        # pinned → ER_BINDING_MISMATCH (fail closed rather than route an unpinnable order).
        acct = _account(login="700900")
        acct.account_number = ""            # no bound broker login
        acct.save(update_fields=["account_number"])
        r = HR.resolve_hosted_route(TradingAccount.objects.get(pk=acct.pk))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, HR.ER_BINDING_MISMATCH)
