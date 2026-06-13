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
