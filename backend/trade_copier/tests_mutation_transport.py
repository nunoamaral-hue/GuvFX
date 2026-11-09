"""ADR-0087 — per-tenant follower MUTATION transport tests.

Pins the transport orchestration: INERT when OFF (no resolve, no network); when armed it ROUTE-PINS to the
governed follower, IDENTITY-BINDS the command to the resolved account, enforces the deadline, POSTs to the
op-specific bridge route with the env token, and maps every result to a structured outcome — UNKNOWN on
timeout/error (never success, never retried). The per-tenant resolver and the token source are audited
elsewhere and are mocked here; no real broker is contacted.
"""
from decimal import Decimal
from unittest import mock

from django.test import override_settings

from execution.snapshot_transport import SnapshotTransport, ST_PER_TENANT_OK, ST_HOSTED_NO_ENDPOINT
from trade_copier import mutation_transport as mt
from trade_copier.models import CopierReplication, ReplicationStatus
from trade_copier.tests_execution_producer import ProducerFixture

ARM = dict(COPIER_ENABLED=True, COPIER_EXECUTE_ENABLED=True, COPIER_LIVE_EXECUTE_ENABLED=True,
           COPIER_MUTATION_DISPATCH_ENABLED=True, COPIER_MUTATION_TRANSPORT_ENABLED=True)
_BASE = "http://10.50.0.9:8812"
_PER_TENANT = SnapshotTransport(True, ST_PER_TENANT_OK, _BASE, per_tenant=True)


def _future():
    import time
    return time.time() + 1800


