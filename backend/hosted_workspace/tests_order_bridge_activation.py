"""FINAL Closed-Beta stream — autonomous per-node ORDER-BRIDGE activation in prepare_hosted_slot (Stage 5c).

Covers the packet's required cases with ZERO host contact (in-memory fake executor): autonomous activation,
activation failure, health-check failure (= host ok=False), endpoint persistence, duplicate activation,
retry/idempotency, Customer-Zero isolation (reserved account / forbidden node / never-clobber-a-different-
endpoint), node routing (endpoint = node.rdp_host + configured port), telemetry, and rollback (fail-closed =
state un-advanced). The flag-OFF path is proven byte-identical to before this stream.
"""
from unittest import mock

from django.test import TestCase, override_settings

from execution.models import TerminalNode

from hosted_workspace import slot_preparation as SP
from hosted_workspace.tests_slot_preparation import FakeExecutor, _PREP_ON, _bound_ws

# prep on, with the Customer-Zero reserved-id guard disabled so a test account that happens to auto-assign
# pk=1 (the default reserved id) does not flakily short-circuit at Stage 0 — the reserved guard has its own
# dedicated test below. Autonomous bridge activation on in _ACT_ON.
_PREP_UNRESERVED = dict(_PREP_ON, HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="")
_ACT_ON = dict(_PREP_UNRESERVED, HOSTED_ORDER_BRIDGE_AUTO_ACTIVATE_ENABLED="1")


class BridgeExecutor(FakeExecutor):
    """FakeExecutor + the new ``activate_order_bridge`` host method (ok unless named in ``fail``)."""

    def activate_order_bridge(self, runtime_root, rdp_host=None):
        return self._r("activate_order_bridge")


def _url(node_pk):
    return TerminalNode.objects.filter(pk=node_pk).values_list("order_bridge_base_url", flat=True).first()


class FlagOffTests(TestCase):
    """DARK: while the flag is off the stage is skipped — byte-identical to before this stream."""

    @override_settings(**_PREP_UNRESERVED)
    def test_flag_off_skips_activation_and_still_prepares(self):
        ws, _acct, node = _bound_ws(uname="off1")
        ex = FakeExecutor()   # NOTE: base fake has NO activate_order_bridge method
        res = SP.prepare_hosted_slot(ws, executor=ex)
        self.assertTrue(res.prepared)                          # prepared without the stage
        self.assertNotIn("activate_order_bridge", ex.calls)    # never called
        self.assertEqual(_url(node.pk) or "", "")              # endpoint NOT written


class AutonomousActivationTests(TestCase):

    @override_settings(**_ACT_ON)
    def test_activation_persists_server_derived_endpoint(self):
        ws, acct, node = _bound_ws(uname="act1", rdp_host="10.9.9.9")
        ex = BridgeExecutor()
        res = SP.prepare_hosted_slot(ws, executor=ex)
        self.assertTrue(res.prepared)
        self.assertIn("activate_order_bridge", ex.calls)
        # node routing: endpoint = node.rdp_host + the configured port (default 8789).
        self.assertEqual(_url(node.pk), "http://10.9.9.9:8789")

    @override_settings(**_ACT_ON)
    def test_node_routing_uses_rdp_host_and_fixed_port(self):
        ws, _acct, node = _bound_ws(uname="rt1", rdp_host="10.1.2.3")
        res = SP.prepare_hosted_slot(ws, executor=BridgeExecutor())
        self.assertTrue(res.prepared)
        self.assertEqual(_url(node.pk), "http://10.1.2.3:%d" % SP.ORDER_BRIDGE_PORT)

    @override_settings(**_ACT_ON)
    def test_duplicate_activation_is_idempotent(self):
        ws, _acct, node = _bound_ws(uname="dup1", rdp_host="10.9.9.9")
        self.assertTrue(SP.prepare_hosted_slot(ws, executor=BridgeExecutor()).prepared)
        self.assertEqual(_url(node.pk), "http://10.9.9.9:8789")
        # Re-run: same endpoint, no clobber, still prepared (idempotent).
        res2 = SP.prepare_hosted_slot(ws, executor=BridgeExecutor())
        self.assertTrue(res2.prepared)
        self.assertEqual(_url(node.pk), "http://10.9.9.9:8789")


