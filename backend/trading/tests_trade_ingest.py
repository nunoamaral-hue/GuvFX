"""GFX-PKT-CLOSE-OUTCOME — deals→positions trade ingestion (mt5_trade_ingest_worker).

Proves the ingest worker builds ONE Trade per MT5 position from raw deals, sourcing the
authoritative open price from the entry (DEAL_ENTRY_IN) deal and the close price/profit from the
exit (DEAL_ENTRY_OUT) deal — never inferred — and fails closed on partial/one-sided fills.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from mt5_trade_ingest_worker import build_positions_from_deals, upsert_trades
from trading.models import TradingAccount, Trade


def _deal(pid, entry, dtype, price, vol, *, time=1784000000, profit=0, comm=0, swap=0,
          comment="", ticket=None, symbol="XAUUSD", magic=0):
    """A raw MT5 deal dict as the bridge's /mt5/snapshots/deals returns it.
    entry: 0=IN(open) 1=OUT(close) 3=OUT_BY. dtype: 0=BUY 1=SELL."""
    return {"ticket": str(ticket or pid), "order": str(pid), "time": time, "type": dtype,
            "entry": entry, "symbol": symbol, "volume": vol, "price": price, "profit": profit,
            "commission": comm, "swap": swap, "magic": magic, "comment": comment,
            "position_id": str(pid)}


class BuildPositionsTests(TestCase):
    def test_closed_position_authoritative_open_and_close(self):
        # SELL opened @4054.61, closed (BUY) @4056.10 hitting SL, profit -2.78 on the exit deal.
        deals = [
            _deal(224238, 0, 1, 4054.61, 0.02, time=100, comment="WAY8L1"),
            _deal(224238, 1, 0, 4056.10, 0.02, time=200, profit=-2.78, comm=-0.05, swap=-0.01),
        ]
        (p,) = build_positions_from_deals(deals)
        self.assertEqual(p["side"], "SELL")
        self.assertEqual(p["open_price"], Decimal("4054.61"))
        self.assertEqual(p["close_price"], Decimal("4056.10"))     # from the OUT deal, not inferred
        self.assertEqual(p["profit"], Decimal("-2.78"))
        self.assertEqual(p["commission"], Decimal("-0.05"))
        self.assertEqual(p["swap"], Decimal("-0.01"))
        self.assertEqual(p["comment"], "WAY8L1")
        self.assertIsNotNone(p["close_time"])

    def test_open_position_only_in_deal_stays_open(self):
        (p,) = build_positions_from_deals([_deal(300, 0, 0, 1.1000, 0.01, comment="WAYX")])
        self.assertEqual(p["open_price"], Decimal("1.1"))
        self.assertIsNone(p["close_price"])
        self.assertIsNone(p["close_time"])

    def test_partial_close_fails_closed_to_open(self):
        deals = [_deal(400, 0, 0, 1.1000, 0.02), _deal(400, 1, 1, 1.1010, 0.01, profit=1.0)]
        (p,) = build_positions_from_deals(deals)
        self.assertIsNone(p["close_price"])  # OUT vol (0.01) < IN vol (0.02) → still open

    def test_no_position_id_skipped(self):
        # A balance/credit deal (position_id 0) can't form a trade.
        self.assertEqual(build_positions_from_deals([_deal(0, 0, 0, 1.0, 0.0)]), [])

    def test_three_legs_are_independent_positions(self):
        deals = []
        for i, pid in enumerate((501, 502, 503)):
            deals += [_deal(pid, 0, 1, 4054.6, 0.02, time=100, comment="WAY9L%d" % (i + 1)),
                      _deal(pid, 1, 0, 4056.1, 0.02, time=200, profit=-2.0)]
        pos = build_positions_from_deals(deals)
        self.assertEqual(len(pos), 3)
        self.assertTrue(all(p["close_price"] == Decimal("4056.1") for p in pos))

    def test_out_by_deal_counts_as_close(self):
        deals = [_deal(600, 0, 1, 4054.6, 0.02), _deal(600, 3, 0, 4055.0, 0.02, profit=-0.8)]
        (p,) = build_positions_from_deals(deals)
        self.assertEqual(p["close_price"], Decimal("4055"))

    def test_hedging_same_symbol_multi_strategy_ownership_survives_lifecycle(self):
        # B1.2 Phase 8/9: on a HEDGING account three strategies hold XAUUSD concurrently, each on its own
        # position_id. The deal-lifecycle builder yields THREE independent positions, each retaining ITS OWN
        # strategy identity (magic 1e9+id) + comment SOURCED FROM THE OPENING DEAL — proving ownership
        # survives the IN→OUT lifecycle and is not lost to the close deal.
        #
        # Critically, the CLOSE deals carry magic=0 / comment="" — matching real MT5 (an OUT deal's magic is
        # typically 0). So this test FAILS if the builder ever sourced magic/comment from the close deal
        # instead of the opening deal (the exact regression the ownership invariant must resist).
        specs = [(901, 1_000_000_010, "WAY10L1"), (902, 1_000_000_011, "WAY11L1"),
                 (903, 1_000_000_012, "WAY12L1")]
        deals = []
        for pid, magic, comment in specs:
            deals += [
                _deal(pid, 0, 1, 4054.6, 0.02, time=100, magic=magic, comment=comment, symbol="XAUUSD"),
                _deal(pid, 1, 0, 4056.1, 0.02, time=200, magic=0, comment="", symbol="XAUUSD", profit=-2.0),
            ]
        by_pid = {p["position_id"]: p for p in build_positions_from_deals(deals)}
        self.assertEqual(set(by_pid), {"901", "902", "903"})           # three independent positions
        for pid, magic, comment in specs:
            p = by_pid[str(pid)]
            self.assertEqual(p["symbol"], "XAUUSD")                     # same symbol, all three
            self.assertEqual(p["magic"], magic)     # OPENING-deal magic survives the close (not the OUT's 0)
            self.assertEqual(p["comment"], comment)  # OPENING-deal comment survives (not the OUT's "")
            self.assertIsNotNone(p["close_time"])                      # each closed independently
        # The three opening-deal magics stay distinct across the closed lifecycle (no cross-contamination).
        self.assertEqual(len({by_pid[str(pid)]["magic"] for pid, _, _ in specs}), 3)

    def test_missing_entry_field_no_open_leg_skipped(self):
        # Old bridge (no 'entry') → cannot identify the IN deal → skip (fail-closed, no bad trade).
        d = _deal(700, 0, 1, 4054.6, 0.02)
        d.pop("entry")
        self.assertEqual(build_positions_from_deals([d]), [])

    def test_deal_entry_inout_is_quarantined_not_built(self):
        # B1.2: a DEAL_ENTRY_INOUT (2) netting reversal is not representable in the single-row Trade schema.
        # It is quarantined (fail-closed, loud) instead of silently dropped / built into a corrupt row.
        import contextlib
        import io
        deals = [
            _deal(800, 0, 1, 4054.6, 0.10, time=100, comment="WAY9L1"),   # IN
            _deal(800, 2, 0, 4060.0, 0.20, time=200, profit=6.0),         # INOUT reversal
            _deal(800, 1, 1, 4066.0, 0.10, time=300, profit=6.0),         # OUT
        ]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pos = build_positions_from_deals(deals)
        self.assertEqual(pos, [])                                   # no corrupt Trade produced
        self.assertIn("QUARANTINE deal_inout", buf.getvalue())     # loud + greppable, never a silent drop
        self.assertIn("position_id=800", buf.getvalue())


class UpsertTradesTests(TestCase):
    def setUp(self):
        u = get_user_model().objects.create_user(username="iu", email="iu@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(user=u, name="D", account_number="ID1", broker_name="DemoBroker", is_demo=True)

    def _closed(self, pid, comment):
        return [_deal(pid, 0, 1, 4054.61, 0.02, time=100, comment=comment),
                _deal(pid, 1, 0, 4056.10, 0.02, time=200, profit=-2.78)]

    def test_creates_one_closed_trade_keyed_by_position(self):
        ins, upd = upsert_trades(self.acct, self._closed(224238, "WAY8L1"))
        self.assertEqual((ins, upd), (1, 0))
        t = Trade.objects.get(account=self.acct, ticket="224238")
        self.assertEqual(t.close_price, Decimal("4056.10"))
        self.assertEqual(t.profit, Decimal("-2.78"))
        self.assertEqual(t.comment, "WAY8L1")

    def test_resync_is_idempotent_no_duplicate(self):
        upsert_trades(self.acct, self._closed(224238, "WAY8L1"))
        ins, upd = upsert_trades(self.acct, self._closed(224238, "WAY8L1"))
        self.assertEqual(ins, 0)
        self.assertEqual(Trade.objects.filter(account=self.acct, ticket="224238").count(), 1)

    def test_open_then_close_fills_close_price(self):
        upsert_trades(self.acct, [_deal(224238, 0, 1, 4054.61, 0.02, time=100, comment="WAY8L1")])
        t = Trade.objects.get(account=self.acct, ticket="224238")
        self.assertIsNone(t.close_price)
        upsert_trades(self.acct, self._closed(224238, "WAY8L1"))
        t.refresh_from_db()
        self.assertEqual(t.close_price, Decimal("4056.10"))
