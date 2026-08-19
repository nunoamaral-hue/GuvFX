"""P0-B1.1 — per-tenant bridge activation wired into prepare_hosted_slot Stage 5c + the signed-executor
``activate_tenant_bridge`` primitive. Proves: flag ON allocates a per-tenant endpoint + activates its OWN
bridge on a per-tenant port (8800-8899) + marks it READY, writing NO node-global :8789; flag OFF is
byte-identical to the legacy per-node path; activation failure fails closed (endpoint not READY); the port
param is signed + range-validated; and the seed command puts existing tenants on their current bridge.
"""
from unittest import mock

from django.test import TestCase, override_settings

from execution.models import HostedExecutionEndpoint, TerminalNode
from hosted_workspace import host_agent_dispatch as D
from hosted_workspace import host_protocol as P
from hosted_workspace import slot_preparation as SP
from hosted_workspace.host_protocol import HostProtocolError
from hosted_workspace.tests_order_bridge_activation import _ACT_ON, BridgeExecutor, _url
from hosted_workspace.tests_slot_preparation import _bound_ws

_PT_ON = dict(_ACT_ON, HOSTED_PER_TENANT_TRANSPORT_ENABLED="1")


class TenantBridgeExecutor(BridgeExecutor):
    """BridgeExecutor + the per-tenant primitive; records the port it was asked to activate."""
    def activate_tenant_bridge(self, runtime_root, port, rdp_host=None):
        self.tenant_port = port
        return self._r("activate_tenant_bridge")


def _intent_ws(**kw):
    ws, acct, node = _bound_ws(**kw)
    acct.is_active = False                      # a NEW hosted tenant is an intent account at provisioning
    acct.save(update_fields=["is_active"])
    return ws, acct, node


class PerTenantActivationTests(TestCase):
    def setUp(self):
        # Neutralise the CZ forbidden-node guard: PostgreSQL sequences are not rolled back between tests, so a
        # test account can auto-assign pk=1 (Customer Zero) and trip Guard A before the per-tenant path. CZ-node
        # isolation is covered by tests_order_bridge_activation; here we isolate the per-tenant behaviour.
        p = mock.patch("hosted_workspace.tenant_isolation.forbidden_execution_node_ids", return_value=frozenset())
        p.start()
        self.addCleanup(p.stop)

    @override_settings(**_PT_ON)
    def test_flag_on_allocates_endpoint_activates_tenant_bridge_marks_ready(self):
        ws, acct, node = _intent_ws(uname="pt1", rdp_host="10.9.9.9")
        ex = TenantBridgeExecutor()
        res = SP.prepare_hosted_slot(ws, executor=ex)
        self.assertTrue(res.prepared, res.reason)
        self.assertIn("activate_tenant_bridge", ex.calls)          # per-tenant primitive used
        self.assertNotIn("activate_order_bridge", ex.calls)        # NOT the legacy node primitive
        ep = HostedExecutionEndpoint.objects.get(workspace=ws)
        self.assertEqual(ep.state, HostedExecutionEndpoint.State.READY)
        self.assertIn(ep.port, range(8800, 8900))                  # per-tenant port, not 8789
        self.assertEqual(ex.tenant_port, ep.port)                  # activated on the allocated port
        self.assertEqual(ep.base_url, "http://10.9.9.9:%d" % ep.port)
        self.assertEqual(_url(node.pk) or "", "")                  # node-global :8789 NOT written

    @override_settings(**_ACT_ON)   # per-tenant flag OFF
    def test_flag_off_is_byte_identical_legacy_node_bridge(self):
        ws, acct, node = _intent_ws(uname="pt2", rdp_host="10.1.2.3")
        ex = TenantBridgeExecutor()
        res = SP.prepare_hosted_slot(ws, executor=ex)
        self.assertTrue(res.prepared, res.reason)
        self.assertIn("activate_order_bridge", ex.calls)           # legacy path
        self.assertNotIn("activate_tenant_bridge", ex.calls)
        self.assertEqual(_url(node.pk), "http://10.1.2.3:%d" % SP.ORDER_BRIDGE_PORT)   # :8789
        self.assertFalse(HostedExecutionEndpoint.objects.filter(workspace=ws).exists())  # no endpoint

    @override_settings(**_PT_ON)
    def test_activation_failure_fails_closed_endpoint_not_ready(self):
        ws, acct, node = _intent_ws(uname="pt3", rdp_host="10.9.9.9")
        ex = TenantBridgeExecutor(fail={"activate_tenant_bridge"})
        res = SP.prepare_hosted_slot(ws, executor=ex)
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_BRIDGE_FAILED)
        # endpoint was allocated but NEVER marked READY (unproven bridge never routes)
        ep = HostedExecutionEndpoint.objects.filter(workspace=ws).first()
        if ep is not None:
            self.assertNotEqual(ep.state, HostedExecutionEndpoint.State.READY)


