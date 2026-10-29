"""ADR-0063 — the execution-free administrative symbol-visibility op on the bridge.

Proves ``ensure_symbol_visible`` reveals EXACTLY ONE symbol via ``symbol_select(symbol, True)``, is idempotent
when already visible, PROVES the postcondition before claiming success, fails closed on every ambiguity,
ALWAYS detaches after a successful attach, and NEVER touches an order path. The bridge imports MetaTrader5
lazily, so MT5 is faked (same harness as tests_bridge_symbols).
"""
import importlib.util
import inspect
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

BRIDGE_PATH = settings.BASE_DIR.parent / "scripts" / "mt5_signal_bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("mt5_signal_bridge_ensure_visible", str(BRIDGE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _info(visible, trade_mode=4):
    i = mock.MagicMock()
    i.visible = visible
    i.trade_mode = trade_mode
    return i


def _fake_mt5(*, init=True, symbol_info=None, select=True):
    m = mock.MagicMock(name="MetaTrader5")
    m.initialize.return_value = init
    m.last_error.return_value = (1, "init failed")
    if isinstance(symbol_info, list):
        m.symbol_info.side_effect = symbol_info
    else:
        m.symbol_info.return_value = symbol_info
    m.symbol_select.return_value = select
    return m


class EnsureSymbolVisibleTests(SimpleTestCase):
    def setUp(self):
        self.b = _load_bridge()

    def _call(self, mt5, symbol="XAUUSD.std"):
        return self.b.ensure_symbol_visible(mt5, symbol, init_kwargs={})

    def test_reveals_non_visible_symbol_once(self):
        mt5 = _fake_mt5(symbol_info=[_info(False), _info(True)], select=True)
        res = self._call(mt5)
        self.assertTrue(res["ok"])
        self.assertTrue(res["changed"])
        self.assertFalse(res["before_visible"])
        self.assertTrue(res["after_visible"])
        mt5.symbol_select.assert_called_once_with("XAUUSD.std", True)
        mt5.shutdown.assert_called_once()

    def test_idempotent_when_already_visible(self):
        mt5 = _fake_mt5(symbol_info=_info(True))
        res = self._call(mt5)
        self.assertTrue(res["ok"])
        self.assertFalse(res["changed"])
        self.assertTrue(res["before_visible"])
        self.assertTrue(res["after_visible"])
        mt5.symbol_select.assert_not_called()          # idempotent: no mutation
        mt5.shutdown.assert_called_once()

    def test_unavailable_symbol_fails_closed(self):
        mt5 = _fake_mt5(symbol_info=None)              # symbol_info() -> None (terminal has no such symbol)
        res = self._call(mt5)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], self.b.SYMBOL_NOT_AVAILABLE_ON_MT5)
        mt5.symbol_select.assert_not_called()
        mt5.shutdown.assert_called_once()

    def test_not_selectable_fails_closed(self):
        mt5 = _fake_mt5(symbol_info=[_info(False)], select=False)
        res = self._call(mt5)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], self.b.SYMBOL_NOT_SELECTABLE_ON_MT5)
        mt5.shutdown.assert_called_once()

    def test_postcondition_not_confirmed_fails_closed(self):
        # symbol_select returns truthy but the terminal still does not report it visible -> fail-closed.
        mt5 = _fake_mt5(symbol_info=[_info(False), _info(False)], select=True)
        res = self._call(mt5)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], self.b.SYMBOL_VISIBILITY_NOT_CONFIRMED)
        mt5.symbol_select.assert_called_once_with("XAUUSD.std", True)
        mt5.shutdown.assert_called_once()

    def test_blank_wildcard_and_bulk_refused_without_touching_mt5(self):
        for bad in ("", "   ", "*", "XAU*", "XAUUSD.std,EURUSD", "XAU USD", "all symbols", "XAU;rm"):
            mt5 = _fake_mt5(symbol_info=_info(True))
            res = self.b.ensure_symbol_visible(mt5, bad, init_kwargs={})
            self.assertFalse(res["ok"], bad)
            self.assertEqual(res["error"], self.b.SYMBOL_INVALID_REQUEST, bad)
            mt5.initialize.assert_not_called()         # never even attaches
            mt5.symbol_select.assert_not_called()

    def test_init_failure_fails_closed(self):
        mt5 = _fake_mt5(init=False, symbol_info=_info(True))
        res = self._call(mt5)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "MT5_INIT_FAILED")
        mt5.symbol_info.assert_not_called()            # nothing inspected when attach failed
        mt5.symbol_select.assert_not_called()
        mt5.shutdown.assert_not_called()               # nothing attached -> nothing to detach

    def test_detaches_even_on_exception(self):
        mt5 = _fake_mt5(symbol_info=_info(False), select=True)
        mt5.symbol_info.side_effect = RuntimeError("boom")
        res = self._call(mt5)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "ENSURE_VISIBLE_ERROR")
        mt5.shutdown.assert_called_once()              # finally detaches on error

    # ---- negative controls: an order / validation path must NEVER be reachable ----
    def test_negative_control_no_order_path_called(self):
        mt5 = _fake_mt5(symbol_info=[_info(False), _info(True)], select=True)
        self._call(mt5)
        for banned in ("order_send", "order_check", "positions_get", "position_modify",
                       "order_calc_margin"):
            getattr(mt5, banned).assert_not_called()

    def test_negative_control_source_references_no_order_path(self):
        """Structural guard: the op's source may reference no order/validation symbol (drift guard)."""
        src = inspect.getsource(self.b.ensure_symbol_visible)
        for banned in ("order_send", "order_check", "execute_demo_order", "validate_broker_symbol",
                       "close_position", "modify_position", "process_job", "claim"):
            self.assertNotIn(banned, src, f"ensure_symbol_visible must not reference {banned}")


def _make_handler(mod):
    """An OHLCRequestHandler WITHOUT BaseHTTPRequestHandler.__init__ (no socket), responses recorded, so the
    real do_POST dispatch can be exercised (mirrors tests_bridge_threading._make_handler)."""
    h = object.__new__(mod.OHLCRequestHandler)
    h.responses = []
    h._send_json_response = lambda data, status_code=200: h.responses.append((status_code, data))
    return h


class EnsureSymbolVisibleRouteTests(SimpleTestCase):
    """Pin the NEW POST route's auth gate and dispatch wiring through the real do_POST (review finding 2)."""

    def setUp(self):
        self.b = _load_bridge()

    def test_route_requires_auth_and_does_not_reach_handler(self):
        h = _make_handler(self.b)
        h.path = "/mt5/ensure-symbol-visible"
        h._validate_token = lambda: False
        reached = {"v": False}
        h._handle_ensure_symbol_visible_request = lambda: reached.__setitem__("v", True)
        h.do_POST()
        self.assertFalse(reached["v"], "unauthenticated request must NOT reach the handler")
        self.assertIn((401, {"ok": False, "error": "unauthorized"}), h.responses)

    def test_authed_route_dispatches_to_handler(self):
        h = _make_handler(self.b)
        h.path = "/mt5/ensure-symbol-visible"
        h._validate_token = lambda: True
        reached = {"v": False}
        h._handle_ensure_symbol_visible_request = lambda: reached.__setitem__("v", True)
        h.do_POST()
        self.assertTrue(reached["v"], "authenticated request must dispatch to the ensure-symbol-visible handler")
