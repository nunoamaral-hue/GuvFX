"""GFX-PKT-BROKER-SYMBOL-BRIDGE-ALIGNMENT — the Windows bridge symbol gate.

Proves the bridge's hardcoded symbol allowlist is replaced by MT5-native validation
(validate_broker_symbol → symbol_info/symbol_select), fail-closed with
SYMBOL_NOT_AVAILABLE_ON_MT5 / SYMBOL_NOT_SELECTABLE_ON_MT5. Baseline symbols still
trade; a broker symbol (incl. an exact suffix) trades iff the terminal offers it; the
resolved broker symbol is what is placed while the provider symbol is preserved for
audit; and NO order/order_check is issued for an unavailable symbol. The lot-cap,
side, SL/TP and order_check rails are unchanged.

The bridge (scripts/mt5_signal_bridge.py) is a standalone Windows script that imports
MetaTrader5 lazily inside each order function, so it loads on CI/Linux and MT5 is faked
via sys.modules — the same harness used by tests_e2b_shadow.py.
"""
import importlib.util
import pathlib
import sys
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

BRIDGE_PATH = settings.BASE_DIR.parent / "scripts" / "mt5_signal_bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("mt5_signal_bridge_symbols", str(BRIDGE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_mt5(*, symbol_info_map=None, default_visible=True, select_returns=True,
             demo=True, check_retcode=0):
    """MetaTrader5 stand-in.

    ``symbol_info_map`` maps a symbol to ``(available: bool, visible: bool)``; symbols
    not in the map fall back to ``(True, default_visible)``. ``select_returns`` is the
    return of ``symbol_select``. ``demo`` sets account trade_mode (0=DEMO else 2=REAL).
    order_send is a plain auto-mock so tests can assert it is/ isn't called.
    """
    m = mock.MagicMock(name="MetaTrader5")
    m.initialize.return_value = True
    acct = mock.MagicMock(); acct.trade_mode = 0 if demo else 2
    m.account_info.return_value = acct

    def _sinfo(sym):
        available, visible = (symbol_info_map or {}).get(sym, (True, default_visible))
        if not available:
            return None
        info = mock.MagicMock(); info.visible = visible
        info.point = 0.0001; info.digits = 5
        info.trade_stops_level = 0; info.trade_freeze_level = 0
        info.trade_tick_size = 0.0001
        return info

    m.symbol_info.side_effect = _sinfo
    m.symbol_select.return_value = select_returns
    tick = mock.MagicMock(); tick.ask = 1.0850; tick.bid = 1.0849
    m.symbol_info_tick.return_value = tick
    check = mock.MagicMock()
    check.retcode = check_retcode; check.margin = 5.0; check.margin_free = 9995.0
    check.comment = "Done"; check.balance = 10000.0
    m.order_check.return_value = check
    send = mock.MagicMock(); send.retcode = m.TRADE_RETCODE_DONE
    send.order = 111; send.deal = 222; send.price = 1.0850; send.volume = 0.01
    m.order_send.return_value = send
    return m


def _order(symbol="EURUSD", **over):
    o = {"symbol": symbol, "side": "BUY", "lots": 0.01, "comment": "WAY1L1",
         "magic": 7, "sl": 1.0800, "tp": 1.0900}
    o.update(over)
    return o


class BridgeSymbolBase(SimpleTestCase):
    def setUp(self):
        self.bridge = _load_bridge()

    def _install(self, mt5):
        sys.modules["MetaTrader5"] = mt5
        self.addCleanup(lambda: sys.modules.pop("MetaTrader5", None))
        return mt5


