"""Phase 2 (Control 2 pt.2) — close-path robustness in the bridge close_position.

A single order_send left a position OPEN on any transient reject. close_position now retries a BOUNDED
number of times with a fresh price on retryable retcodes, re-checks whether the position went flat (a
lost-ACK close may have landed), and returns an explicit residual_exposure marker on persistent failure.
"""
import sys
from unittest import mock

from django.test import SimpleTestCase

from execution.tests_bridge_symbols import _load_bridge


def _res(retcode, *, order=11, deal=22, price=1.0849, volume=0.01, comment="x"):
    r = mock.MagicMock()
    r.retcode = retcode
    r.order = order
    r.deal = deal
    r.price = price
    r.volume = volume
    r.comment = comment
    return r


class CloseRobustnessTests(SimpleTestCase):
    def setUp(self):
        self.bridge = _load_bridge()

    def _install(self, m):
        sys.modules["MetaTrader5"] = m
        self.addCleanup(lambda: sys.modules.pop("MetaTrader5", None))

    def _base(self):
        m = mock.MagicMock(name="MetaTrader5")
        m.initialize.return_value = True
        acct = mock.MagicMock()
        acct.trade_mode = 0
        m.account_info.return_value = acct
        tick = mock.MagicMock()
        tick.bid = 1.0849
        tick.ask = 1.0851
        m.symbol_info_tick.return_value = tick
        si = mock.MagicMock()
        si.filling_mode = 2
        m.symbol_info.return_value = si
        return m

    def _pos(self, m):
        p = mock.MagicMock()
        p.type = m.POSITION_TYPE_BUY
        p.volume = 0.01
        p.symbol = "EURUSD"
        p.magic = 7
        return p

    def test_done_first_attempt(self):
        m = self._base()
        m.positions_get.return_value = [self._pos(m)]
        m.order_send.side_effect = [_res(m.TRADE_RETCODE_DONE)]
        self._install(m)
        r = self.bridge.close_position(999)
        self.assertTrue(r["ok"])
        self.assertEqual(r["close_order"], 11)
        self.assertEqual(m.order_send.call_count, 1)

    def test_requote_then_done(self):
        m = self._base()
        m.positions_get.return_value = [self._pos(m)]
        m.order_send.side_effect = [_res(10004), _res(m.TRADE_RETCODE_DONE)]  # requote, then DONE
        self._install(m)
        r = self.bridge.close_position(999)
        self.assertTrue(r["ok"])
        self.assertEqual(m.order_send.call_count, 2)  # retried with a fresh price
        self.assertEqual(m.symbol_info_tick.call_count, 2)  # a FRESH price is read each attempt

    def test_persistent_requote_reports_residual_exposure(self):
        m = self._base()
        m.positions_get.return_value = [self._pos(m)]
        m.order_send.side_effect = [_res(10004), _res(10004), _res(10004)]  # all 3 attempts requote
        self._install(m)
        r = self.bridge.close_position(999)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "close_failed_position_open")
        self.assertTrue(r["residual_exposure"])
        self.assertEqual(m.order_send.call_count, 3)

    def test_hard_rejection_stops_immediately(self):
        m = self._base()
        m.positions_get.return_value = [self._pos(m)]
        m.order_send.side_effect = [_res(10019)]  # NO_MONEY, non-retryable
        self._install(m)
        r = self.bridge.close_position(999)
        self.assertFalse(r["ok"])
        self.assertTrue(r["residual_exposure"])
        self.assertEqual(m.order_send.call_count, 1)  # did NOT retry a hard rejection

    def test_lost_ack_then_flat_is_success(self):
        m = self._base()
        m.positions_get.side_effect = [[self._pos(m)], []]  # present, then gone after the lost-ACK send
        m.order_send.side_effect = [None]
        self._install(m)
        r = self.bridge.close_position(999)
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("closed"))

    def test_positions_get_none_on_recheck_is_never_reported_closed(self):
        # H1 regression: positions_get() -> None is a QUERY ERROR, not proof the position is flat.
        # After a lost-ACK send, a transient None must NOT be misread as closed (that strands exposure).
        m = self._base()
        m.positions_get.side_effect = [[self._pos(m)], None, None]  # present, then query FAILS
        m.order_send.side_effect = [None]  # lost ACK on attempt 0; attempts 1-2 short-circuit on None
        self._install(m)
        r = self.bridge.close_position(999)
        self.assertFalse(r["ok"])              # NOT falsely reported closed
        self.assertTrue(r["residual_exposure"])

    def test_position_not_found(self):
        m = self._base()
        m.positions_get.return_value = []
        self._install(m)
        r = self.bridge.close_position(999)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "position_not_found")
