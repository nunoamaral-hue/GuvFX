"""LIVE READ-ONLY MONITORING — bridge read/execute separation (ADR LIVE_READONLY_MONITORING_SAFETY_DESIGN,
refined by ADR-0072 / GFX-LIVE-POSITIONS-READONLY-001).

Proves the safety contract at the bridge boundary:

* ``fetch_positions`` serves a LIVE (trade_mode=2) terminal ONLY when the purpose-specific read-only flag
  ``MT5_LIVE_READONLY_POSITIONS_ENABLED`` is set (ADR-0072 default-OFF refinement). With the flag OFF a LIVE
  terminal is refused ``account_not_demo`` (HTTP 400) exactly as the pre-#472 bridge; DEMO reads are unaffected
  by the flag. A FLAT live account (flag ON) returns ``ok:true`` with an EMPTY positions list (HTTP 200).
* ``fetch_deals_snapshot`` / ``fetch_account_snapshot`` remain UNCONDITIONALLY ungated for LIVE (out of this
  packet's scope — #472 unchanged): they report the observed identity so the backend firewall verifies env.
* The EXECUTE/mutation handlers (execute_demo_order, close_position, modify_position) STILL hard-refuse a
  non-demo terminal with ``account_not_demo`` and NEVER call ``mt5.order_send`` — even with the READ flag ON.
  A read can never trade; the read flag can never arm execution.

Loaded + mocked exactly like the other bridge tests (``_load_bridge`` + a MetaTrader5 stand-in in sys.modules).
Guarded-attach stays unset, so ``guarded_initialize`` is a pass-through of ``mt5.initialize``.
"""
import os
import sys
from unittest import mock

from django.test import SimpleTestCase

from execution.tests_bridge_symbols import _load_bridge

LIVE = 2   # trade_mode: 0=DEMO, 1=CONTEST, 2=REAL
DEMO = 0
RO_FLAG = "MT5_LIVE_READONLY_POSITIONS_ENABLED"


def _readonly_on():
    """Context manager: the LIVE read-only positions monitoring flag ON (read fresh per call by the bridge)."""
    return mock.patch.dict(os.environ, {RO_FLAG: "1"})


def _readonly_off():
    """Context manager: the flag explicitly cleared (default posture)."""
    return mock.patch.dict(os.environ, {RO_FLAG: ""})


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
        # ADR-0072 req (2): LIVE + read-only flag ON => positions returned + provenance + observation time.
        m = _fake(trade_mode=LIVE, positions=(_position(profit=123.45),))
        self._install(m)
        with _readonly_on():
            r = self.bridge.fetch_positions("")
        self.assertTrue(r["ok"], r)                              # NOT account_not_demo
        self.assertNotEqual(r.get("error"), "account_not_demo")
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["positions"][0]["profit"], 123.45)    # simulated floating P/L flows through
        self.assertEqual(r["trade_mode"], LIVE)                  # observed environment reported to the firewall
        self.assertEqual(r["account_login"], "55442")
        self.assertEqual(r["account_server"], "TradersWay-Live")
        self.assertIn("observed_at", r)                          # observation timestamp (provenance)
        m.order_send.assert_not_called()                         # a read never places an order

    def test_fetch_positions_live_refused_when_flag_off(self):
        # ADR-0072 req (1) + (18 guard): LIVE + flag OFF => account_not_demo (HTTP 400), fail-closed by default.
        # Removing the ``and not _live_readonly_positions_enabled()`` guard would make this read succeed => this
        # test fails, pinning the gate.
        m = _fake(trade_mode=LIVE, positions=(_position(profit=123.45),))
        self._install(m)
        with _readonly_off():
            r = self.bridge.fetch_positions("")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "account_not_demo")
        self.assertNotIn("positions", r)                         # no exposure leaked on refusal
        m.order_send.assert_not_called()

    def test_fetch_positions_includes_sl_tp_for_copier_observer(self):
        # ADR-0051: the read handler ADDITIVELY exposes sl/tp (+ time/identifier) so the read-only
        # copier observer can diff protective-level changes. Still a PURE READ — no order_send.
        p = _position()
        p.sl = 1990.0
        p.tp = 2100.0
        p.time = 1_700_000_000
        p.time_update = 1_700_000_050
        p.identifier = 777
        m = _fake(trade_mode=LIVE, positions=(p,))
        self._install(m)
        with _readonly_on():
            r = self.bridge.fetch_positions("")
        self.assertTrue(r["ok"], r)
        pos = r["positions"][0]
        self.assertEqual(pos["sl"], 1990.0)
        self.assertEqual(pos["tp"], 2100.0)
        self.assertEqual(pos["identifier"], 777)
        m.order_send.assert_not_called()                         # a read never places an order

    def test_fetch_positions_flat_live_returns_empty_ok_not_400(self):
        # ADR-0072 req (13): a truthful EMPTY only after a SUCCESSFUL broker read (flag ON, positions_get ()).
        m = _fake(trade_mode=LIVE, positions=())
        self._install(m)
        with _readonly_on():
            r = self.bridge.fetch_positions("")
        self.assertTrue(r["ok"])                                 # 200 path (dispatch sends 200 when ok)
        self.assertEqual(r["positions"], [])
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["trade_mode"], LIVE)

    def test_fetch_positions_none_is_error_not_flat(self):
        # ADR-0051 BLOCKER-1 / ADR-0072 req (14): positions_get() returning None is a FAILURE, not a flat
        # account. It must report an error (unavailable, never empty) rather than masquerade as empty.
        m = _fake(trade_mode=LIVE)
        m.positions_get.return_value = None
        self._install(m)
        with _readonly_on():
            r = self.bridge.fetch_positions("")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "positions_get_failed")
        m.order_send.assert_not_called()

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
        # ADR-0072 req (3): DEMO behaviour is independent of the read-only flag — a demo terminal reads fine
        # with the flag OFF (default) AND ON, always reporting trade_mode 0.
        m = _fake(trade_mode=DEMO, positions=(_position(),))
        self._install(m)
        with _readonly_off():
            r_off = self.bridge.fetch_positions("")
        with _readonly_on():
            r_on = self.bridge.fetch_positions("")
        for r in (r_off, r_on):
            self.assertTrue(r["ok"])
            self.assertEqual(r["trade_mode"], DEMO)
            self.assertEqual(r["count"], 1)
        m.order_send.assert_not_called()


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

    def test_read_flag_on_does_not_arm_execution(self):
        # ADR-0072 req (11)+(12): the READ-ONLY flag must NEVER weaken an execute path. With the read flag ON,
        # every mutation handler STILL hard-refuses a LIVE terminal and never reaches order_send.
        m = _fake(trade_mode=LIVE, positions=(_position(),))
        self._install(m)
        with _readonly_on():
            r_exec = self.bridge.execute_demo_order(
                {"symbol": "EURUSD", "side": "BUY", "lots": 0.01, "magic": 1, "comment": "t"})
            r_close = self.bridge.close_position(1)
            r_modify = self.bridge.modify_position(1, sl=1990.0, tp=2020.0)
        for r in (r_exec, r_close, r_modify):
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
