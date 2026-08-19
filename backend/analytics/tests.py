"""
TX-AC1F — trade-history balance/equity IDOR regression tests.

Proves the ownership gate on GET /api/analytics/trade-history/:
  - a non-staff user CANNOT read a foreign account's balance/equity
  - a non-staff user CAN read their own account's balance/equity
  - staff bypass is preserved
  - foreign and nonexistent account ids both return 404 (no existence oracle)
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from trading.models import TradingAccount
from mt5.models import Mt5Instance

User = get_user_model()

_FAKE_BALANCE = {"balance": 12345.67, "equity": 12300.00, "currency": "USD"}
_MOCK = "analytics.views_trade_history._fetch_mt5_account_balance"


class TradeHistoryBalanceIDORTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="owner_ac1f", email="owner_ac1f@test.local", password="x", is_staff=False)
        cls.other = User.objects.create_user(username="other_ac1f", email="other_ac1f@test.local", password="x", is_staff=False)
        cls.staff = User.objects.create_user(username="staff_ac1f", email="staff_ac1f@test.local", password="x", is_staff=True)
        # Instance with a windows_username so the balance/equity enrichment path runs.
        cls.inst = Mt5Instance.objects.create(hostname="ac1f-host", windows_username="guvfx_u_ac1f")
        cls.acct = TradingAccount.objects.create(
            user=cls.owner, name="Owner Account", account_number="AC1F-OWN", mt5_instance=cls.inst,
            broker_name="DemoBroker",
        )

    def _get(self, user, account_id):
        c = APIClient()
        c.force_authenticate(user=user)
        # secure=True → https request, avoids SECURE_SSL_REDIRECT 301 in tests.
        return c.get(reverse("trade-history"), {"account_id": account_id, "mode": "roundtrip"}, secure=True)

    @patch(_MOCK, return_value=_FAKE_BALANCE)
    def test_owner_can_read_own_balance(self, _m):
        r = self._get(self.owner, self.acct.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data.get("mt5_balance_current"), _FAKE_BALANCE["balance"])
        self.assertEqual(r.data.get("mt5_equity_current"), _FAKE_BALANCE["equity"])

    @patch(_MOCK, return_value=_FAKE_BALANCE)
    def test_foreign_user_cannot_read_balance(self, m):
        r = self._get(self.other, self.acct.id)
        # 404 (not 200) — and the balance enrichment must never have been invoked.
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("mt5_balance_current", (r.data or {}))
        m.assert_not_called()

    @patch(_MOCK, return_value=_FAKE_BALANCE)
    def test_staff_bypass_preserved(self, _m):
        r = self._get(self.staff, self.acct.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data.get("mt5_balance_current"), _FAKE_BALANCE["balance"])

    @patch(_MOCK, return_value=_FAKE_BALANCE)
    def test_no_existence_oracle(self, _m):
        # Foreign-existing and nonexistent ids return the SAME 404 for a non-staff user.
        foreign = self._get(self.other, self.acct.id)
        missing = self._get(self.other, 99999999)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(foreign.data, missing.data)


class LongOnlyRoundTripReadModelTests(APITestCase):
    """P0 read-model regression: each Trade row is a COMPLETE POSITION, so roundtrip mode must render each
    CLOSED position as one round-trip. The prior FIFO BUY<->SELL pairing silently dropped every unpaired leg,
    so a long-only (BUY-only) account rendered EMPTY despite correct durable data (the P0 symptom)."""

    @classmethod
    def setUpTestData(cls):
        from decimal import Decimal
        from django.utils import timezone
        from datetime import timedelta
        from trading.models import Trade
        cls.owner = User.objects.create_user(username="rt_own", email="rt_own@test.local", password="x")
        cls.acct = TradingAccount.objects.create(
            user=cls.owner, name="Hosted Workspace", account_number="RT-1302575", broker_name="DemoBroker")
        base = timezone.now() - timedelta(hours=5)
        # 3 CLOSED long positions (all BUY — a long-only Wayond tenant) + 1 still-open position.
        specs = [("T1", "0.27", True), ("T2", "1.62", True), ("T3", "3.80", True), ("T4", "0.00", False)]
        for i, (tk, pnl, closed) in enumerate(specs):
            Trade.objects.create(
                account=cls.acct, ticket=tk, symbol="XAUUSD", side="BUY", volume=Decimal("0.01"),
                open_time=base + timedelta(minutes=i), open_price=Decimal("4431.00"),
                close_time=(base + timedelta(minutes=i + 5)) if closed else None,
                close_price=Decimal("4432.00") if closed else None, profit=Decimal(pnl))

    def _get(self):
        c = APIClient(); c.force_authenticate(user=self.owner)
        return c.get(reverse("trade-history"),
                     {"account_id": self.acct.id, "mode": "roundtrip", "stage": "ALL"}, secure=True)

    @patch(_MOCK, return_value=None)
    def test_all_buy_closed_positions_each_render_as_a_round_trip(self, _m):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 3)                      # 3 closed (the open one excluded)
        tickets = sorted(rt["trade_numbers"] for rt in r.data["trades"])
        self.assertEqual(tickets, ["T1", "T2", "T3"])             # NOT dropped as "orphan BUY legs"
        # aggregate net P&L equals the sum of position profits, counted ONCE (never doubled)
        net = sum(rt["net_pnl_money"] for rt in r.data["trades"])
        self.assertAlmostEqual(net, 0.27 + 1.62 + 3.80, places=2)
        # observed stats are populated (Dashboard "How am I doing" reads these)
        self.assertEqual(r.data["observed_stats"]["wins"], 3)

    @patch(_MOCK, return_value=None)
    def test_open_position_excluded_until_closed(self, _m):
        # The still-open position (T4, close_time=None) is not a completed round-trip.
        r = self._get()
        self.assertNotIn("T4", [rt["trade_numbers"] for rt in r.data["trades"]])

    @patch(_MOCK, return_value=None)
    def test_short_only_account_also_renders(self, _m):
        # Symmetry: an all-SELL (short-only) account must render too (the old pairing dropped these as well).
        from decimal import Decimal
        from django.utils import timezone
        from datetime import timedelta
        from trading.models import Trade
        acct2 = TradingAccount.objects.create(
            user=self.owner, name="Short Acct", account_number="RT-SHORT", broker_name="DemoBroker")
        base = timezone.now() - timedelta(hours=2)
        Trade.objects.create(account=acct2, ticket="S1", symbol="EURUSD", side="SELL", volume=Decimal("0.01"),
                             open_time=base, open_price=Decimal("1.10"), close_time=base + timedelta(minutes=3),
                             close_price=Decimal("1.09"), profit=Decimal("5.00"))
        c = APIClient(); c.force_authenticate(user=self.owner)
        r = c.get(reverse("trade-history"), {"account_id": acct2.id, "mode": "roundtrip", "stage": "ALL"}, secure=True)
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["trades"][0]["direction"], "SELL")


class HostedBalanceGroundingTests(APITestCase):
    """DEFECT A — the balance trajectory must ground on the hosted account's REAL broker balance (resolved via
    AccountProvisioning, since hosted accounts have no mt5_instance), NOT the synthetic 10,000 fallback."""

    @classmethod
    def setUpTestData(cls):
        from decimal import Decimal
        from django.utils import timezone
        from datetime import timedelta
        from trading.models import Trade
        from terminal_provisioning.models import AccountProvisioning
        cls.user = User.objects.create_user(username="hb", email="hb@x.invalid", password="x")
        cls.acct = TradingAccount.objects.create(user=cls.user, name="Hosted Workspace",
                                                 account_number="1302575", broker_name="Hosted Workspace")
        AccountProvisioning.objects.create(trading_account=cls.acct, windows_username="guvfx_u_28",
                                           runtime_root=r"C:\GuvFX\accounts\28",
                                           status=AccountProvisioning.Status.PROVISIONED)
        base = timezone.now() - timedelta(hours=3)
        for i, pnl in enumerate(["2.00", "-3.00", "5.00"]):
            Trade.objects.create(account=cls.acct, ticket=f"H{i}", symbol="XAUUSD", side="BUY",
                                 volume=Decimal("0.01"), open_time=base + timedelta(minutes=i),
                                 close_time=base + timedelta(minutes=i + 5), open_price=Decimal("4431"),
                                 close_price=Decimal("4432"), profit=Decimal(pnl))

    def _get(self, balance):
        with patch(_MOCK, return_value={"balance": balance, "equity": balance, "currency": "USD"}):
            c = APIClient(); c.force_authenticate(user=self.user)
            return c.get(reverse("trade-history"),
                         {"account": self.acct.id, "mode": "roundtrip", "stage": "ALL"}, secure=True)

    def test_50k_account_grounds_at_50k_not_10k(self):
        r = self._get(50000.0)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["opening_balance_source"], "last_used")     # real MT5, not fallback_10000
        self.assertNotEqual(r.data["opening_balance_source"], "fallback_10000")
        # opening = current_balance - total_pnl (2-3+5 = 4) -> ~49996; every chart point sits near 50k
        self.assertAlmostEqual(r.data["opening_balance_used"], 50000.0 - 4.0, places=1)
        for p in r.data["balance_series"]:
            self.assertGreater(p["balance_after_trade"], 49000)             # visually ~50k, never ~10k

    def test_5k_account_grounds_at_5k(self):
        r = self._get(5000.0)
        self.assertEqual(r.data["opening_balance_source"], "last_used")
        for p in r.data["balance_series"]:
            self.assertLess(p["balance_after_trade"], 6000)
            self.assertGreater(p["balance_after_trade"], 4000)              # ~5k, not 10k


class StrategyMetricsAccountDiscoveryTests(APITestCase):
    """DEFECT B — an ASSIGNED strategy (Wayond WIM) is shown even when its trades aren't comment-attributable;
    ownership is enforced; the response carries the broker account number (frontend never needs the DB PK)."""

    @classmethod
    def setUpTestData(cls):
        from decimal import Decimal
        from django.utils import timezone
        from datetime import timedelta
        from trading.models import Trade
        from strategies.models import Strategy, StrategyAssignment
        cls.owner = User.objects.create_user(username="sm", email="sm@x.invalid", password="x")
        cls.other = User.objects.create_user(username="smo", email="smo@x.invalid", password="x")
        cls.acct = TradingAccount.objects.create(user=cls.owner, name="Hosted Workspace",
                                                 account_number="1302575", broker_name="Hosted Workspace")
        strat = Strategy.objects.create(owner=cls.owner, name="Wayond WIM Strategy")
        StrategyAssignment.objects.create(strategy=strat, account=cls.acct, is_active=True,
                                          signal_source="ti_signals",
                                          execution_mode=StrategyAssignment.ExecutionMode.AUTO_DEMO,
                                          stage=StrategyAssignment.STAGE_LIVE)
        base = timezone.now() - timedelta(hours=2)
        # Wayond trades: WAY### comments carry no guvfx:sid tag -> comment attribution = "Unattributed".
        for i, pnl in enumerate(["2.12", "3.93"]):
            Trade.objects.create(account=cls.acct, ticket=f"W{i}", symbol="XAUUSD", side="BUY",
                                 volume=Decimal("0.01"), open_time=base + timedelta(minutes=i),
                                 close_time=base + timedelta(minutes=i + 3), open_price=Decimal("4431"),
                                 close_price=Decimal("4433"), profit=Decimal(pnl), comment=f"WAY266L{i+1}")

    def _get(self, user, account_id):
        c = APIClient(); c.force_authenticate(user=user)
        return c.get(reverse("strategy-metrics"), {"account": account_id}, secure=True)

    def test_assigned_wayond_wim_is_shown_with_empty_state(self):
        r = self._get(self.owner, self.acct.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["account_number"], "1302575")             # broker number, for the customer UI
        by_name = {s["strategy_name"]: s for s in r.data["strategies"]}
        self.assertIn("Wayond WIM Strategy", by_name)                      # the assigned strategy APPEARS
        wim = by_name["Wayond WIM Strategy"]
        self.assertTrue(wim["assigned"])
        self.assertFalse(wim["has_attributed_trades"])                    # no fabricated attribution
        self.assertEqual(wim["trades"], 0)
        # the trades themselves are honestly still under Unattributed (not fabricated onto Wayond)
        self.assertIn("Unattributed", by_name)
        self.assertEqual(by_name["Unattributed"]["trades"], 2)

    def test_foreign_account_is_404(self):
        r = self._get(self.other, self.acct.id)                           # not owner
        self.assertEqual(r.status_code, 404)

    def test_assigned_strategies_sorted_first(self):
        r = self._get(self.owner, self.acct.id)
        self.assertTrue(r.data["strategies"][0]["assigned"])              # customer's live strategy up top
