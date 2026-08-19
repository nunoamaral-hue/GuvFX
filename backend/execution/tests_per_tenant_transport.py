"""P0-B1 — per-tenant order-execution transport: endpoint model + allocator + lifecycle + account-aware
routing + bridge config. The core safety proof is PHASE 5/6/9: a job for Customer A can resolve ONLY
Customer A's endpoint, every mismatch fails CLOSED (never another tenant, never the node URL, never the
global bridge), and the DARK flag-OFF path is byte-identical to the pre-change per-node behaviour (support@
untouched).
"""
from __future__ import annotations

import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from execution import endpoint_service as svc
from execution.bridge_config import render_bridge_env
from execution.models import ExecutionJob, HostedExecutionEndpoint, TerminalNode
from execution.order_transport import (
    OT_ENDPOINT_NOT_READY,
    OT_ENDPOINT_UNCONFIGURED,
    OT_LEGACY_GLOBAL,
    OT_NODE_MISMATCH,
    OT_NODE_OK,
    resolve_order_transport,
)
from execution.readiness import PERSISTENT_WORKSPACE
from hosted_workspace.models import HostedMt5Workspace
from terminal_provisioning.models import AccountProvisioning
from trading.models import TradingAccount

GLOBAL = "http://guvfx-agent:8788"
BOTH_FLAGS = {"HOSTED_PERSISTENT_MT5_ENABLED": "1", "HOSTED_PER_TENANT_TRANSPORT_ENABLED": "1"}
DARK_TRANSPORT = {"HOSTED_PERSISTENT_MT5_ENABLED": "1", "HOSTED_PER_TENANT_TRANSPORT_ENABLED": "0"}


def _make_tenant(login, windows_username, *, node, host="100.79.101.19", is_demo=True, is_active=False,
                 broker_server=None):
    """Create a full hosted (Provider-B) tenant: user, account, workspace, PROVISIONED isolation profile.
    A NEW hosted tenant is an intent account (``is_active=False``) at endpoint-allocation time; live accounts
    (support@/CZ) are ``is_active=True`` and must be seeded explicitly (the re-home guard). The hosted broker
    identity is write-once, so ``broker_server`` is set at creation."""
    user = get_user_model().objects.create_user(
        username=f"t-{login}", email=f"t-{login}@x.invalid", password="x")
    acct = TradingAccount.objects.create(
        user=user, name="a", broker_name="B", account_number=login, is_demo=is_demo, is_active=is_active,
        readiness_provider=PERSISTENT_WORKSPACE, terminal_node=node, broker_server=broker_server)
    ws = HostedMt5Workspace.objects.create(trading_account=acct, execution_node=node, workspace_node=node)
    AccountProvisioning.objects.create(
        trading_account=acct, windows_username=windows_username,
        runtime_root=fr"C:\GuvFX\accounts\{acct.id}", status=AccountProvisioning.Status.PROVISIONED)
    return acct, ws


def _job(acct, node):
    return ExecutionJob.objects.create(
        account=acct, terminal_node=node, job_type=ExecutionJob.JobType.CLOSE_TRADE, payload={"ticket": 1})


