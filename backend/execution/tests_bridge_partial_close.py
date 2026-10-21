"""Bridge-contract tests for ``partial_close_position`` (ADR-0055). DEMO + HEDGING only; explicit ticket +
explicit partial volume; re-read + re-normalize at send time; NEVER a full close / different ticket / symbol;
a SINGLE order_send (an unknown result is not retried); structured reason codes. Mocks MetaTrader5 (no broker).

Discriminating negative controls: removing any one guard makes its test fail (each asserts a distinct reason)."""
import sys
from unittest import mock

from django.test import SimpleTestCase

from execution.tests_bridge_symbols import _load_bridge


class PartialCloseBridgeTests(SimpleTestCase):
    def setUp(self):
        self.bridge = _load_bridge()

    def _install(self, m):
        sys.modules["MetaTrader5"] = m
        self.addCleanup(lambda: sys.modules.pop("MetaTrader5", None))

    def _base(self, *, trade_mode=0, margin_mode=2, pos_volume=1.00, pos_symbol="XAUUSD",
              vmin=0.01, vstep=0.01, filling_mode=2, positions="one", retcode="done", order_send="ok"):
        m = mock.MagicMock(name="MetaTrader5")
        m.initialize.return_value = True
        # concrete int constants so comparisons are deterministic
        m.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
        m.POSITION_TYPE_BUY = 1
        m.ORDER_TYPE_SELL = 0
        m.ORDER_TYPE_BUY = 1
        m.TRADE_ACTION_DEAL = 1
        m.TRADE_RETCODE_DONE = 10009
        m.ORDER_TIME_GTC = 0
        m.ORDER_FILLING_IOC = 1
        m.ORDER_FILLING_FOK = 2
        acct = mock.MagicMock()
        acct.trade_mode = trade_mode
        acct.margin_mode = margin_mode
        acct.login = 123456
        acct.server = "IS6-Demo"
        m.account_info.return_value = acct
        term = mock.MagicMock()
        term.connected = True
        term.trade_allowed = True
        m.terminal_info.return_value = term
        si = mock.MagicMock()
        si.volume_min = vmin
        si.volume_step = vstep
        si.volume_max = 100
        si.filling_mode = filling_mode
        m.symbol_info.return_value = si
        tick = mock.MagicMock()
        tick.bid = 2000.0
        tick.ask = 2000.5
        m.symbol_info_tick.return_value = tick
        pos = mock.MagicMock()
        pos.ticket = 777
        pos.volume = pos_volume
        pos.symbol = pos_symbol
        pos.type = 1
        pos.magic = 0
        if positions == "one":
            m.positions_get.return_value = [pos]
        elif positions == "empty":
            m.positions_get.return_value = []
        else:  # "none"
            m.positions_get.return_value = None
        if order_send == "none":
            m.order_send.return_value = None
        else:
            res = mock.MagicMock()
            res.retcode = m.TRADE_RETCODE_DONE if retcode == "done" else 10019
            res.order, res.deal, res.price, res.volume, res.comment = 11, 22, 2000.0, 0.40, "ok"
            m.order_send.return_value = res
        self._install(m)
        return m

    def _call(self, m, *, ticket=777, volume=0.40, identity=None, expected_symbol="XAUUSD"):
        return self.bridge.partial_close_position(ticket, volume, identity=identity,
                                                  expected_symbol=expected_symbol)

    # --- happy path + "never full / correct ticket" proof ---
    def test_happy_partial_close(self):
        m = self._base()
        out = self._call(m)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["partial"])
        self.assertEqual(out["requested_volume"], 0.40)
        self.assertEqual(out["expected_remaining"], 0.60)
        req = m.order_send.call_args[0][0]
        self.assertEqual(req["volume"], 0.40)          # the PARTIAL volume, NOT pos.volume (1.00)
        self.assertEqual(req["position"], 777)          # exact ticket
        self.assertEqual(req["symbol"], "XAUUSD")

    # --- demo / hedging locks ---
    def test_real_account_rejected(self):
        out = self._call(self._base(trade_mode=2))
        self.assertEqual(out["error"], "account_not_demo")

    def test_netting_rejected(self):
        out = self._call(self._base(margin_mode=0))
        self.assertEqual(out["error"], "account_mode_not_certified")

    def test_exchange_rejected(self):
        out = self._call(self._base(margin_mode=1))
        self.assertEqual(out["error"], "account_mode_not_certified")

    # --- volume guards ---
    def test_zero_volume_rejected(self):
        out = self._call(self._base(), volume=0)
        self.assertEqual(out["error"], "volume_non_positive")

    def test_negative_volume_rejected(self):
        out = self._call(self._base(), volume=-0.1)
        self.assertEqual(out["error"], "volume_non_positive")

    def test_excessive_volume_is_full_close_rejected(self):
        out = self._call(self._base(pos_volume=1.00), volume=1.5)
        self.assertEqual(out["error"], "would_be_full_close")

    def test_equal_full_volume_rejected(self):
        out = self._call(self._base(pos_volume=1.00), volume=1.00)
        self.assertEqual(out["error"], "would_be_full_close")

    def test_rounds_to_zero_rejected(self):
        # request below one step -> floor to 0
        out = self._call(self._base(vstep=0.01, vmin=0.01), volume=0.004)
        self.assertEqual(out["error"], "rounds_to_zero")

    def test_below_min_reduction_rejected(self):
        out = self._call(self._base(vstep=0.01, vmin=0.02), volume=0.01)
        self.assertEqual(out["error"], "below_min_reduction")

    def test_remainder_below_min_rejected(self):
        # step 0.01, vmin 0.03, current 0.05, reduce 0.04 -> remaining 0.01 < vmin
        out = self._call(self._base(pos_volume=0.05, vstep=0.01, vmin=0.03), volume=0.04)
        self.assertEqual(out["error"], "remainder_below_min")

    # --- ticket / symbol / position guards ---
    def test_position_not_found(self):
        out = self._call(self._base(positions="empty"))
        self.assertEqual(out["error"], "position_not_found")

    def test_positions_get_failed(self):
        out = self._call(self._base(positions="none"))
        self.assertEqual(out["error"], "positions_get_failed")

    def test_symbol_changed_rejected(self):
        out = self._call(self._base(pos_symbol="XAUUSD"), expected_symbol="EURUSD")
        self.assertEqual(out["error"], "symbol_changed")

    def test_unsupported_filling_mode_rejected(self):
        out = self._call(self._base(filling_mode=0))
        self.assertEqual(out["error"], "unsupported_filling_mode")

    # --- identity gate ---
    def test_identity_login_mismatch_rejected(self):
        m = self._base()
        out = self._call(m, identity={"require_identity_pin": True, "expected_login": "999",
                                      "expected_server": "IS6-Demo"})
        self.assertEqual(out["error"], "identity_rejected")
        m.order_send.assert_not_called()

    # --- send-time results ---
    def test_order_send_none_unknown_not_retried(self):
        m = self._base(order_send="none")
        out = self._call(m)
        self.assertEqual(out["error"], "order_send_none_unknown")
        self.assertTrue(out["need_reconcile"])
        self.assertEqual(m.order_send.call_count, 1)   # SINGLE send — never blindly retried

    def test_order_send_rejected(self):
        out = self._call(self._base(retcode="rejected"))
        self.assertEqual(out["error"], "partial_close_rejected")
