"""ADR-0075 Phase 9/10 — host_gateway component security + behaviour proofs (pure, no DB).

Proves the authenticated monitoring gateway's security properties in-process (no sockets, no host): auth
fail-closed/total, unset-credential denies-all + refuses startup, allow-listed read-only ops only,
route-by-registered-identity (no arbitrary forwarding / no path traversal), cross-tenant isolation,
generation/stale/tombstone rejection, replay + timestamp-skew protection, capability gating, login masking
in attestation, and sanitised errors.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from host_gateway import BridgeRegistry, MonitoringGateway, RegisteredBridge
from host_gateway.gateway_core import GatewayError


class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _gateway(token="secret-token", *, caps=("positions", "account_snapshot"), state="SERVING",
             generation=1, login="123456789"):
    reg = BridgeRegistry()
    reg.register(RegisteredBridge(
        account_id=42, workspace_uuid="ws-42", local_port=9001, generation=generation,
        verified_login=login, verified_server="IS6Technologies-Live", verified_is_demo=False,
        capabilities=tuple(caps), state=state))
    calls = []

    def bridge_caller(port, op):
        calls.append((port, op))
        return {"ok": True, "positions": [], "count": 0, "trade_mode": 2}

    clock = _Clock()
    gw = MonitoringGateway(gateway_id="gw-1", host_key="100.79.101.19", expected_token=token,
                           registry=reg, bridge_caller=bridge_caller, clock=clock)
    return gw, reg, calls, clock


_NONCE = [0]


def _req(op="positions", *, account_id=42, workspace_uuid="ws-42", token="secret-token",
         nonce=None, ts=1_000_000.0, **extra):
    # Unique nonce by default so a test making several valid calls is not tripped by replay protection;
    # pass an explicit nonce to exercise replay.
    if nonce is None:
        _NONCE[0] += 1
        nonce = f"auto-{_NONCE[0]}"
    r = {"op": op, "account_id": account_id, "workspace_uuid": workspace_uuid,
         "token": token, "nonce": nonce, "ts": ts}
    r.update(extra)
    return r


class GatewayAuthTests(SimpleTestCase):
    def test_valid_positions_routes_to_registry_port(self):
        gw, _reg, calls, _c = _gateway()
        out = gw.handle(_req())
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["count"], 0)
        self.assertEqual(calls, [(9001, "positions")])  # dialed the REGISTRY port, that op only

    def test_wrong_token_unauthenticated_and_no_bridge_call(self):
        gw, _reg, calls, _c = _gateway()
        out = gw.handle(_req(token="wrong"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "unauthenticated")
        self.assertEqual(out["_status"], 401)
        self.assertEqual(calls, [])

    def test_missing_token_denied(self):
        gw, _reg, _calls, _c = _gateway()
        r = _req()
        r.pop("token")
        self.assertEqual(gw.handle(r)["error"], "unauthenticated")

    def test_unset_credential_denies_all_and_refuses_startup(self):
        gw, _reg, calls, _c = _gateway(token="")
        # Every request denied (fail-closed) even with a 'correct' empty token.
        self.assertEqual(gw.handle(_req(token=""))["error"], "unauthenticated")
        self.assertEqual(calls, [])
        with self.assertRaises(GatewayError) as ctx:
            gw.assert_startup()
        self.assertEqual(ctx.exception.code, "gateway_no_credential")

    def test_malformed_token_type_is_denied_not_crash(self):
        gw, _reg, _calls, _c = _gateway()
        self.assertEqual(gw.handle(_req(token=12345))["error"], "unauthenticated")  # non-str tolerated


class GatewayOpAllowlistTests(SimpleTestCase):
    def test_write_ops_are_not_in_vocabulary(self):
        gw, _reg, calls, _c = _gateway()
        for op in ("order", "close", "modify", "place_order", "delete", "set_sl"):
            out = gw.handle(_req(op=op))
            self.assertEqual(out["error"], "op_not_allowed", op)
            self.assertEqual(out["_status"], 403)
        self.assertEqual(calls, [])

    def test_path_traversal_style_op_rejected(self):
        gw, _reg, calls, _c = _gateway()
        for op in ("../etc/passwd", "positions/../order", "..\\..\\x", "health/../positions"):
            self.assertEqual(gw.handle(_req(op=op))["error"], "op_not_allowed")
        self.assertEqual(calls, [])

    def test_health_is_gateway_level_no_route_needed(self):
        gw, _reg, calls, _c = _gateway()
        out = gw.handle(_req(op="health", account_id=999999, workspace_uuid="nope"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["routes"], 1)
        self.assertEqual(calls, [])


class GatewayRoutingIsolationTests(SimpleTestCase):
    def test_unknown_account_404(self):
        gw, _reg, calls, _c = _gateway()
        self.assertEqual(gw.handle(_req(account_id=999))["error"], "unknown_route")
        self.assertEqual(calls, [])

    def test_cross_tenant_workspace_mismatch_refused(self):
        gw, _reg, calls, _c = _gateway()
        # Correct account id but another tenant's workspace binding → refused, bridge never dialed.
        out = gw.handle(_req(workspace_uuid="ws-OTHER"))
        self.assertEqual(out["error"], "workspace_mismatch")
        self.assertEqual(out["_status"], 409)
        self.assertEqual(calls, [])

    def test_generation_mismatch_refused(self):
        gw, _reg, calls, _c = _gateway(generation=3)
        self.assertEqual(gw.handle(_req(expected_generation=2))["error"], "generation_mismatch")
        self.assertEqual(gw.handle(_req(expected_generation=3))["ok"], True)

    def test_non_serving_states_refused(self):
        for st in ("DRAINING", "STALE", "TOMBSTONED"):
            gw, _reg, calls, _c = _gateway(state=st)
            self.assertEqual(gw.handle(_req(nonce=f"n-{st}"))["error"], "route_not_serving", st)
            self.assertEqual(calls, [])

    def test_request_cannot_supply_destination_host_or_port(self):
        # Even if the caller injects host/port/path, the gateway ignores them and dials the registry port.
        gw, _reg, calls, _c = _gateway()
        out = gw.handle(_req(host="evil.example", port=22, path="/etc/passwd", local_port=2222))
        self.assertTrue(out["ok"])
        self.assertEqual(calls, [(9001, "positions")])  # registry port only — no arbitrary forwarding


class GatewayReplayAndFreshnessTests(SimpleTestCase):
    def test_replayed_nonce_rejected(self):
        gw, _reg, _calls, _c = _gateway()
        self.assertTrue(gw.handle(_req(nonce="dup"))["ok"])
        out = gw.handle(_req(nonce="dup"))
        self.assertEqual(out["error"], "replay_detected")

    def test_stale_timestamp_rejected(self):
        gw, _reg, _calls, _c = _gateway()
        self.assertEqual(gw.handle(_req(ts=1.0))["error"], "stale_timestamp")       # far in the past
        self.assertEqual(gw.handle(_req(ts=9_999_999_999.0))["error"], "stale_timestamp")

    def test_missing_nonce_malformed(self):
        gw, _reg, _calls, _c = _gateway()
        r = _req()
        r.pop("nonce")
        self.assertEqual(gw.handle(r)["error"], "malformed_request")

    def test_non_finite_timestamp_rejected(self):
        # NaN must NOT bypass the skew bound (fail-open class); Infinity/-Infinity/bool also rejected; a
        # float()-overflowing huge int is a 4xx stale_timestamp (client fault), NOT a 5xx bridge_error.
        gw, _reg, calls, _c = _gateway()
        for bad_ts in (float("nan"), float("inf"), float("-inf"), True, 10 ** 400):
            out = gw.handle(_req(ts=bad_ts))
            self.assertEqual(out["error"], "stale_timestamp", bad_ts)
            self.assertEqual(out["_status"], 400, bad_ts)
        self.assertEqual(calls, [])  # never routed to a bridge


class NonceCacheTests(SimpleTestCase):
    def test_bounded_rotation_and_replay(self):
        from host_gateway.gateway_core import _NonceCache
        c = _NonceCache(ttl=100.0, max_per_window=1000)
        self.assertTrue(c.check_and_add("a", 1000.0))
        self.assertFalse(c.check_and_add("a", 1000.0))      # exact replay within window
        # After > ttl the window rotates (a is still remembered one extra generation), after 2*ttl it is
        # forgotten and may be reused — but the timestamp-skew bound governs real replayability.
        self.assertFalse(c.check_and_add("a", 1150.0))      # still in prev generation
        self.assertTrue(c.check_and_add("a", 1300.0))       # two rotations later → forgotten, reusable

    def test_flood_cap_forces_rotation(self):
        from host_gateway.gateway_core import _NonceCache
        c = _NonceCache(ttl=1e9, max_per_window=50)         # ttl huge so only the size cap can rotate
        for i in range(120):
            self.assertTrue(c.check_and_add(f"n{i}", 1000.0))
        # Memory is bounded to ~2 windows of `max_per_window`, not 120+ in one unbounded dict.
        self.assertLessEqual(len(c._cur) + len(c._prev), 2 * 50 + 1)


class GatewayCapabilityAndAttestationTests(SimpleTestCase):
    def test_capability_not_advertised_blocks_data_op(self):
        gw, _reg, calls, _c = _gateway(caps=())  # no capabilities
        out = gw.handle(_req(op="positions"))
        self.assertEqual(out["error"], "capability_not_advertised")
        self.assertEqual(calls, [])

    def test_attest_identity_masks_login_and_needs_no_capability(self):
        gw, _reg, calls, _c = _gateway(caps=(), login="123456789")
        out = gw.handle(_req(op="attest_identity"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["verified_login_masked"], "••6789")
        self.assertNotIn("123456789", str(out))     # full login never present
        self.assertEqual(calls, [])                  # attestation makes no bridge call

    def test_bridge_error_sanitised(self):
        reg = BridgeRegistry()
        reg.register(RegisteredBridge(account_id=42, workspace_uuid="ws-42", local_port=9001,
                                      capabilities=("positions",)))

        def boom(port, op):
            raise RuntimeError("internal secret detail")

        gw = MonitoringGateway(gateway_id="g", host_key="h", expected_token="t", registry=reg,
                               bridge_caller=boom, clock=_Clock())
        out = gw.handle(_req(token="t"))
        self.assertEqual(out["error"], "bridge_error")
        self.assertEqual(out["_status"], 502)
        self.assertNotIn("secret", str(out))

    def test_non_dict_request_malformed(self):
        gw, _reg, _calls, _c = _gateway()
        for bad in (None, "x", 5, [1, 2]):
            out = gw.handle(bad)
            self.assertFalse(out["ok"])
            self.assertEqual(out["error"], "malformed_request", bad)