class PortAllocatorTests(TestCase):
    def setUp(self):
        self.node = TerminalNode.objects.create(hostname="beta-pa", rdp_host="10.0.0.1")

    def _live(self, host, port, acct_login):
        acct, ws = _make_tenant(acct_login, f"guvfx_u_{acct_login}", node=self.node, host=host)
        return HostedExecutionEndpoint.objects.create(
            workspace=ws, trading_account=acct, terminal_node=self.node, host=host, port=port,
            base_url=f"http://{host}:{port}", windows_username=f"guvfx_u_{acct_login}",
            runtime_path="x", workspace_uuid=ws.workspace_uuid, state=HostedExecutionEndpoint.State.READY)

    def test_lowest_free_in_range(self):
        self.assertEqual(svc.allocate_port("10.0.0.1"), svc.PORT_RANGE_START)  # 8800

    def test_skips_reserved_ports(self):
        # No reserved port is ever handed out even though it is in numeric range-adjacency.
        for p in range(svc.PORT_RANGE_START, svc.PORT_RANGE_START + 5):
            self.assertNotIn(p, svc.RESERVED_PORTS)  # 8800.. are clear of 8787/8788/8789/8791

    def test_excludes_live_ports_and_is_lowest(self):
        self._live("10.0.0.1", svc.PORT_RANGE_START, "111")
        self.assertEqual(svc.allocate_port("10.0.0.1"), svc.PORT_RANGE_START + 1)

    def test_retired_port_is_reclaimed(self):
        ep = self._live("10.0.0.1", svc.PORT_RANGE_START, "222")
        ep.state = HostedExecutionEndpoint.State.RETIRED
        ep.save()
        # retired frees the port -> the lowest free is the retired one again
        self.assertEqual(svc.allocate_port("10.0.0.1"), svc.PORT_RANGE_START)

    def test_per_host_scoping(self):
        self._live("10.0.0.1", svc.PORT_RANGE_START, "333")
        # a DIFFERENT host is unaffected -> gets the lowest port
        self.assertEqual(svc.allocate_port("10.0.0.9"), svc.PORT_RANGE_START)

    def test_db_unique_constraint_blocks_double_live_port(self):
        self._live("10.0.0.1", svc.PORT_RANGE_START, "444")
        acct, ws = _make_tenant("445", "guvfx_u_445", node=self.node)
        with self.assertRaises(IntegrityError), transaction.atomic():
            HostedExecutionEndpoint.objects.create(
                workspace=ws, trading_account=acct, terminal_node=self.node, host="10.0.0.1",
                port=svc.PORT_RANGE_START, base_url="x", windows_username="guvfx_u_445",
                runtime_path="x", workspace_uuid=ws.workspace_uuid,
                state=HostedExecutionEndpoint.State.ALLOCATED)


