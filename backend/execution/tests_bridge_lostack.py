"""Phase 2 (Control 2) — lost-ACK guaranteed exposure recovery.

When order_send returns None the broker MAY have filled (a lost acknowledgement). Reporting failure
blindly would strand exposure the platform believes does not exist. The bridge now reconciles: if a
position with the intent's comment is already visible after a None send, it is treated as FILLED
(the backend orphaned-PLACE_ORDER reconciler backstops settlement-lag misses).

Fakes model the timeline: the pre-send idempotency check sees NO position; the post-None-send
reconcile sees the filled position.
"""
import sys
from unittest import mock

from django.test import SimpleTestCase

from execution.tests_bridge_symbols import _fake_mt5, _load_bridge, _order
from execution.tests_bridge_idempotency import _Pos


def _positions_none_then(pos):
    """positions_get: empty on the first call (pre-send idempotency), pos on every call after."""
    state = {"n": 0}

    def _pg(*a, **k):
        state["n"] += 1
        return [] if state["n"] == 1 else [pos]

    return _pg


class HttpLostAckTests(SimpleTestCase):
    def setUp(self):
        self.bridge = _load_bridge()

    def _install(self, m):
        sys.modules["MetaTrader5"] = m
        self.addCleanup(lambda: sys.modules.pop("MetaTrader5", None))

    def test_lost_ack_recovers_as_filled(self):
        m = _fake_mt5(check_retcode=0)
        m.order_send.return_value = None
        m.positions_get.side_effect = _positions_none_then(_Pos("WAY1L1", 4242))
        self._install(m)
        res = self.bridge.execute_demo_order(_order())  # _order() comment default "WAY1L1"
        self.assertTrue(res["ok"])
        self.assertTrue(res["lost_ack_recovered"])
        self.assertEqual(res["order"], 4242)

    def test_no_fill_reports_failure(self):
        m = _fake_mt5(check_retcode=0)
        m.order_send.return_value = None
        m.positions_get.return_value = []  # nothing ever appears
        self._install(m)
        res = self.bridge.execute_demo_order(_order())
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "order_send_none")


class PollerLostAckTests(SimpleTestCase):
    def setUp(self):
        self.bridge = _load_bridge()

    def _install(self, m):
        sys.modules["MetaTrader5"] = m
        self.addCleanup(lambda: sys.modules.pop("MetaTrader5", None))

    def _job(self):
        return {"id": 7, "payload": {"symbol": "EURUSD", "side": "BUY", "lots": 0.01,
                "sl_price": 1.08, "tp_price": 1.09, "is_demo": True, "comment": "WAY1L1"}}

    def test_lost_ack_recovers_as_filled(self):
        m = _fake_mt5(check_retcode=0)
        m.order_send.return_value = None
        m.positions_get.side_effect = _positions_none_then(_Pos("WAY1L1", 5151))
        self._install(m)
        ok, result, err = self.bridge.execute_mt5_trade(self._job())
        self.assertTrue(ok)
        self.assertTrue(result["lost_ack_recovered"])
        self.assertEqual(result["ticket"], 5151)

    def test_no_fill_reports_failure(self):
        m = _fake_mt5(check_retcode=0)
        m.order_send.return_value = None
        m.positions_get.return_value = []
        self._install(m)
        ok, result, err = self.bridge.execute_mt5_trade(self._job())
        self.assertFalse(ok)
        self.assertIn("None", err)