class ActivationFailureTests(TestCase):

    @override_settings(**_ACT_ON)
    def test_host_failure_fails_closed_no_endpoint(self):
        ws, _acct, node = _bound_ws(uname="fail1", rdp_host="10.9.9.9")
        res = SP.prepare_hosted_slot(ws, executor=BridgeExecutor(fail=["activate_order_bridge"]))
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_BRIDGE_FAILED)
        self.assertEqual(res.stage_reached, SP.ST_BRIDGE)
        self.assertEqual(_url(node.pk) or "", "")              # never persisted on failure

    @override_settings(**_ACT_ON)
    def test_missing_method_is_executor_incomplete(self):
        ws, _acct, node = _bound_ws(uname="inc1")
        # base FakeExecutor has no activate_order_bridge → required step → EXECUTOR_INCOMPLETE, fail closed.
        res = SP.prepare_hosted_slot(ws, executor=FakeExecutor())
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_EXECUTOR_INCOMPLETE)
        self.assertEqual(res.stage_reached, SP.ST_BRIDGE)
        self.assertEqual(_url(node.pk) or "", "")

    @override_settings(**_ACT_ON)
    def test_host_exception_fails_closed(self):
        ws, _acct, node = _bound_ws(uname="raise1")
        res = SP.prepare_hosted_slot(ws, executor=BridgeExecutor(raise_at="activate_order_bridge"))
        self.assertFalse(res.prepared)
        self.assertEqual(res.stage_reached, SP.ST_BRIDGE)      # host_step_error sanitised → still fail closed
        self.assertEqual(_url(node.pk) or "", "")


class CustomerZeroIsolationTests(TestCase):

    @override_settings(**_ACT_ON)
    def test_never_clobbers_a_different_endpoint(self):
        # A node already routing elsewhere (e.g. Customer Zero's :8788) is NEVER overwritten.
        ws, _acct, node = _bound_ws(uname="cz1", rdp_host="10.9.9.9")
        TerminalNode.objects.filter(pk=node.pk).update(order_bridge_base_url="http://10.9.9.9:8788")
        ex = BridgeExecutor()
        res = SP.prepare_hosted_slot(ws, executor=ex)
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_BRIDGE_ENDPOINT_CONFLICT)
        self.assertNotIn("activate_order_bridge", ex.calls)    # refused BEFORE any host contact
        self.assertEqual(_url(node.pk), "http://10.9.9.9:8788") # left untouched

    @override_settings(**dict(_ACT_ON, HOSTED_BETA_FORBIDDEN_RDP_HOSTS=("10.9.9.9",)))
    def test_forbidden_node_refused(self):
        ws, _acct, node = _bound_ws(uname="cz2", rdp_host="10.9.9.9")
        ex = BridgeExecutor()
        res = SP.prepare_hosted_slot(ws, executor=ex)
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_BRIDGE_FORBIDDEN_NODE)
        self.assertNotIn("activate_order_bridge", ex.calls)
        self.assertEqual(_url(node.pk) or "", "")

    def test_reserved_account_refused_before_any_stage(self):
        ws, acct, node = _bound_ws(uname="cz3", rdp_host="10.9.9.9")
        ex = BridgeExecutor()
        with override_settings(**dict(_ACT_ON, HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS=str(acct.pk))):
            res = SP.prepare_hosted_slot(ws, executor=ex)
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_REFUSED_RESERVED)
        self.assertNotIn("activate_order_bridge", ex.calls)    # never reached the host at all
        self.assertEqual(_url(node.pk) or "", "")