class EndpointServiceTests(TestCase):
    def setUp(self):
        self.node = TerminalNode.objects.create(hostname="beta-es", rdp_host="100.79.101.19")

    def test_allocate_creates_allocated_not_routable(self):
        acct, ws = _make_tenant("501", "guvfx_u_501", node=self.node)
        res = svc.allocate_endpoint(ws)
        self.assertTrue(res.ok)
        ep = HostedExecutionEndpoint.objects.get(workspace=ws)
        self.assertEqual(ep.state, HostedExecutionEndpoint.State.ALLOCATED)
        self.assertFalse(ep.is_routable)                       # ALLOCATED never routes
        self.assertEqual(ep.base_url, f"http://100.79.101.19:{ep.port}")
        self.assertEqual(ep.windows_username, "guvfx_u_501")   # server-derived
        self.assertEqual(ep.workspace_uuid, ws.workspace_uuid)
        self.assertIn(ep.port, range(svc.PORT_RANGE_START, svc.PORT_RANGE_END + 1))

    def test_allocate_idempotent(self):
        acct, ws = _make_tenant("502", "guvfx_u_502", node=self.node)
        a = svc.allocate_endpoint(ws)
        b = svc.allocate_endpoint(ws)
        self.assertEqual(a.endpoint_id, b.endpoint_id)
        self.assertEqual(a.port, b.port)
        self.assertEqual(HostedExecutionEndpoint.objects.filter(workspace=ws).count(), 1)

    def test_mark_ready_then_routable(self):
        acct, ws = _make_tenant("503", "guvfx_u_503", node=self.node)
        svc.allocate_endpoint(ws)
        svc.mark_ready(ws, health_ok=True)
        ep = HostedExecutionEndpoint.objects.get(workspace=ws)
        self.assertEqual(ep.state, HostedExecutionEndpoint.State.READY)
        self.assertTrue(ep.is_routable)
        self.assertIsNotNone(ep.activated_at)

    def test_mark_ready_health_fail_stays_allocated(self):
        acct, ws = _make_tenant("504", "guvfx_u_504", node=self.node)
        svc.allocate_endpoint(ws)
        svc.mark_ready(ws, health_ok=False)
        ep = HostedExecutionEndpoint.objects.get(workspace=ws)
        self.assertEqual(ep.state, HostedExecutionEndpoint.State.ALLOCATED)  # unproven bridge never routes
        self.assertIs(ep.last_health_ok, False)

    def test_retire_frees_port_and_blocks_routing(self):
        acct, ws = _make_tenant("505", "guvfx_u_505", node=self.node)
        svc.allocate_endpoint(ws)
        svc.mark_ready(ws, health_ok=True)
        port = HostedExecutionEndpoint.objects.get(workspace=ws).port
        svc.retire_endpoint(ws)
        ep = HostedExecutionEndpoint.objects.get(workspace=ws)
        self.assertEqual(ep.state, HostedExecutionEndpoint.State.RETIRED)
        self.assertFalse(ep.is_routable)
        self.assertEqual(svc.allocate_port("100.79.101.19"), port)  # port reclaimed (lowest free)

    def test_reactivate_retired_reuses_row(self):
        acct, ws = _make_tenant("506", "guvfx_u_506", node=self.node)
        svc.allocate_endpoint(ws)
        svc.retire_endpoint(ws)
        res = svc.allocate_endpoint(ws)
        self.assertEqual(res.reason, svc.EP_REACTIVATED)
        self.assertEqual(HostedExecutionEndpoint.objects.filter(workspace=ws).count(), 1)  # same row
        self.assertEqual(HostedExecutionEndpoint.objects.get(workspace=ws).state,
                         HostedExecutionEndpoint.State.ALLOCATED)

    def test_explicit_port_seeds_existing_bridge_without_new_allocation(self):
        # support@ preservation: seed its EXISTING :8789 bridge — no 88xx port is minted.
        acct, ws = _make_tenant("1302587", "guvfx_u_25", node=self.node)
        res = svc.allocate_endpoint(ws, explicit_port=8789, explicit_base_url="http://100.79.101.19:8789")
        self.assertEqual(res.port, 8789)
        self.assertEqual(res.base_url, "http://100.79.101.19:8789")

    def test_missing_windows_identity_fails_closed(self):
        # No PROVISIONED AccountProvisioning -> hosted_windows_username_for() empty -> refuse.
        user = get_user_model().objects.create_user(username="noid", email="noid@x.invalid", password="x")
        acct = TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number="9",
                                             is_demo=True, readiness_provider=PERSISTENT_WORKSPACE,
                                             terminal_node=self.node)
        ws = HostedMt5Workspace.objects.create(trading_account=acct, execution_node=self.node)
        with self.assertRaises(svc.EndpointError) as cm:
            svc.allocate_endpoint(ws)
        self.assertEqual(cm.exception.reason, svc.EP_NO_WINDOWS_IDENTITY)

    def test_missing_node_host_fails_closed(self):
        node = TerminalNode.objects.create(hostname="beta-nohost")  # no rdp_host
        acct, ws = _make_tenant("507", "guvfx_u_507", node=node)
        with self.assertRaises(svc.EndpointError) as cm:
            svc.allocate_endpoint(ws)
        self.assertEqual(cm.exception.reason, svc.EP_NODE_NO_HOST)


