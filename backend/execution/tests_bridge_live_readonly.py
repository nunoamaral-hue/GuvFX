"""LIVE READ-ONLY MONITORING — bridge read/execute separation (ADR LIVE_READONLY_MONITORING_SAFETY_DESIGN).

Proves the Phase-1 safety contract at the bridge boundary:

* The READ handlers (fetch_positions, fetch_deals_snapshot, fetch_account_snapshot) now serve a LIVE
  (trade_mode=2) terminal — the old ``account_not_demo`` refusal is gone — and report the observed identity
  (login/server/trade_mode) so the backend firewall can verify environment. A FLAT live account returns
  ``ok:true`` with an EMPTY positions list (HTTP 200), NOT ``account_not_demo`` (HTTP 400).
* The EXECUTE/mutation handlers (execute_demo_order, close_position, modify_position) STILL hard-refuse a
  non-demo terminal with ``account_not_demo`` and NEVER call ``mt5.order_send``. A read can never trade.

Loaded + mocked exactly like the other bridge tests (``_load_bridge`` + a MetaTrader5 stand-in in sys.modules).
Guarded-attach stays unset, so ``guarded_initialize`` is a pass-through of ``mt5.initialize``.
"""
import sys
from unittest import mock

from django.test import SimpleTestCase

from execution.tests_bridge_symbols import _load_bridge

LIVE = 2   # trade_mode: 0=DEMO, 1=CONTEST, 2=REAL
DEMO = 0


def _acct(trade_mode=LIVE, login=55442, server="TradersWay-Live", balance=205.81, equity=205.81, currency="USD"):
    a = mock.MagicMock()
    a.trade_mode = trade_mode
    a.login = login
    a.server = server
    a.balance = balance
    a.equity = equity
    a.currency = currency
    return a


def _position(ticket=1, symbol="XAUUSD", ptype=0, volume=0.10, open_=2000.0, cur=2010.0, profit=100.0,
              magic=0, comment="obs"):
    p = mock.MagicMock()
    p.ticket = ticket
    p.symbol = symbol
    p.type = ptype
    p.volume = volume
    p.price_open = open_
    p.price_current = cur
    p.profit = profit
    p.magic = magic
    p.comment = comment
    return p


def _fake(trade_mode=LIVE, positions=(), deals=()):
    m = mock.MagicMock(name="MetaTrader5")
    m.initialize.return_value = True
    m.account_info.return_value = _acct(trade_mode=trade_mode)
    m.positions_get.return_value = positions
    m.history_deals_get.return_value = deals
    m.last_error.return_value = (0, "ok")
    return m


class BridgeLiveReadAllowed(SimpleTestCase):
    def setUp(self):
        self.bridge = _load_bridge()

    def _install(self, m):
        sys.modules["MetaTrader5"] = m
        self.addCleanup(lambda: sys.modules.pop("MetaTrader5", None))

    def test_fetch_positions_allows_live_and_reports_identity(self):
        m = _fake(trade_mode=LIVE, positions=(_position(profit=123.45),))
        self._install(m)
        r = self.bridge.fetch_positions("")
        self.assertTrue(r["ok"], r)                              # NOT account_not_demo
        self.assertNotEqual(r.get("error"), "account_not_demo")
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["positions"][0]["profit"], 123.45)    # simulated floating P/L flows through
        self.assertEqual(r["trade_mode"], LIVE)                  # observed environment reported to the firewall
        self.assertEqual(r["account_login"], "55442")
        self.assertEqual(r["account_server"], "TradersWay-Live")
        m.order_send.assert_not_called()                         # a read never places an order

    def test_fetch_positions_flat_live_returns_empty_ok_not_400(self):
        m = _fake(trade_mode=LIVE, positions=())
        self._install(m)
        r = self.bridge.fetch_positions("")
        self.assertTrue(r["ok"])                                 # 200 path (dispatch sends 200 when ok)
        self.assertEqual(r["positions"], [])
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["trade_mode"], LIVE)

    def test_fetch_deals_snapshot_allows_live_and_reports_trade_mode(self):
        m = _fake(trade_mode=LIVE, deals=())
        self._install(m)
        r = self.bridge.fetch_deals_snapshot("guvfx_u_55442")
        self.assertTrue(r["ok"])
        self.assertNotEqual(r.get("error"), "account_not_demo")
        self.assertEqual(r["trade_mode"], LIVE)
        self.assertEqual(r["account_login"], "55442")

    def test_fetch_account_snapshot_reports_trade_mode_for_live(self):
        m = _fake(trade_mode=LIVE)
        self._install(m)
        r = self.bridge.fetch_account_snapshot("guvfx_u_55442")
        self.assertTrue(r["ok"])
        self.assertEqual(r["trade_mode"], LIVE)
        self.assertEqual(r["balance"], 205.81)

    def test_demo_reads_unchanged(self):
        # DEMO behaviour preserved byte-for-byte: a demo terminal still reads fine and reports trade_mode 0.
        m = _fake(trade_mode=DEMO, positions=(_position(),))
        self._install(m)
        r = self.bridge.fetch_positions("")
        self.assertTrue(r["ok"])
        self.assertEqual(r["trade_mode"], DEMO)


class BridgeExecuteStillRefusesLive(SimpleTestCase):
    """The execute/mutation handlers keep the hard demo gate — a LIVE terminal is refused and order_send is
    NEVER reached. These are the invariants (8),(9),(10) the read change must not weaken."""

    def setUp(self):
        self.bridge = _load_bridge()

    def _install(self, m):
        sys.modules["MetaTrader5"] = m
        self.addCleanup(lambda: sys.modules.pop("MetaTrader5", None))

    def test_execute_demo_order_refuses_live_no_order_send(self):
        m = _fake(trade_mode=LIVE)
        self._install(m)
        r = self.bridge.execute_demo_order(
            {"symbol": "EURUSD", "side": "BUY", "lots": 0.01, "magic": 1, "comment": "t"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "account_not_demo")
        m.order_send.assert_not_called()

    def test_close_position_refuses_live_no_order_send(self):
        m = _fake(trade_mode=LIVE, positions=(_position(),))
        self._install(m)
        r = self.bridge.close_position(1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "account_not_demo")
        m.order_send.assert_not_called()

    def test_modify_position_refuses_live_no_order_send(self):
        m = _fake(trade_mode=LIVE, positions=(_position(),))
        self._install(m)
        r = self.bridge.modify_position(1, sl=1990.0, tp=2020.0)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "account_not_demo")
        m.order_send.assert_not_called()
