"""Bridge-contract tests for ``partial_close_position`` (ADR-0055). DEMO + HEDGING only; MANDATORY job-bound
identity pin (login+server, no env fallback) + MANDATORY expected_symbol; explicit ticket + explicit partial
volume; exactly-one-ticket re-read; a SINGLE order_send; EVERY post-send outcome requires reconciliation
(lost ACK / partial fill / volume mismatch / reject); NEVER a full close / different ticket / symbol. Mocks
MetaTrader5 (no broker). Discriminating: removing any one guard makes its test fail (distinct reason codes)."""
import sys
from unittest import mock

from django.test import SimpleTestCase

from execution.tests_bridge_symbols import _load_bridge

_LOGIN = 123456
_SERVER = "IS6-Demo"
_GOOD_ID = {"expected_login": str(_LOGIN), "expected_server": _SERVER}


class PartialCloseBridgeTests(SimpleTestCase):
    def setUp(self):
        self.bridge = _load_bridge()

    def _install(self, m):
        sys.modules["MetaTrader5"] = m
        self.addCleanup(lambda: sys.modules.pop("MetaTrader5", None))

    def _base(self, *, trade_mode=0, margin_mode=2, pos_volume=1.00, pos_symbol="XAUUSD", pos_ticket=777,
              positions="one", vmin=0.01, vstep=0.01, filling_mode=2, retcode="done", filled=0.40,
              login=_LOGIN, server=_SERVER):
        m = mock.MagicMock(name="MetaTrader5")
        m.initialize.return_value = True
        m.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
        m.POSITION_TYPE_BUY = 1
        m.ORDER_TYPE_SELL, m.ORDER_TYPE_BUY = 0, 1
        m.TRADE_ACTION_DEAL = 1
        m.TRADE_RETCODE_DONE = 10009
        m.TRADE_RETCODE_DONE_PARTIAL = 10010
        m.ORDER_TIME_GTC = 0
        m.ORDER_FILLING_IOC, m.ORDER_FILLING_FOK = 1, 2
        acct = mock.MagicMock()
        acct.trade_mode, acct.margin_mode, acct.login, acct.server = trade_mode, margin_mode, login, server
        m.account_info.return_value = acct
        term = mock.MagicMock()
        term.connected, term.trade_allowed = True, True
        m.terminal_info.return_value = term
        si = mock.MagicMock()
        si.volume_min, si.volume_step, si.volume_max, si.filling_mode = vmin, vstep, 100, filling_mode
        m.symbol_info.return_value = si
        tick = mock.MagicMock()
        tick.bid, tick.ask = 2000.0, 2000.5
        m.symbol_info_tick.return_value = tick

        def _pos(ticket):
            p = mock.MagicMock()
            p.ticket, p.volume, p.symbol, p.type, p.magic = ticket, pos_volume, pos_symbol, 1, 0
            return p
        if positions == "one":
            m.positions_get.return_value = [_pos(pos_ticket)]
        elif positions == "empty":
            m.positions_get.return_value = []
        elif positions == "two":
            m.positions_get.return_value = [_pos(pos_ticket), _pos(pos_ticket + 1)]
        elif positions == "wrongticket":
            m.positions_get.return_value = [_pos(pos_ticket + 99)]
        else:  # "none"
            m.positions_get.return_value = None
        if retcode == "none":
            m.order_send.return_value = None
        else:
            res = mock.MagicMock()
            res.retcode = {"done": 10009, "done_partial": 10010, "rejected": 10019}[retcode]
            res.order, res.deal, res.price, res.comment = 11, 22, 2000.0, "x"
            res.volume = filled
            m.order_send.return_value = res
        self._install(m)
        return m

    def _call(self, m, *, ticket=777, volume=0.40, identity="good", expected_symbol="XAUUSD"):
        if identity == "good":
            identity = dict(_GOOD_ID)
        return self.bridge.partial_close_position(ticket, volume, identity=identity,
                                                  expected_symbol=expected_symbol)

    # --- happy path: mutated + reconciliation ALWAYS required + correct request ---
    def test_happy_partial_close(self):
        m = self._base()
        out = self._call(m)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["mutated"])
        self.assertTrue(out["need_reconcile"])            # reconciliation ALWAYS after a send
        self.assertTrue(out["volume_confirmed"])          # full DONE, filled == requested
        self.assertEqual(out["requested_volume"], 0.40)
        self.assertEqual(out["estimated_remaining"], 0.60)  # arithmetic estimate, not confirmed
        req = m.order_send.call_args[0][0]
        self.assertEqual(req["volume"], 0.40)              # PARTIAL volume, not pos.volume (1.00)
        self.assertEqual(req["position"], 777)             # verified ticket
        self.assertEqual(m.order_send.call_count, 1)

    # --- P0: partial-fill / volume-mismatch handling (never a clean failure) ---
    def test_done_partial_requires_reconcile_not_rejected(self):
        m = self._base(retcode="done_partial", filled=0.20)
        out = self._call(m)
        self.assertTrue(out["ok"], out)                   # a real mutation, NOT partial_close_rejected
        self.assertTrue(out["mutated"])
        self.assertTrue(out["need_reconcile"])
        self.assertFalse(out["volume_confirmed"])
        self.assertEqual(out["filled_volume"], 0.20)
        self.assertEqual(m.order_send.call_count, 1)      # no second send

    def test_done_smaller_volume_not_confirmed(self):
        out = self._call(self._base(retcode="done", filled=0.20))  # requested 0.40
        self.assertTrue(out["ok"])
        self.assertTrue(out["need_reconcile"])
        self.assertFalse(out["volume_confirmed"])

    def test_done_missing_volume_not_confirmed(self):
        out = self._call(self._base(retcode="done", filled=None))
        self.assertTrue(out["ok"])
        self.assertTrue(out["need_reconcile"])
        self.assertFalse(out["volume_confirmed"])

    def test_rejected_requires_reconcile(self):
        m = self._base(retcode="rejected")
        out = self._call(m)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "partial_close_rejected")
        self.assertTrue(out["need_reconcile"])            # authoritative read before terminal state
        self.assertEqual(m.order_send.call_count, 1)

    def test_order_send_none_unknown_not_retried(self):
        m = self._base(retcode="none")
        out = self._call(m)
        self.assertEqual(out["error"], "order_send_none_unknown")
        self.assertTrue(out["need_reconcile"])
        self.assertEqual(m.order_send.call_count, 1)

    # --- P0: mandatory job-bound identity pin (no env fallback) ---
    def test_identity_none_rejected_before_send(self):
        m = self._base()
        out = self._call(m, identity=None)
        self.assertEqual(out["error"], "identity_pin_required")
        m.order_send.assert_not_called()

    def test_identity_empty_rejected(self):
        self.assertEqual(self._call(self._base(), identity={})["error"], "identity_pin_required")

    def test_identity_missing_login_rejected(self):
        out = self._call(self._base(), identity={"expected_server": _SERVER})
        self.assertEqual(out["error"], "identity_pin_required")

    def test_identity_missing_server_rejected(self):
        out = self._call(self._base(), identity={"expected_login": str(_LOGIN)})
        self.assertEqual(out["error"], "identity_pin_required")

    def test_identity_wrong_login_rejected(self):
        m = self._base()
        out = self._call(m, identity={"expected_login": "999", "expected_server": _SERVER})
        self.assertEqual(out["error"], "identity_rejected")
        m.order_send.assert_not_called()

    def test_identity_wrong_server_rejected(self):
        m = self._base()
        out = self._call(m, identity={"expected_login": str(_LOGIN), "expected_server": "OtherServer"})
        self.assertEqual(out["error"], "identity_rejected")
        m.order_send.assert_not_called()

    def test_identity_correct_passes(self):
        self.assertTrue(self._call(self._base())["ok"])

    def test_env_pins_alone_cannot_authorize(self):
        # No job-bound identity -> rejected regardless of any process-env pins (no env fallback for pclose).
        import os
        with mock.patch.dict(os.environ, {"MT5_EXPECTED_LOGIN": str(_LOGIN), "MT5_EXPECTED_SERVER": _SERVER}):
            out = self._call(self._base(), identity=None)
        self.assertEqual(out["error"], "identity_pin_required")

    # --- P1: mandatory expected_symbol ---
    def test_expected_symbol_missing_rejected(self):
        self.assertEqual(self._call(self._base(), expected_symbol=None)["error"], "expected_symbol_required")

    def test_expected_symbol_empty_rejected(self):
        self.assertEqual(self._call(self._base(), expected_symbol="  ")["error"], "expected_symbol_required")

    def test_symbol_mismatch_rejected(self):
        out = self._call(self._base(pos_symbol="XAUUSD"), expected_symbol="EURUSD")
        self.assertEqual(out["error"], "symbol_changed")

    # --- P1: returned-ticket verification ---
    def test_wrong_returned_ticket_rejected(self):
        m = self._base(positions="wrongticket")
        out = self._call(m)
        self.assertEqual(out["error"], "ticket_mismatch")
        m.order_send.assert_not_called()

    def test_multiple_positions_rejected(self):
        m = self._base(positions="two")
        out = self._call(m)
        self.assertEqual(out["error"], "multiple_positions_returned")
        m.order_send.assert_not_called()

    # --- demo / hedging / volume / position / filling guards ---
    def test_real_account_rejected(self):
        self.assertEqual(self._call(self._base(trade_mode=2))["error"], "account_not_demo")

    def test_netting_rejected(self):
        self.assertEqual(self._call(self._base(margin_mode=0))["error"], "account_mode_not_certified")

    def test_exchange_rejected(self):
        self.assertEqual(self._call(self._base(margin_mode=1))["error"], "account_mode_not_certified")

    def test_zero_volume_rejected(self):
        self.assertEqual(self._call(self._base(), volume=0)["error"], "volume_non_positive")

    def test_excessive_volume_is_full_close_rejected(self):
        self.assertEqual(self._call(self._base(pos_volume=1.00), volume=1.5)["error"], "would_be_full_close")

    def test_equal_full_volume_rejected(self):
        self.assertEqual(self._call(self._base(pos_volume=1.00), volume=1.00)["error"], "would_be_full_close")

    def test_rounds_to_zero_rejected(self):
        self.assertEqual(self._call(self._base(vstep=0.01, vmin=0.01), volume=0.004)["error"], "rounds_to_zero")

    def test_below_min_reduction_rejected(self):
        self.assertEqual(self._call(self._base(vstep=0.01, vmin=0.02), volume=0.01)["error"],
                         "below_min_reduction")

    def test_remainder_below_min_rejected(self):
        out = self._call(self._base(pos_volume=0.05, vstep=0.01, vmin=0.03), volume=0.04)
        self.assertEqual(out["error"], "remainder_below_min")

    def test_position_not_found(self):
        self.assertEqual(self._call(self._base(positions="empty"))["error"], "position_not_found")

    def test_positions_get_failed(self):
        self.assertEqual(self._call(self._base(positions="none"))["error"], "positions_get_failed")

    def test_unsupported_filling_mode_rejected(self):
        self.assertEqual(self._call(self._base(filling_mode=0))["error"], "unsupported_filling_mode")