class PerTenantRoutingTests(TestCase):
    """PHASE 5/9 — the core cross-tenant isolation proof (all with per-tenant transport ON)."""

    def setUp(self):
        self.node = TerminalNode.objects.create(
            hostname="beta-node-1", rdp_host="100.79.101.19",
            order_bridge_base_url="http://100.79.101.19:8789")

    def _ready_tenant(self, login, wu):
        acct, ws = _make_tenant(login, wu, node=self.node)
        svc.allocate_endpoint(ws)
        svc.mark_ready(ws, health_ok=True)
        return acct, ws, HostedExecutionEndpoint.objects.get(workspace=ws)

    def _resolve(self, job, env=BOTH_FLAGS):
        with mock.patch.dict(os.environ, env, clear=False):
            return resolve_order_transport(job, global_base_url=GLOBAL)

    def test_A_job_routes_to_A_endpoint(self):
        acct, ws, ep = self._ready_tenant("601", "guvfx_u_601")
        t = self._resolve(_job(acct, self.node))
        self.assertTrue(t.ok and t.hosted)
        self.assertEqual(t.reason_code, OT_NODE_OK)
        self.assertEqual(t.base_url, ep.base_url)

    def test_A_cannot_reach_B_endpoint(self):
        acctA, wsA, epA = self._ready_tenant("602", "guvfx_u_602")
        acctB, wsB, epB = self._ready_tenant("603", "guvfx_u_603")
        self.assertNotEqual(epA.base_url, epB.base_url)          # distinct ports
        tA = self._resolve(_job(acctA, self.node))
        tB = self._resolve(_job(acctB, self.node))
        self.assertEqual(tA.base_url, epA.base_url)              # A -> A only
        self.assertEqual(tB.base_url, epB.base_url)              # B -> B only
        self.assertNotEqual(tA.base_url, tB.base_url)            # never crossed

    def test_endpoint_not_ready_fails_closed(self):
        acct, ws = _make_tenant("604", "guvfx_u_604", node=self.node)
        svc.allocate_endpoint(ws)                                # ALLOCATED, not READY
        t = self._resolve(_job(acct, self.node))
        self.assertFalse(t.ok)
        self.assertEqual(t.reason_code, OT_ENDPOINT_NOT_READY)
        self.assertEqual(t.base_url, "")
        self.assertNotEqual(t.base_url, GLOBAL)

    def test_no_endpoint_fails_closed_never_global(self):
        acct, ws = _make_tenant("605", "guvfx_u_605", node=self.node)  # no endpoint at all
        t = self._resolve(_job(acct, self.node))
        self.assertFalse(t.ok)
        self.assertEqual(t.reason_code, OT_ENDPOINT_UNCONFIGURED)
        self.assertNotEqual(t.base_url, GLOBAL)
        self.assertNotEqual(t.base_url, self.node.order_bridge_base_url)  # never the node URL either

    def test_retired_endpoint_not_routable(self):
        acct, ws, ep = self._ready_tenant("606", "guvfx_u_606")
        svc.retire_endpoint(ws)
        t = self._resolve(_job(acct, self.node))
        self.assertFalse(t.ok)                                   # RETIRED excluded -> UNCONFIGURED
        self.assertEqual(t.reason_code, OT_ENDPOINT_UNCONFIGURED)

    def test_job_node_mismatch_fails_closed(self):
        acct, ws, ep = self._ready_tenant("607", "guvfx_u_607")
        other = TerminalNode.objects.create(hostname="other-node", rdp_host="10.9.9.9")
        job = _job(acct, other)                                  # job snapshot node != endpoint node
        t = self._resolve(job)
        self.assertFalse(t.ok)
        self.assertEqual(t.reason_code, OT_NODE_MISMATCH)

    def test_flag_off_is_byte_identical_per_node(self):
        # DARK per-tenant transport: routing falls back to the node URL EXACTLY as before (support@ path).
        acct, ws, ep = self._ready_tenant("608", "guvfx_u_608")
        t = self._resolve(_job(acct, self.node), env=DARK_TRANSPORT)
        self.assertTrue(t.ok and t.hosted)
        self.assertEqual(t.base_url, self.node.order_bridge_base_url)  # :8789 node URL, NOT the endpoint port

    def test_non_hosted_still_global(self):
        # A non-hosted job is unaffected by per-tenant transport -> global bridge.
        user = get_user_model().objects.create_user(username="leg", email="leg@x.invalid", password="x")
        acct = TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number="700",
                                             is_demo=True)   # NOT persistent_workspace -> non-hosted
        t = self._resolve(_job(acct, self.node))
        self.assertTrue(t.ok)
        self.assertFalse(t.hosted)
        self.assertEqual(t.reason_code, OT_LEGACY_GLOBAL)
        self.assertEqual(t.base_url, GLOBAL)


