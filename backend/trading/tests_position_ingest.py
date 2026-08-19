"""Canonical deal->position builder + SyncNowView position-shaped persistence.

Locks the invariant the round-trip read model depends on: BOTH trade writers persist ONE Trade row per MT5
position (not per deal). A per-deal writer would emit entry+exit legs as two rows and corrupt round-trip
counts / observed_stats for the analytics trade-history view.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from trading.models import TradingAccount, Trade
from trading.position_ingest import build_positions_from_deals
from trading.views import _upsert_trades

User = get_user_model()


def _deal(**kw):
    d = {"position_id": "P1", "ticket": "1", "symbol": "XAUUSD", "type": 0, "entry": 0,
         "time": 1_700_000_000, "price": "4431.0", "volume": "0.01", "profit": "0",
         "commission": "0", "swap": "0", "comment": "WAY258L1", "magic": 0}
    d.update(kw)
    return d


class BuildPositionsFromDeals(TestCase):
    def test_entry_plus_exit_deals_form_one_closed_position(self):
        deals = [
            _deal(ticket="10", entry=0, time=1_700_000_000, price="4431.0", profit="0"),        # IN
            _deal(ticket="11", entry=1, time=1_700_000_600, price="4435.0", profit="4.00"),      # OUT
        ]
        pos = build_positions_from_deals(deals)
        self.assertEqual(len(pos), 1)
        p = pos[0]
        self.assertEqual(p["position_id"], "P1")
        self.assertEqual(p["side"], "BUY")
        self.assertEqual(p["profit"], Decimal("4.00"))       # profit from the OUT deal, counted once
        self.assertIsNotNone(p["close_time"])                # closed (OUT vol >= IN vol)
        self.assertEqual(Decimal(p["open_price"]), Decimal("4431.0"))    # authoritative IN price
        self.assertEqual(Decimal(p["close_price"]), Decimal("4435.0"))   # authoritative OUT price

    def test_entry_only_is_open_position(self):
        pos = build_positions_from_deals([_deal(ticket="20", entry=0)])
        self.assertEqual(len(pos), 1)
        self.assertIsNone(pos[0]["close_time"])              # fail-closed to OPEN, no premature outcome

    def test_deals_grouped_by_position_id(self):
        deals = [
            _deal(position_id="A", ticket="1", entry=0), _deal(position_id="A", ticket="2", entry=1, profit="1"),
            _deal(position_id="B", ticket="3", entry=0), _deal(position_id="B", ticket="4", entry=1, profit="2"),
        ]
        pos = {p["position_id"]: p for p in build_positions_from_deals(deals)}
        self.assertEqual(set(pos), {"A", "B"})               # two positions, not four deal rows

    def test_no_position_id_is_skipped(self):
        self.assertEqual(build_positions_from_deals([_deal(position_id="0", entry=0)]), [])

    def test_malformed_element_is_skipped_not_fatal(self):
        # A non-dict element (None / junk) must be skipped, never abort the batch (robustness LOW fix).
        deals = [None, "junk", 42,
                 _deal(position_id="P1", ticket="1", entry=0),
                 _deal(position_id="P1", ticket="2", entry=1, profit="3.00", time=1_700_000_600)]
        pos = build_positions_from_deals(deals)
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]["profit"], Decimal("3.00"))


class SyncNowUpsertProducesPositionRows(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pi", email="pi@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Hosted Workspace", account_number="1302575", broker_name="DemoBroker")

    def test_two_deals_of_one_position_become_ONE_trade_row(self):
        deals = [
            _deal(position_id="P9", ticket="90", entry=0, price="4431.0", profit="0"),
            _deal(position_id="P9", ticket="91", entry=1, price="4436.0", profit="5.00", time=1_700_001_000),
        ]
        inserted, updated, skipped, _ = _upsert_trades(self.acct, deals)
        self.assertEqual(inserted, 1)                        # ONE position row (was: 2 deal rows)
        rows = Trade.objects.filter(account=self.acct)
        self.assertEqual(rows.count(), 1)
        t = rows.get()
        self.assertEqual(t.ticket, "P9")                     # keyed by position_id
        self.assertEqual(t.profit, Decimal("5.00"))          # counted once
        self.assertIsNotNone(t.close_time)

    def test_cutover_excludes_pre_customer_position(self):
        from django.utils import timezone
        import datetime
        self.acct.ingest_cutover_time = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
        self.acct.save(update_fields=["ingest_cutover_time"])
        # deal time 1_700_000_000 = 2023-11-14 -> before the 2024 cutover -> skipped
        inserted, updated, skipped, _ = _upsert_trades(self.acct, [
            _deal(position_id="OLD", ticket="1", entry=0), _deal(position_id="OLD", ticket="2", entry=1)])
        self.assertEqual(inserted, 0)
        self.assertEqual(skipped, 1)
        self.assertEqual(Trade.objects.filter(account=self.acct).count(), 0)
