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
