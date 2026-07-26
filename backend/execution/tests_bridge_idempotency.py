"""Phase 2 (Control 4) — open-position idempotency guard.

A re-delivered / retried PLACE_ORDER (worker retry, job redelivery, agent/bridge restart, backend
timeout) must NOT open a duplicate position. The order comment ("WAY{plan}L{leg}") is the intent's
idempotency key; before order_send the bridge checks whether an OPEN position already carries it and,
if so, returns that ticket instead of sending again.

Fail-safe contract: any read error / non-iterable / None result => None ("no known existing execution
=> proceed"), so normal orders are unaffected. That negative path (order proceeds when nothing matches)
is already exercised by the passing tests_bridge_symbols suite; here we pin the positive guard.
"""
import importlib.util
import os
import sys
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

BRIDGE_PATH = settings.BASE_DIR.parent / "scripts" / "mt5_signal_bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("mt5_signal_bridge_idem", str(BRIDGE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Pos:
    def __init__(self, comment, ticket, volume=0.01):
        self.comment = comment
        self.ticket = ticket
        self.volume = volume


class FindExistingExecutionTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mod = _load_bridge()

    def _mt5(self, positions=None, raises=False):
        m = mock.MagicMock()
        if raises:
            m.positions_get.side_effect = RuntimeError("ipc down")
        else:
            m.positions_get.return_value = positions
        return m

    def test_matching_comment_returns_ticket(self):
        r = self.mod.find_existing_execution(self._mt5([_Pos("WAY1L1", 555)]), "WAY1L1")
        self.assertEqual(r["ticket"], 555)
        self.assertEqual(r["volume"], 0.01)

    def test_non_matching_returns_none(self):
        self.assertIsNone(self.mod.find_existing_execution(self._mt5([_Pos("OTHER", 1)]), "WAY1L1"))

    def test_empty_positions_returns_none(self):
        self.assertIsNone(self.mod.find_existing_execution(self._mt5([]), "WAY1L1"))

    def test_none_positions_returns_none(self):
        self.assertIsNone(self.mod.find_existing_execution(self._mt5(None), "WAY1L1"))

    def test_blank_comment_returns_none(self):
        # a blank intent comment is never an idempotency key
        self.assertIsNone(self.mod.find_existing_execution(self._mt5([_Pos("", 1)]), ""))

    def test_read_error_returns_none(self):
        self.assertIsNone(self.mod.find_existing_execution(self._mt5(raises=True), "WAY1L1"))

    def test_non_iterable_positions_returns_none(self):
        # positions_get() -> bare MagicMock (not iterable) must fail SAFE to None, not raise
        self.assertIsNone(self.mod.find_existing_execution(mock.MagicMock(), "WAY1L1"))

    def test_comment_truncated_to_31_matches(self):
        long_comment = "WAY" + "X" * 40  # > 31 chars
        r = self.mod.find_existing_execution(self._mt5([_Pos(long_comment[:31], 777)]), long_comment)
        self.assertEqual(r["ticket"], 777)

    def test_first_match_of_many(self):
        m = self._mt5([_Pos("A", 1), _Pos("WAY1L1", 2), _Pos("WAY1L1", 3)])
        self.assertEqual(self.mod.find_existing_execution(m, "WAY1L1")["ticket"], 2)


class ExecuteDemoOrderIdempotencyTests(SimpleTestCase):
    """Integration: a matching open position makes /mt5/order a no-op (NO order_send)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mod = _load_bridge()

    def _fake_mt5(self, positions):
        m = mock.MagicMock(name="MetaTrader5")
        m.initialize.return_value = True
        acct = mock.MagicMock()
        acct.trade_mode = 0
        m.account_info.return_value = acct
        term = mock.MagicMock()
        term.connected = True
        term.trade_allowed = True
        m.terminal_info.return_value = term
        m.positions_get.return_value = positions
        return m

    def test_matching_position_is_idempotent_noop(self):
        m = self._fake_mt5([_Pos("WAY1L1", 999)])
        sys.modules["MetaTrader5"] = m
        try:
            with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "", "MT5_EXPECTED_LOGIN": "", "MT5_EXPECTED_SERVER": ""}):
                res = self.mod.execute_demo_order(
                    {"symbol": "EURUSD", "side": "BUY", "lots": 0.01, "comment": "WAY1L1"})
        finally:
            sys.modules.pop("MetaTrader5", None)
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("idempotent"))
        self.assertEqual(res["order"], 999)
        m.order_send.assert_not_called()


class ExecuteMt5TradeIdempotencyTests(SimpleTestCase):
    """Integration for the POLLER path: a matching open position makes execute_mt5_trade a no-op."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mod = _load_bridge()

    def _fake_mt5(self, positions):
        m = mock.MagicMock(name="MetaTrader5")
        m.initialize.return_value = True
        acct = mock.MagicMock()
        acct.trade_mode = 0
        m.account_info.return_value = acct
        term = mock.MagicMock()
        term.connected = True
        term.trade_allowed = True
        m.terminal_info.return_value = term
        m.positions_get.return_value = positions
        return m

    def test_poller_matching_position_is_idempotent_noop(self):
        m = self._fake_mt5([_Pos("WAY1L1", 888)])
        sys.modules["MetaTrader5"] = m
        try:
            with mock.patch.dict(os.environ, {"MT5_ALLOW_LIVE": "", "MT5_EXPECTED_LOGIN": "", "MT5_EXPECTED_SERVER": ""}):
                job = {"id": 42, "payload": {"symbol": "EURUSD", "side": "BUY", "lots": 0.01,
                       "sl_price": 1.08, "tp_price": 1.09, "is_demo": True, "comment": "WAY1L1"}}
                ok, result, err = self.mod.execute_mt5_trade(job)
        finally:
            sys.modules.pop("MetaTrader5", None)
        self.assertTrue(ok)
        self.assertTrue(result.get("idempotent"))
        self.assertEqual(result["ticket"], 888)
        m.order_send.assert_not_called()