class DispatchPortParamTests(TestCase):
    def test_op_is_registered_and_covered(self):
        self.assertIn("ACTIVATE_TENANT_BRIDGE", P.HOSTED_OPERATIONS)
        self.assertIn("ACTIVATE_TENANT_BRIDGE", D.OP_PRIMITIVES)
        self.assertEqual(D.OP_PRIMITIVES["ACTIVATE_TENANT_BRIDGE"]["params_allow"], ("port",))

    def test_valid_port_builds_args_with_account_and_port(self):
        slot = {"username": "guvfx_u_9", "runtime_root": r"C:\GuvFX\accounts\9",
                "terminal_root": r"C:\GuvFX\accounts\9\terminal", "account_id": 9}
        args = D._build_args("ACTIVATE_TENANT_BRIDGE", slot, {"params": {"port": 8801}}, envelope_open=None)
        self.assertEqual(args["port"], 8801)
        self.assertEqual(args["account_id"], 9)
        self.assertEqual(args["terminal_root"], r"C:\GuvFX\accounts\9\terminal")

    def test_reserved_or_out_of_range_port_rejected(self):
        slot = {"username": "u", "runtime_root": "r", "terminal_root": "t", "account_id": 9}
        for bad in (8789, 8799, 8900, 9000):
            with self.assertRaises(HostProtocolError):
                D._build_args("ACTIVATE_TENANT_BRIDGE", slot, {"params": {"port": bad}}, envelope_open=None)

    def test_missing_port_rejected(self):
        slot = {"username": "u", "runtime_root": "r", "terminal_root": "t", "account_id": 9}
        with self.assertRaises(HostProtocolError):
            D._build_args("ACTIVATE_TENANT_BRIDGE", slot, {"params": {}}, envelope_open=None)

    def test_validate_params_rejects_nonport_key(self):
        with self.assertRaises(HostProtocolError):
            D._validate_params("ACTIVATE_TENANT_BRIDGE", {"evil": 1})


class ExecutorProxyTests(TestCase):
    def test_activate_tenant_bridge_sends_signed_port_param_confined(self):
        from hosted_workspace.host_executor import SignedHostExecutor
        sent = {}
        ex = SignedHostExecutor.__new__(SignedHostExecutor)
        ex._confined = lambda **kw: True
        ex._send = lambda op, params=None, **kw: sent.update(op=op, params=params) or {"ok": True}
        r = ex.activate_tenant_bridge(runtime_root=r"C:\GuvFX\accounts\9", port=8801)
        self.assertTrue(r["ok"])
        self.assertEqual(sent["op"], "ACTIVATE_TENANT_BRIDGE")
        self.assertEqual(sent["params"], {"port": 8801})

    def test_confinement_mismatch_fails_closed(self):
        from hosted_workspace.host_executor import SignedHostExecutor
        ex = SignedHostExecutor.__new__(SignedHostExecutor)
        ex._confined = lambda **kw: False
        r = ex.activate_tenant_bridge(runtime_root="x", port=8801)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "confinement_mismatch")


class SeedExistingTenantsTests(TestCase):
    """The seed command puts existing live tenants on their CURRENT per-node bridge (no re-home)."""

    def _live_ws(self, login, uname, port):
        ws, acct, node = _bound_ws(login=login, uname=uname, rdp_host="100.79.101.19")
        node.order_bridge_base_url = "http://100.79.101.19:%d" % port
        node.save(update_fields=["order_bridge_base_url"])
        acct.is_active = True                       # existing live tenant (support@/CZ style)
        acct.save(update_fields=["is_active"])
        from terminal_provisioning.models import AccountProvisioning
        AccountProvisioning.objects.create(
            trading_account=acct, windows_username=uname, runtime_root=rf"C:\GuvFX\accounts\{acct.pk}",
            status=AccountProvisioning.Status.PROVISIONED)
        return ws, acct

    def test_seed_apply_puts_tenants_on_their_current_bridge(self):
        from django.core.management import call_command
        from io import StringIO
        wsA, a = self._live_ws("1302587", "guvfx_u_25", 8789)      # support@ style
        wsB, b = self._live_ws("1302561", "guvfx_u_1", 8788)       # CZ style
        call_command("seed_hosted_endpoints", "--apply", stdout=StringIO())
        epA = HostedExecutionEndpoint.objects.get(workspace=wsA)
        epB = HostedExecutionEndpoint.objects.get(workspace=wsB)
        self.assertEqual((epA.base_url, epA.state), ("http://100.79.101.19:8789", HostedExecutionEndpoint.State.READY))
        self.assertEqual((epB.base_url, epB.state), ("http://100.79.101.19:8788", HostedExecutionEndpoint.State.READY))
        # idempotent — a second run keeps one endpoint per workspace on the same port
        call_command("seed_hosted_endpoints", "--apply", stdout=StringIO())
        self.assertEqual(HostedExecutionEndpoint.objects.filter(workspace=wsA).count(), 1)
        self.assertEqual(HostedExecutionEndpoint.objects.get(workspace=wsA).port, 8789)

    def test_dry_run_writes_nothing(self):
        from django.core.management import call_command
        from io import StringIO
        wsA, a = self._live_ws("1302587", "guvfx_u_25b", 8789)
        call_command("seed_hosted_endpoints", stdout=StringIO())   # no --apply
        self.assertFalse(HostedExecutionEndpoint.objects.filter(workspace=wsA).exists())