class BridgeConfigTests(SimpleTestCase):
    class _Ep:
        trading_account_id = 26
        runtime_path = r"C:\GuvFX\accounts\26\terminal\terminal64.exe"
        port = 8801
        expected_login = "1302599"
        expected_server = "GuvfxBeta-Demo"
        windows_username = "guvfx_u_26"
        workspace_uuid = "a9b92fb6-288c-42e7-a069-f5853972e028"
        is_demo = True

    def test_render_has_per_tenant_pin_and_safety_posture(self):
        env = render_bridge_env(self._Ep())
        self.assertIn("set MT5_ACCOUNT_ID=26", env)
        self.assertIn(r"set MT5_TERMINAL_PATH=C:\GuvFX\accounts\26\terminal\terminal64.exe", env)
        self.assertIn("set HTTP_SERVER_PORT=8801", env)
        self.assertIn("set MT5_EXPECTED_WINDOWS_USERNAME=guvfx_u_26", env)
        self.assertIn("set MT5_EXPECTED_WORKSPACE_UUID=a9b92fb6-288c-42e7-a069-f5853972e028", env)
        self.assertIn("set MT5_EXPECTED_LOGIN=1302599", env)
        self.assertIn("set MT5_REQUIRE_IDENTITY_PIN=1", env)
        self.assertIn("set MT5_GUARDED_ATTACH=1", env)
        self.assertNotIn("MT5_ALLOW_LIVE", env)                 # DEMO-only posture preserved
        self.assertTrue(env.isascii())                          # RULE 9: ASCII-only

    def test_control_char_injection_rejected(self):
        import types
        ep = types.SimpleNamespace(
            trading_account_id=26, runtime_path="x", port=8801, expected_login="1",
            expected_server="s", workspace_uuid="u", is_demo=True,
            windows_username="guvfx\r\nset MT5_ALLOW_LIVE=1")   # injection attempt
        with self.assertRaises(ValueError):
            render_bridge_env(ep)