class ValidateBrokerSymbolHelperTests(BridgeSymbolBase):
    """The pure MT5-native validator underneath every order path."""

    def test_available_visible_symbol_ok(self):
        mt5 = _fake_mt5()
        ok, info, err = self.bridge.validate_broker_symbol(mt5, "EURUSD")
        self.assertTrue(ok); self.assertIsNone(err); self.assertIsNotNone(info)

    def test_exact_broker_suffix_symbol_ok(self):
        # The bridge validates the EXACT symbol handed to it (backend already resolved it).
        mt5 = _fake_mt5(symbol_info_map={"BTCUSD.": (True, True)})
        ok, _, err = self.bridge.validate_broker_symbol(mt5, "BTCUSD.")
        self.assertTrue(ok); self.assertIsNone(err)

    def test_unavailable_symbol_not_available_reason(self):
        mt5 = _fake_mt5(symbol_info_map={"BTCUSD": (False, False)})
        ok, info, err = self.bridge.validate_broker_symbol(mt5, "BTCUSD")
        self.assertFalse(ok); self.assertIsNone(info)
        self.assertEqual(err, self.bridge.SYMBOL_NOT_AVAILABLE_ON_MT5)
        mt5.symbol_select.assert_not_called()

    def test_non_visible_but_selectable_ok(self):
        mt5 = _fake_mt5(symbol_info_map={"BTCUSD": (True, False)}, select_returns=True)
        ok, _, err = self.bridge.validate_broker_symbol(mt5, "BTCUSD")
        self.assertTrue(ok); self.assertIsNone(err)
        mt5.symbol_select.assert_called_once_with("BTCUSD", True)

    def test_non_visible_non_selectable_reason(self):
        mt5 = _fake_mt5(symbol_info_map={"BTCUSD": (True, False)}, select_returns=False)
        ok, info, err = self.bridge.validate_broker_symbol(mt5, "BTCUSD")
        self.assertFalse(ok); self.assertIsNone(info)
        self.assertEqual(err, self.bridge.SYMBOL_NOT_SELECTABLE_ON_MT5)