class TelemetryTests(TestCase):

    @override_settings(**_ACT_ON)
    def test_activated_event_emitted_on_success(self):
        from hosted_workspace.telemetry import WorkspaceEvent
        ws, _acct, _node2 = _bound_ws(uname="tel1", rdp_host="10.9.9.9")
        with mock.patch("hosted_workspace.telemetry.emit_workspace_event") as m:
            res = SP.prepare_hosted_slot(ws, executor=BridgeExecutor())
        self.assertTrue(res.prepared)
        events = [c.args[0] for c in m.call_args_list]
        self.assertIn(WorkspaceEvent.ORDER_BRIDGE_ACTIVATED, events)
        self.assertNotIn(WorkspaceEvent.ORDER_BRIDGE_ACTIVATION_FAILED, events)

    @override_settings(**_ACT_ON)
    def test_failed_event_emitted_on_host_failure(self):
        from hosted_workspace.telemetry import WorkspaceEvent
        ws, _acct, _node2 = _bound_ws(uname="tel2", rdp_host="10.9.9.9")
        with mock.patch("hosted_workspace.telemetry.emit_workspace_event") as m:
            res = SP.prepare_hosted_slot(ws, executor=BridgeExecutor(fail=["activate_order_bridge"]))
        self.assertFalse(res.prepared)
        events = [c.args[0] for c in m.call_args_list]
        self.assertIn(WorkspaceEvent.ORDER_BRIDGE_ACTIVATION_FAILED, events)
        self.assertNotIn(WorkspaceEvent.ORDER_BRIDGE_ACTIVATED, events)


class PrimitiveContractTests(TestCase):
    """The new op is wired end-to-end through the allow-lists (backend + host runner) without drift."""

    def test_operation_and_primitive_registered(self):
        from hosted_workspace.host_agent_dispatch import OP_PRIMITIVES, is_known_primitive
        from hosted_workspace.host_protocol import HOSTED_OPERATIONS
        self.assertIn("ACTIVATE_ORDER_BRIDGE", HOSTED_OPERATIONS)
        self.assertIn("ACTIVATE_ORDER_BRIDGE", OP_PRIMITIVES)
        self.assertEqual(OP_PRIMITIVES["ACTIVATE_ORDER_BRIDGE"]["primitive"], "activate_order_bridge")
        self.assertTrue(is_known_primitive("activate_order_bridge"))

    def test_port_is_single_source_of_truth_across_backend_primitive_launcher(self):
        # The order-bridge port must be ONE value across the Django constant, the activation primitive that
        # binds/health-checks it, and the launcher — a divergence would persist a routing URL to a port the
        # host never binds (and =8788 would route beta orders to Customer Zero's bridge). Static: no host, no
        # fake — reads the real files so a future edit to any one of the three fails the suite.
        import re
        from pathlib import Path

        from hosted_workspace.slot_preparation import ORDER_BRIDGE_PORT
        root = Path(__file__).resolve().parents[2]
        ps1 = (root / "backend/terminal_provisioning/windows/Activate-GuvfxOrderBridge.ps1").read_text()
        bat = (root / "deploy/node2-order-bridge/start_node2_bridge.bat").read_text()
        ps1_port = int(re.search(r"\$PORT\s*=\s*(\d+)", ps1).group(1))
        bat_port = int(re.search(r"HTTP_SERVER_PORT=(\d+)", bat).group(1))
        self.assertEqual(ORDER_BRIDGE_PORT, 8789)
        self.assertEqual(ps1_port, ORDER_BRIDGE_PORT)
        self.assertEqual(bat_port, ORDER_BRIDGE_PORT)
        self.assertNotEqual(ORDER_BRIDGE_PORT, 8788)   # never Customer Zero's legacy bridge port

    def test_executor_method_confines_and_refuses_customer_zero(self):
        from hosted_workspace.host_executor import SignedHostExecutor
        sent = {}

        def _transport(base_url, req):
            sent["op"] = req.get("operation")
            return {"ok": True}

        # A reserved (Customer Zero) executor refuses to send at all.
        cz = SignedHostExecutor(account_id=1, rdp_host="h", transport=_transport, keyring={"k": "0" * 64},
                                key_id="k", base_url="http://h", seal_password=lambda *a, **k: {}, reserved_ids={1})
        out = cz.activate_order_bridge(r"C:\GuvFX\accounts\1\terminal")
        self.assertFalse(out["ok"])
        self.assertNotIn("op", sent)   # never dispatched for Customer Zero
