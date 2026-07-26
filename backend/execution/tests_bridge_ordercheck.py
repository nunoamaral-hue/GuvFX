"""Phase 2 (Control 3) — the HTTP /mt5/order path pre-flights with order_check, like the poller.

Previously execute_demo_order went straight to order_send. Now it runs order_check first and refuses
to send when the check reports a non-executable retcode; a None/errored check does NOT block (the
broker still gives the verdict on send). Reuses the proven MetaTrader5 fake from tests_bridge_symbols.
"""
import sys
from unittest import mock

from django.test import SimpleTestCase

from execution.tests_bridge_symbols import _fake_mt5, _load_bridge, _order


class HttpOrderCheckTests(SimpleTestCase):
    def setUp(self):
        self.bridge = _load_bridge()

    def _install(self, m):
        sys.modules["MetaTrader5"] = m
        self.addCleanup(lambda: sys.modules.pop("MetaTrader5", None))

    def test_nonexecutable_check_blocks_send(self):
        m = _fake_mt5(check_retcode=10016)  # INVALID_STOPS
        self._install(m)
        res = self.bridge.execute_demo_order(_order())
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "order_check_failed")
        self.assertEqual(res["retcode"], 10016)
        m.order_send.assert_not_called()

    def test_ok_check_proceeds_to_send(self):
        m = _fake_mt5(check_retcode=0)
        self._install(m)
        res = self.bridge.execute_demo_order(_order())
        self.assertTrue(res["ok"])
        m.order_send.assert_called_once()

    def test_check_error_fails_safe_and_sends(self):
        # a raising order_check must not block the order (broker gives the real verdict on send)
        m = _fake_mt5(check_retcode=0)
        m.order_check.side_effect = RuntimeError("check ipc down")
        self._install(m)
        res = self.bridge.execute_demo_order(_order())
        self.assertTrue(res["ok"])
        m.order_send.assert_called_once()

    def test_none_check_fails_safe_and_sends(self):
        # order_check returning None must NOT block (the other fail-safe branch)
        m = _fake_mt5(check_retcode=0)
        m.order_check.return_value = None
        self._install(m)
        res = self.bridge.execute_demo_order(_order())
        self.assertTrue(res["ok"])
        m.order_send.assert_called_once()

    def test_done_retcode_treated_as_pass(self):
        # TRADE_RETCODE_DONE is the second accepted "pass" value, not just 0
        m = _fake_mt5(check_retcode=0)
        m.order_check.return_value.retcode = m.TRADE_RETCODE_DONE
        self._install(m)
        res = self.bridge.execute_demo_order(_order())
        self.assertTrue(res["ok"])
        m.order_send.assert_called_once()
