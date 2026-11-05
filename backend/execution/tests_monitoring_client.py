"""ADR-0075 Phase 5/9/10 — the backend monitoring transport client (DB + injected HTTP).

Proves: gateway-mode request shape (one gateway port, nonce+timestamp+X-GuvFX-Agent-Token, account id +
workspace_uuid + expected_generation in the body — never a per-account port in the URL); legacy transition
mode; the deterministic positions-reason-code classification for every Phase-1 cause; and that the agent
token is attached to the header but never appears in the returned result."""
from __future__ import annotations

import json
import os
from unittest import mock

from django.test import TestCase, override_settings

from execution import monitoring_allocation as alloc
from execution import monitoring_models as mm
from execution.monitoring_gateway_client import probe_route
from execution.monitoring_models import MonitoringRoute, PhysicalHostGateway
from execution.monitoring_testkit import gateway_for, make_endpoint, make_node, make_tenant

TOKEN = "unit-agent-token-SECRET"
_TOKEN_ENV = {"WINDOWS_AGENT_TOKEN": TOKEN, "GUVFX_WINDOWS_AGENT_TOKEN": "", "GUVFX_AGENT_TOKEN": ""}


class _FakeTransport:
    """Records calls (with parsed body) and returns canned responses keyed by op (gateway) or path tail
    (legacy). Never touches the network."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, url, *, headers, body, timeout):
        body_json = None
        if body:
            try:
                body_json = json.loads(body.decode("utf-8"))
            except Exception:
                body_json = None
        key = (body_json or {}).get("op") or url.rsplit("/", 1)[-1]
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body_json": body_json, "key": key})
        return self.responses.get(key, (None, {}))


@override_settings(MONITORING_GATEWAY_ENABLED="1")
@mock.patch.dict(os.environ, _TOKEN_ENV)
class LegacyModeTests(TestCase):
    def setUp(self):
        self.node = make_node()
        self.gw = gateway_for(self.node)

    def _route(self, login="3001", port=8800):
        acct, ws = make_tenant(login, node=self.node)
        ep = make_endpoint(acct, ws, self.node, port=port)
        res = alloc.reserve_route(acct, gateway=self.gw)
        route = MonitoringRoute.objects.get(pk=res.route_id)
        alloc.register_route(route, execution_endpoint=ep, local_port=ep.port)
        return MonitoringRoute.objects.select_related("gateway", "execution_endpoint",
                                                      "trading_account").get(pk=route.pk)

    def test_legacy_ok_flat(self):
        route = self._route()
        t = _FakeTransport({
            "account": (200, {"ok": True, "login": "3001", "server": "IS6Technologies-Live", "trade_mode": 2}),
            "positions": (200, {"ok": True, "positions": [], "count": 0, "trade_mode": 2}),
        })
        r = probe_route(route, transport=t)
        self.assertEqual(r.mode, "legacy")
        self.assertTrue(r.reachable)
        self.assertTrue(r.identity_ok)
        self.assertEqual(r.reason_code, mm.RC_NO_POSITION)
        self.assertEqual(t.calls[0]["headers"].get("X-GuvFX-Agent-Token"), TOKEN)
        self.assertNotIn(TOKEN, repr(r))

    def test_legacy_account_not_demo_feature_disabled(self):
        route = self._route(login="3053", port=8810)
        t = _FakeTransport({
            "account": (200, {"ok": True, "login": "3053", "server": "IS6Technologies-Live"}),
            "positions": (400, {"ok": False, "error": "account_not_demo"}),
        })
        r = probe_route(route, transport=t)
        self.assertFalse(r.positions_ok)
        self.assertEqual(r.reason_code, mm.RC_FEATURE_DISABLED)

    def test_legacy_mt5_init_failed(self):
        route = self._route(login="3050", port=8807)
        t = _FakeTransport({
            "account": (400, {"ok": False, "error": "mt5_init_failed"}),
            "positions": (400, {"ok": False, "error": "mt5_init_failed"}),
        })
        r = probe_route(route, transport=t)
        self.assertFalse(r.identity_ok)
        self.assertEqual(r.reason_code, mm.RC_TERMINAL_INIT_FAILED)

    def test_legacy_unreachable(self):
        route = self._route(login="3003", port=8802)
        t = _FakeTransport({"account": (None, {}), "positions": (None, {})})
        r = probe_route(route, transport=t)
        self.assertFalse(r.reachable)
        self.assertEqual(r.reason_code, mm.RC_UNREACHABLE)

    def test_legacy_no_endpoint(self):
        acct, ws = make_tenant("3004", node=self.node)
        res = alloc.reserve_route(acct, gateway=self.gw)  # no endpoint registered
        route = MonitoringRoute.objects.select_related("gateway", "execution_endpoint").get(pk=res.route_id)
        r = probe_route(route, transport=_FakeTransport({}))
        self.assertEqual(r.reason_code, mm.RC_NO_ENDPOINT)

    def test_classify_positions_non_dict_and_data_null_fail_closed(self):
        # A stray list payload and an explicit data:null must NOT crash — fail closed, positions not ok.
        for idx, pos_resp in enumerate([(200, ["unexpected", "list"]),
                                        (200, {"ok": False, "data": None, "error": "weird"})]):
            route = self._route(login=f"3910{idx}", port=8820 + idx)
            t = _FakeTransport({
                "account": (200, {"ok": True, "login": route.trading_account.account_number,
                                  "server": "IS6Technologies-Live"}),
                "positions": pos_resp,
            })
            r = probe_route(route, transport=t)
            self.assertTrue(r.reachable)
            self.assertFalse(r.positions_ok)   # no exception; classified fail-closed


@override_settings(MONITORING_GATEWAY_ENABLED="1", MONITORING_GATEWAY_ROUTING_ENABLED="1")
@mock.patch.dict(os.environ, _TOKEN_ENV)
class GatewayModeTests(TestCase):
    def setUp(self):
        self.node = make_node()
        self.gw = gateway_for(self.node)
        self.gw.status = PhysicalHostGateway.Status.HEALTHY
        self.gw.last_health_ok = True
        self.gw.gateway_base_url = "http://100.79.101.19:8790"
        self.gw.save()

    def _route(self, login="4001", port=8800):
        acct, ws = make_tenant(login, node=self.node)
        ep = make_endpoint(acct, ws, self.node, port=port)
        res = alloc.reserve_route(acct, gateway=self.gw)
        route = MonitoringRoute.objects.get(pk=res.route_id)
        alloc.register_route(route, execution_endpoint=ep, local_port=ep.port)
        return MonitoringRoute.objects.select_related("gateway", "execution_endpoint",
                                                      "trading_account").get(pk=route.pk)

    def test_gateway_request_shape_single_port_no_per_account_port(self):
        route = self._route()
        t = _FakeTransport({
            "attest_identity": (200, {"ok": True, "verified_login_masked": "••4001",
                                      "verified_server": "IS6Technologies-Live", "is_demo": False,
                                      "capabilities": ["positions"]}),
            "positions": (200, {"ok": True, "op": "positions", "account_id": route.trading_account_id,
                                "data": {"ok": True, "positions": [{"ticket": 1}], "trade_mode": 2}}),
        })
        r = probe_route(route, transport=t)
        self.assertEqual(r.mode, "gateway")
        self.assertTrue(r.reachable)
        self.assertEqual(r.reason_code, mm.RC_OK)
        self.assertEqual(r.n_positions, 1)
        for c in t.calls:
            self.assertEqual(c["url"], "http://100.79.101.19:8790")  # ONE gateway port, no per-account port
            self.assertEqual(c["method"], "POST")
            self.assertEqual(c["body_json"]["account_id"], route.trading_account_id)
            self.assertEqual(c["body_json"]["workspace_uuid"], str(route.workspace_uuid))
            self.assertEqual(c["body_json"]["expected_generation"], route.allocation_generation)
            self.assertIn("nonce", c["body_json"])
            self.assertIn("ts", c["body_json"])
            self.assertEqual(c["headers"].get("X-GuvFX-Agent-Token"), TOKEN)
        self.assertNotIn(TOKEN, repr(r))

    def test_gateway_capability_refusal_maps_feature_disabled(self):
        route = self._route(login="4002", port=8801)
        t = _FakeTransport({
            "attest_identity": (200, {"ok": True, "verified_login_masked": "••4002",
                                      "verified_server": "IS6Technologies-Live", "capabilities": []}),
            "positions": (400, {"ok": False, "error": "capability_not_advertised"}),
        })
        r = probe_route(route, transport=t)
        self.assertEqual(r.reason_code, mm.RC_FEATURE_DISABLED)

    def test_gateway_unreachable(self):
        route = self._route(login="4003", port=8802)
        t = _FakeTransport({"attest_identity": (None, {}), "positions": (None, {})})
        r = probe_route(route, transport=t)
        self.assertFalse(r.reachable)
        self.assertEqual(r.reason_code, mm.RC_UNREACHABLE)

    def test_gateway_non_dict_identity_body_fail_closed(self):
        route = self._route(login="4004", port=8803)
        t = _FakeTransport({"attest_identity": (200, "not-a-dict")})  # hostile/malformed body
        r = probe_route(route, transport=t)
        self.assertTrue(r.reachable)
        self.assertFalse(r.identity_ok)                 # no crash
        self.assertEqual(r.reason_code, mm.RC_GATEWAY_UNHEALTHY)

    def test_gateway_generation_mismatch_is_identity_mismatch(self):
        route = self._route(login="4005", port=8804)
        t = _FakeTransport({"attest_identity": (409, {"ok": False, "error": "generation_mismatch"})})
        r = probe_route(route, transport=t)
        self.assertEqual(r.reason_code, mm.RC_IDENTITY_MISMATCH)