class DemoOrderSymbolTests(BridgeSymbolBase):
    """execute_demo_order — the live POST /mt5/order path (the Wayond demo order)."""

    # 1 + 2: baseline symbols still trade.
    def test_1_eurusd_still_accepted(self):
        mt5 = self._install(_fake_mt5())
        res = self.bridge.execute_demo_order(_order("EURUSD"))
        self.assertTrue(res["ok"])
        mt5.order_send.assert_called_once()

    def test_2_xauusd_still_accepted(self):
        mt5 = self._install(_fake_mt5())
        res = self.bridge.execute_demo_order(_order("XAUUSD"))
        self.assertTrue(res["ok"])
        mt5.order_send.assert_called_once()

    # 3: BTCUSD accepted when MT5 offers it (was impossible with the static list).
    def test_3_btcusd_accepted_when_available(self):
        mt5 = self._install(_fake_mt5(symbol_info_map={"BTCUSD": (True, True)}))
        res = self.bridge.execute_demo_order(_order("BTCUSD"))
        self.assertTrue(res["ok"])
        self.assertEqual(mt5.order_send.call_args.args[0]["symbol"], "BTCUSD")

    # 4 + broker-symbol-placed + 12: exact suffix accepted, placed under broker symbol,
    # provider symbol preserved (and never leaked into the MT5 request).
    def test_4_broker_suffix_placed_provider_preserved(self):
        mt5 = self._install(_fake_mt5(symbol_info_map={"BTCUSD.": (True, True)}))
        res = self.bridge.execute_demo_order(
            _order("BTCUSD.", provider_symbol="BTCUSD", comment="WAY9L1"))
        self.assertTrue(res["ok"])
        req = mt5.order_send.call_args.args[0]
        self.assertEqual(req["symbol"], "BTCUSD.")          # broker symbol placed
        self.assertNotIn("provider_symbol", req)            # audit-only, not traded on
        self.assertEqual(res["provider_symbol"], "BTCUSD")  # preserved in the result

    # 5 + 11: unsupported symbol rejected fail-closed, order_send NOT called.
    def test_5_unsupported_symbol_rejected(self):
        mt5 = self._install(_fake_mt5(symbol_info_map={"DOGEUSD": (False, False)}))
        res = self.bridge.execute_demo_order(_order("DOGEUSD"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], self.bridge.SYMBOL_NOT_AVAILABLE_ON_MT5)
        mt5.order_send.assert_not_called()

    # 6: non-visible but selectable symbol accepted after symbol_select.
    def test_6_non_visible_selectable_accepted(self):
        mt5 = self._install(_fake_mt5(symbol_info_map={"BTCUSD": (True, False)}, select_returns=True))
        res = self.bridge.execute_demo_order(_order("BTCUSD"))
        self.assertTrue(res["ok"])
        mt5.symbol_select.assert_called_once_with("BTCUSD", True)
        mt5.order_send.assert_called_once()

    # 7: non-visible and non-selectable rejected fail-closed, no order.
    def test_7_non_visible_non_selectable_rejected(self):
        mt5 = self._install(_fake_mt5(symbol_info_map={"BTCUSD": (True, False)}, select_returns=False))
        res = self.bridge.execute_demo_order(_order("BTCUSD"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], self.bridge.SYMBOL_NOT_SELECTABLE_ON_MT5)
        mt5.order_send.assert_not_called()

    # 9: lot cap still enforced (before MT5 is even touched), no order.
    def test_9_lot_cap_still_enforced(self):
        mt5 = self._install(_fake_mt5())
        res = self.bridge.execute_demo_order(_order("EURUSD", lots=0.05))
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "lots_out_of_range")
        mt5.order_send.assert_not_called()

    # side gate preserved.
    def test_side_gate_preserved(self):
        mt5 = self._install(_fake_mt5())
        res = self.bridge.execute_demo_order(_order("EURUSD", side="HODL"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "side_not_allowed")
        mt5.order_send.assert_not_called()

    # 12 (reject path also preserves provider_symbol).
    def test_12_provider_symbol_preserved_on_rejection(self):
        mt5 = self._install(_fake_mt5(symbol_info_map={"DOGEUSD": (False, False)}))
        res = self.bridge.execute_demo_order(_order("DOGEUSD", provider_symbol="DOGEUSD"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["provider_symbol"], "DOGEUSD")

    # demo-account gate still enforced (real account refused before any order).
    def test_non_demo_account_refused(self):
        mt5 = self._install(_fake_mt5(demo=False))
        res = self.bridge.execute_demo_order(_order("EURUSD"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "account_not_demo")
        mt5.order_send.assert_not_called()


class ShadowOrderCheckSymbolTests(BridgeSymbolBase):
    """shadow_order_check — POST /mt5/order_check dry-run; order_check only, never send."""

    # 10: order_check still required (called exactly once on the happy path).
    def test_10_order_check_still_required(self):
        mt5 = self._install(_fake_mt5())
        res = self.bridge.shadow_order_check(_order("EURUSD"))
        self.assertTrue(res["ok"])
        mt5.order_check.assert_called_once()
        mt5.order_send.assert_not_called()

    def test_btcusd_shadow_accepted_when_available(self):
        mt5 = self._install(_fake_mt5(symbol_info_map={"BTCUSD": (True, True)}))
        res = self.bridge.shadow_order_check(_order("BTCUSD"))
        self.assertTrue(res["ok"])
        mt5.order_check.assert_called_once()
        mt5.order_send.assert_not_called()

    def test_unsupported_symbol_no_order_check(self):
        mt5 = self._install(_fake_mt5(symbol_info_map={"DOGEUSD": (False, False)}))
        res = self.bridge.shadow_order_check(_order("DOGEUSD"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], self.bridge.SYMBOL_NOT_AVAILABLE_ON_MT5)
        mt5.order_check.assert_not_called()
        mt5.order_send.assert_not_called()

    def test_shadow_provider_symbol_preserved(self):
        mt5 = self._install(_fake_mt5(symbol_info_map={"BTCUSD.": (True, True)}))
        res = self.bridge.shadow_order_check(
            _order("BTCUSD.", provider_symbol="BTCUSD"))
        self.assertTrue(res["ok"])
        self.assertEqual(res["provider_symbol"], "BTCUSD")
        self.assertNotIn("provider_symbol", res["request"])  # audit-only, not in MT5 request

    def test_shadow_request_still_identical_to_live(self):
        # The request built for order_check must remain identical to the live order_send
        # request (provider_symbol lives outside the request, so equality holds).
        mt5 = self._install(_fake_mt5(symbol_info_map={"BTCUSD.": (True, True)}))
        self.bridge.execute_demo_order(_order("BTCUSD.", provider_symbol="BTCUSD"))
        self.bridge.shadow_order_check(_order("BTCUSD.", provider_symbol="BTCUSD"))
        self.assertEqual(mt5.order_send.call_args.args[0], mt5.order_check.call_args.args[0])


class StrategyPathAndBoundaryTests(BridgeSymbolBase):
    """validate_job_safety (strategy PLACE_ORDER pre-check) + the no-allowlist boundary."""

    def _job(self, **payload_over):
        payload = {"is_demo": True, "symbol": "EURUSD", "lots": 0.01, "side": "BUY",
                   "sl_price": 1.0800, "tp_price": 1.0900}
        payload.update(payload_over)
        return {"job_type": "PLACE_ORDER", "payload": payload}

    # 8: SL/TP still mandatory on the strategy path.
    def test_8_sltp_still_mandatory(self):
        ok, _ = self.bridge.validate_job_safety(self._job())
        self.assertTrue(ok)
        j = self._job(); del j["payload"]["sl_price"]
        ok, err = self.bridge.validate_job_safety(j)
        self.assertFalse(ok); self.assertIn("SL", err)
        j = self._job(); del j["payload"]["tp_price"]
        ok, err = self.bridge.validate_job_safety(j)
        self.assertFalse(ok); self.assertIn("TP", err)

    def test_strategy_precheck_no_longer_blocks_nonbaseline_symbol(self):
        # The allowlist is gone from the pre-check: a non-baseline symbol passes the
        # payload gate (MT5-native availability is then enforced in execute_mt5_trade).
        ok, err = self.bridge.validate_job_safety(self._job(symbol="BTCUSD",
                                                            sl_price=60000, tp_price=64000))
        self.assertTrue(ok, err)

    def test_strategy_precheck_blank_symbol_rejected(self):
        ok, err = self.bridge.validate_job_safety(self._job(symbol=""))
        self.assertFalse(ok); self.assertIn("Symbol", err)

    def test_strategy_precheck_lot_cap_preserved(self):
        ok, err = self.bridge.validate_job_safety(self._job(lots=0.05))
        self.assertFalse(ok); self.assertIn("exceeds max", err)

    def test_execute_mt5_trade_rejects_unavailable_symbol_no_order(self):
        mt5 = self._install(_fake_mt5(symbol_info_map={"DOGEUSD": (False, False)}))
        ok, result, err = self.bridge.execute_mt5_trade(
            {"id": 1, "job_type": "PLACE_ORDER",
             "payload": {"symbol": "DOGEUSD", "side": "BUY", "lots": 0.01,
                         "sl_price": 1.0, "tp_price": 2.0, "provider_symbol": "DOGEUSD"}})
        self.assertFalse(ok)
        self.assertEqual(result.get("error"), self.bridge.SYMBOL_NOT_AVAILABLE_ON_MT5)
        self.assertEqual(result.get("provider_symbol"), "DOGEUSD")
        mt5.order_send.assert_not_called()

    def test_no_static_symbol_allowlist_remains(self):
        # The hardcoded allowlist constants are gone entirely (module + source).
        self.assertFalse(hasattr(self.bridge, "ALLOWED_SYMBOLS"))
        self.assertFalse(hasattr(self.bridge, "DEMO_ORDER_ALLOWED_SYMBOLS"))
        src = pathlib.Path(BRIDGE_PATH).read_text()
        self.assertNotIn("ALLOWED_SYMBOLS", src)  # neither constant name appears anywhere