class MutationTransportTests(ProducerFixture):
    _n = 3000

    def _repl(self, *, op="FULL_CLOSE", ticket=8800, symbol="XAUUSD"):
        f = self._follower(number=str(2000000 + MutationTransportTests._n))
        MutationTransportTests._n += 1
        acct = f.follower_account
        rep = CopierReplication.objects.create(
            master_event=self.event, follower=f, requested_op="CLOSE", follower_symbol=symbol,
            target_volume=Decimal("0.05"), follower_ticket=ticket, status=ReplicationStatus.EXECUTION_REQUESTED)
        command = {"copier_operation": op, "ticket": ticket, "symbol": symbol,
                   "copier_replication_id": rep.id, "require_identity_pin": True,
                   "expected_login": str(acct.account_number),
                   "expected_server": acct.broker_server.server_name,
                   "authorization_expires_at": _future()}
        return rep, command, acct

    def _send(self, command, *, http, now=None):
        import time
        return mt.send_mutation(command, now_epoch=now if now is not None else time.time(), http=http)

    # ── INERT when OFF ──────────────────────────────────────────────────────────────────────────────────
    def test_disabled_is_inert_no_resolve_no_network(self):
        rep, command, _ = self._repl()
        calls = []
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base") as rsv:
            out = self._send(command, http=lambda *a, **k: calls.append(1) or (200, {"ok": True}))
        self.assertEqual(out["error"], "mutation_transport_disabled")
        self.assertEqual(calls, [])
        rsv.assert_not_called()

    # ── armed happy path: route+identity+deadline -> POST to the op route ───────────────────────────────
    @override_settings(**ARM)
    def test_armed_full_close_posts_to_close_route(self):
        rep, command, _ = self._repl(op="FULL_CLOSE")
        seen = {}

        def _http(url, *, body, headers, timeout):
            seen["url"] = url
            seen["has_token"] = bool(headers.get("X-GuvFX-Agent-Token"))
            return 200, {"ok": True, "lifecycle_outcome": "APPLIED", "lifecycle_reason": "closed"}
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base", return_value=_PER_TENANT), \
             mock.patch("execution.monitoring_gateway_client._agent_token", return_value="tok"):
            out = self._send(command, http=_http)
        self.assertEqual(out["lifecycle_outcome"], "APPLIED")
        self.assertEqual(seen["url"], _BASE + "/mt5/close-position")
        self.assertTrue(seen["has_token"])

    @override_settings(**ARM)
    def test_armed_modify_posts_to_modify_route(self):
        rep, command, _ = self._repl(op="MODIFY_PROTECTION")
        seen = {}
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base", return_value=_PER_TENANT), \
             mock.patch("execution.monitoring_gateway_client._agent_token", return_value="tok"):
            out = self._send(command, http=lambda url, **k: (seen.update(url=url) or (200, {"ok": True})))
        self.assertEqual(seen["url"], _BASE + "/mt5/modify-position")

    # ── fail-closed refusals (nothing sent) ─────────────────────────────────────────────────────────────
    @override_settings(**ARM)
    def test_unsupported_op_blocked_no_send(self):
        rep, command, _ = self._repl(op="PARTIAL_CLOSE")
        calls = []
        out = self._send(command, http=lambda *a, **k: calls.append(1))
        self.assertTrue(out["guard_blocked"])
        self.assertEqual(out["error"], "transport_unsupported_operation")
        self.assertEqual(calls, [])

    @override_settings(**ARM)
    def test_replication_missing_blocked(self):
        rep, command, _ = self._repl()
        command["copier_replication_id"] = 99999999
        out = self._send(command, http=lambda *a, **k: (200, {}))
        self.assertEqual(out["error"], "transport_replication_missing")

    @override_settings(**ARM)
    def test_follower_not_enabled_blocked(self):
        rep, command, _ = self._repl()
        rep.follower.enabled = False
        rep.follower.save(update_fields=["enabled"])
        calls = []
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base") as rsv:
            out = self._send(command, http=lambda *a, **k: calls.append(1))
        self.assertEqual(out["error"], "transport_follower_not_enabled")
        self.assertEqual(calls, [])
        rsv.assert_not_called()

    @override_settings(**ARM)
    def test_endpoint_not_per_tenant_blocked_no_send(self):
        rep, command, _ = self._repl()
        calls = []
        bad = SnapshotTransport(False, ST_HOSTED_NO_ENDPOINT)
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base", return_value=bad):
            out = self._send(command, http=lambda *a, **k: calls.append(1))
        self.assertTrue(out["guard_blocked"])
        self.assertIn("endpoint_", out["error"])
        self.assertEqual(calls, [])

    @override_settings(**ARM)
    def test_identity_mismatch_blocked_no_send(self):
        rep, command, _ = self._repl()
        command["expected_login"] = "99999999"       # != the resolved follower account number
        calls = []
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base", return_value=_PER_TENANT):
            out = self._send(command, http=lambda *a, **k: calls.append(1))
        self.assertEqual(out["error"], "transport_identity_mismatch")
        self.assertEqual(calls, [])

    @override_settings(**ARM)
    def test_past_deadline_blocked_no_send(self):
        rep, command, _ = self._repl()
        command["authorization_expires_at"] = 1.0     # far past
        calls = []
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base", return_value=_PER_TENANT):
            out = self._send(command, http=lambda *a, **k: calls.append(1))
        self.assertEqual(out["error"], "transport_past_deadline")
        self.assertEqual(calls, [])

    @override_settings(**ARM)
    def test_no_agent_token_blocked(self):
        rep, command, _ = self._repl()
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base", return_value=_PER_TENANT), \
             mock.patch("execution.monitoring_gateway_client._agent_token", return_value=""):
            out = self._send(command, http=lambda *a, **k: (200, {}))
        self.assertEqual(out["error"], "transport_no_agent_token")

    # ── indeterminate send -> UNKNOWN (never success, never retried) ────────────────────────────────────
    @override_settings(**ARM)
    def test_timeout_is_unknown_no_retry(self):
        rep, command, _ = self._repl()
        n = {"calls": 0}

        def _boom(*a, **k):
            n["calls"] += 1
            raise TimeoutError("lost")
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base", return_value=_PER_TENANT), \
             mock.patch("execution.monitoring_gateway_client._agent_token", return_value="tok"):
            out = self._send(command, http=_boom)
        self.assertEqual(out["lifecycle_outcome"], "UNKNOWN_OUTCOME")
        self.assertTrue(out["need_reconcile"])
        self.assertEqual(n["calls"], 1)             # exactly once, no retry

    @override_settings(**ARM)
    def test_non_200_is_unknown_unless_guard_body(self):
        rep, command, _ = self._repl()
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base", return_value=_PER_TENANT), \
             mock.patch("execution.monitoring_gateway_client._agent_token", return_value="tok"):
            out = self._send(command, http=lambda *a, **k: (503, {"error": "mt5_busy"}))
            self.assertEqual(out["lifecycle_outcome"], "UNKNOWN_OUTCOME")
            # a 400 pre-send bridge refusal carrying guard_blocked is passed through (nothing sent).
            out2 = self._send(command, http=lambda *a, **k: (400, {"guard_blocked": True, "error": "x"}))
            self.assertTrue(out2["guard_blocked"])