class AdversarialFixTests(TestCase):
    """Regressions for the P0-B1 adversarial-review findings (0 HIGH / 0 MEDIUM bar)."""

    def setUp(self):
        self.node = TerminalNode.objects.create(
            hostname="beta-adv", rdp_host="100.79.101.19",
            order_bridge_base_url="http://100.79.101.19:8789")

    # MEDIUM-1: per-tenant ON, a node-UNBOUND job fails closed (matches the DARK per-node OT_NODE_UNBOUND),
    # never routes to a stale endpoint.
    def test_node_unbound_job_fails_closed_when_flag_on(self):
        from execution.order_transport import OT_NODE_UNBOUND
        acct, ws = _make_tenant("701", "guvfx_u_701", node=self.node)
        svc.allocate_endpoint(ws)
        svc.mark_ready(ws, health_ok=True)
        job = ExecutionJob.objects.create(
            account=acct, terminal_node=None, job_type=ExecutionJob.JobType.CLOSE_TRADE, payload={"ticket": 1})
        with mock.patch.dict(os.environ, BOTH_FLAGS, clear=False):
            t = resolve_order_transport(job, global_base_url=GLOBAL)
        self.assertFalse(t.ok)
        self.assertEqual(t.reason_code, OT_NODE_UNBOUND)
        self.assertNotEqual(t.base_url, GLOBAL)

    # MEDIUM-2: expected_server is derived from the bound BrokerServer.server_name (was always "").
    def test_expected_server_derived_from_broker_server(self):
        from trading.models import BrokerServer
        bs = BrokerServer.objects.create(broker_display_name="IS6", server_name="GuvfxBeta-Demo")
        acct, ws = _make_tenant("702", "guvfx_u_702", node=self.node, broker_server=bs)
        svc.allocate_endpoint(ws)
        ep = HostedExecutionEndpoint.objects.get(workspace=ws)
        self.assertEqual(ep.expected_server, "GuvfxBeta-Demo")
        self.assertEqual(ep.expected_login, "702")

    # MEDIUM-4: a LIVE (is_active) account is never auto-re-homed onto a fresh port.
    def test_live_account_auto_allocate_refused(self):
        acct, ws = _make_tenant("703", "guvfx_u_703", node=self.node, is_active=True)
        with self.assertRaises(svc.EndpointError) as cm:
            svc.allocate_endpoint(ws)                       # no explicit_port
        self.assertEqual(cm.exception.reason, svc.EP_LIVE_ACCOUNT_REQUIRES_EXPLICIT)
        self.assertFalse(HostedExecutionEndpoint.objects.filter(workspace=ws).exists())

    def test_live_account_explicit_seed_ok(self):
        # support@-style: seed the EXISTING :8789 bridge explicitly — allowed, no fresh port minted.
        acct, ws = _make_tenant("1302587", "guvfx_u_25", node=self.node, is_active=True)
        res = svc.allocate_endpoint(ws, explicit_port=8789, explicit_base_url="http://100.79.101.19:8789")
        self.assertEqual(res.port, 8789)
        self.assertEqual(res.base_url, "http://100.79.101.19:8789")

    def test_live_account_allow_rehome_flag_ok(self):
        acct, ws = _make_tenant("704", "guvfx_u_704", node=self.node, is_active=True)
        res = svc.allocate_endpoint(ws, allow_rehome=True)
        self.assertIn(res.port, range(svc.PORT_RANGE_START, svc.PORT_RANGE_END + 1))

    # LOW: an explicit_port already held by ANOTHER live endpoint is refused (never seed onto another bridge).
    def test_explicit_port_collision_refused(self):
        a1, w1 = _make_tenant("705", "guvfx_u_705", node=self.node)
        svc.allocate_endpoint(w1)                            # gets 8800
        p1 = HostedExecutionEndpoint.objects.get(workspace=w1).port
        a2, w2 = _make_tenant("706", "guvfx_u_706", node=self.node)
        with self.assertRaises(svc.EndpointError) as cm:
            svc.allocate_endpoint(w2, explicit_port=p1, explicit_base_url=f"http://100.79.101.19:{p1}")
        self.assertEqual(cm.exception.reason, svc.EP_PORT_IN_USE)

    # MEDIUM-3: a lost port race (IntegrityError) is retried onto the next free port, not surfaced raw.
    def test_alloc_race_retries_to_next_free_port(self):
        a1, w1 = _make_tenant("707", "guvfx_u_707", node=self.node)
        svc.allocate_endpoint(w1)                            # occupies 8800 for real
        a2, w2 = _make_tenant("708", "guvfx_u_708", node=self.node)
        # Force allocate_port to first hand back the already-taken 8800 (→ IntegrityError → retry), then let
        # the real allocator run and pick the next free port.
        real = svc.allocate_port
        calls = {"n": 0}
        def flaky(host, **kw):
            calls["n"] += 1
            return svc.PORT_RANGE_START if calls["n"] == 1 else real(host, **kw)
        with mock.patch.object(svc, "allocate_port", side_effect=flaky):
            res = svc.allocate_endpoint(w2)
        self.assertTrue(res.ok)
        self.assertNotEqual(res.port, svc.PORT_RANGE_START)  # retried off the colliding 8800
        self.assertGreaterEqual(calls["n"], 2)               # proved a retry happened
