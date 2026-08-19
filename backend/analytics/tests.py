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
from django.test import TestCase
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


# ---------------------------------------------------------------------------------------------------------
# P1 — CASH-FLOW-AWARE BALANCE BASELINE
#
# The balance chart must ground on the account's real FUNDING and never treat a deposit as trading profit or
# a withdrawal as a trading loss. These exercise ``_compute_balance_series`` directly across the funding
# matrix, then the view for identity/isolation/fail-closed. Uses aware datetimes so events order in time.
# ---------------------------------------------------------------------------------------------------------
from datetime import datetime, timezone as _tz  # noqa: E402
from analytics.views_trade_history import _compute_balance_series  # noqa: E402


def _dt(day, minute=0):
    return datetime(2026, 7, day, 12, minute, tzinfo=_tz.utc)


def _rt(close_day, pnl, minute=0, open_day=None):
    row = {"close_time": _dt(close_day, minute), "net_pnl_money": float(pnl)}
    if open_day is not None:
        row["open_time"] = _dt(open_day)
    return row


def _op(day, amount, minute=0):
    return {"time": _dt(day, minute), "amount": float(amount)}


class CashFlowAwareBalanceSeriesTests(TestCase):
    def _series(self, rts, balance, ops=None, credit=0.0):
        # _build_round_trips yields DESC; the function re-sorts, so order in is irrelevant.
        return _compute_balance_series(list(reversed(rts)), mt5_balance_current=balance,
                                       balance_ops=ops, credit=credit)

    # S2 / S3 — initial deposit + trades: opening is the real deposit, from the ledger (not reverse-derived).
    def test_s2_50k_plus_profit_grounds_on_real_deposit(self):
        rts = [_rt(2, 100), _rt(3, 200)]  # +300 total
        series, stats, opening, source = self._series(rts, 50300.0, ops=[_op(1, 50000)])
        self.assertEqual(source, "reconciled_ledger")
        self.assertEqual(opening, 50000.0)                       # the deposit, not 50300-300 coincidence
        self.assertEqual(stats["net_pnl_total"], 300.0)
        self.assertAlmostEqual(series[-1]["balance_after_trade"], 50300.0, places=2)

    def test_s3_50k_plus_loss(self):
        series, stats, opening, source = self._series([_rt(2, -100), _rt(3, -50)], 49850.0, ops=[_op(1, 50000)])
        self.assertEqual(opening, 50000.0)
        self.assertEqual(source, "reconciled_ledger")
        self.assertAlmostEqual(series[-1]["balance_after_trade"], 49850.0, places=2)

    # S4 — a REAL $10,000 account must be distinguishable from the synthetic 10,000 fallback.
    def test_s4_real_10k_is_reconciled_not_fallback(self):
        series, stats, opening, source = self._series([_rt(2, 25)], 10025.0, ops=[_op(1, 10000)])
        self.assertEqual(opening, 10000.0)
        self.assertEqual(source, "reconciled_ledger")           # NOT "fallback_10000"

    # S5 — a mid-period deposit is a STEP at its real time, never trading profit.
    def test_s5_mid_period_deposit_is_a_step_not_profit(self):
        rts = [_rt(2, 100), _rt(4, 200)]
        ops = [_op(1, 10000), _op(3, 5000)]                     # 10k before trading, 5k mid-period
        series, stats, opening, source = self._series(rts, 15300.0, ops=ops)
        self.assertEqual(opening, 10000.0)                       # opening is only the pre-trade funding
        funding = [p for p in series if p.get("funding_amount") is not None]
        self.assertEqual(len(funding), 1)
        self.assertEqual(funding[0]["funding_amount"], 5000.0)
        self.assertEqual(funding[0]["net_pnl_money"], 0.0)       # NOT counted as +5000 P&L
        self.assertEqual(stats["net_pnl_total"], 300.0)          # trading P&L excludes the deposit
        self.assertEqual(stats["wins"], 2)                       # the deposit is not a "win"
        self.assertAlmostEqual(series[-1]["balance_after_trade"], 15300.0, places=2)

    # S6 — a mid-period withdrawal is a negative STEP, never a trading loss.
    def test_s6_mid_period_withdrawal_is_not_a_loss(self):
        rts = [_rt(2, 100), _rt(4, -50)]
        ops = [_op(1, 10000), _op(3, -2000)]
        series, stats, opening, source = self._series(rts, 8050.0, ops=ops)
        self.assertEqual(stats["losses"], 1)                     # only the -50 trade, NOT the withdrawal
        self.assertEqual(stats["net_pnl_total"], 50.0)
        funding = [p for p in series if p.get("funding_amount") is not None]
        self.assertEqual(funding[0]["funding_amount"], -2000.0)
        self.assertEqual(funding[0]["net_pnl_money"], 0.0)
        self.assertAlmostEqual(series[-1]["balance_after_trade"], 8050.0, places=2)

    # S7 / S8 — deposit + withdrawal + trading result: funding and trading stay separate.
    def test_s7_deposit_withdrawal_plus_profit(self):
        rts = [_rt(2, 300)]
        ops = [_op(1, 10000), _op(3, 5000), _op(4, -1000)]      # net funding 14000
        series, stats, opening, source = self._series(rts, 14300.0, ops=ops)
        self.assertEqual(source, "reconciled_ledger")
        self.assertEqual(stats["net_pnl_total"], 300.0)
        self.assertAlmostEqual(series[-1]["balance_after_trade"], 14300.0, places=2)

    def test_s8_deposit_withdrawal_plus_loss(self):
        rts = [_rt(2, -400)]
        ops = [_op(1, 10000), _op(3, 2000), _op(4, -500)]       # net funding 11500
        series, stats, opening, source = self._series(rts, 11100.0, ops=ops)
        self.assertEqual(stats["net_pnl_total"], -400.0)
        self.assertAlmostEqual(series[-1]["balance_after_trade"], 11100.0, places=2)

    # S9 — broker credit forces the fail-closed reconstruction (credit is neither funding nor P&L).
    def test_s9_credit_present_forces_fail_closed(self):
        series, stats, opening, source = self._series([_rt(2, 100)], 10600.0, ops=[_op(1, 10000)], credit=500.0)
        self.assertEqual(source, "last_used")                    # NOT reconciled_ledger
        self.assertEqual(opening, 10600.0 - 100.0)               # previous reconstruction preserved

    # S10 — open positions (balance != equity): reconciliation uses BALANCE (closed P&L), so it still holds.
    def test_s10_open_positions_reconcile_on_balance(self):
        # balance reflects only closed trades; equity (not passed here) would differ — irrelevant to funding.
        series, stats, opening, source = self._series([_rt(2, 20)], 50020.0, ops=[_op(1, 50000)])
        self.assertEqual(source, "reconciled_ledger")
        self.assertEqual(opening, 50000.0)

    # Adversarial regression (Finding 1) — a deposit that lands WHILE a position is open (open < deposit <
    # close) must be a dated STEP, not folded into the opening baseline. The boundary is the trade OPEN time.
    def test_deposit_during_open_position_is_a_step_not_baseline(self):
        # single position: opened day1, closed day10; a $5,000 deposit on day5 (mid-journey).
        rts = [_rt(10, 100.0, open_day=1)]
        ops = [_op(1, 10000), _op(5, 5000)]                    # 10k before trading, 5k while position open
        series, stats, opening, source = self._series(rts, 15100.0, ops=ops)
        self.assertEqual(source, "reconciled_ledger")
        self.assertEqual(opening, 10000.0)                      # starts at 10k, NOT 15k
        funding = [p for p in series if p.get("funding_amount") is not None]
        self.assertEqual(len(funding), 1)                       # the day-5 deposit IS a step
        self.assertEqual(funding[0]["funding_amount"], 5000.0)
        self.assertEqual(funding[0]["net_pnl_money"], 0.0)

    # Adversarial regression (Findings 2/3) — the tolerance must NOT scale with balance: a real multi-dollar
    # gap on a large account must fail closed, not be mislabelled reconciled.
    def test_large_account_real_gap_fails_closed(self):
        # $1,000,000 balance, but $20,000 of true funding is out-of-window -> 45-dollar-class gap here.
        series, stats, opening, source = self._series([_rt(2, 99955.0)], 1_000_000.0, ops=[_op(1, 900000.0)])
        self.assertEqual(source, "last_used")                   # was wrongly "reconciled_ledger" under bal*5e-5
        self.assertIsNone([p for p in series if p.get("funding_amount")] or None)

    # S14 — incomplete funding history (funding older than the snapshot window) must NOT be fabricated.
    def test_s14_non_reconciling_fails_closed(self):
        # only 30k of the true 50k funding is in-window -> identity breaks -> fall back, never invent.
        series, stats, opening, source = self._series([_rt(2, -5)], 49995.0, ops=[_op(1, 30000)])
        self.assertEqual(source, "last_used")
        self.assertAlmostEqual(opening, 49995.0 - (-5.0), places=2)

    # Beta parity: single pre-trade deposit -> the trajectory is BYTE-IDENTICAL with/without the ledger.
    def test_beta_single_deposit_trajectory_is_byte_identical(self):
        rts = [_rt(2, 2.0), _rt(3, -3.0), _rt(4, 5.0)]          # +4 total; balance 50004
        with_ops = self._series(rts, 50004.0, ops=[_op(1, 50000)])
        without = self._series(rts, 50004.0, ops=None)
        self.assertEqual([p["balance_after_trade"] for p in with_ops[0]],
                         [p["balance_after_trade"] for p in without[0]])
        self.assertEqual(with_ops[2], without[2])                # same opening value (50000)
        self.assertEqual(with_ops[0][-1]["balance_after_trade"], 50004.0)

    # No cash-flow data at all -> exact previous behaviour (last_used from current balance).
    def test_no_ops_is_previous_behaviour(self):
        series, stats, opening, source = self._series([_rt(2, 10)], 5010.0, ops=None)
        self.assertEqual(source, "last_used")
        self.assertEqual(opening, 5000.0)


class BalanceOpsFetchTests(TestCase):
    """`_fetch_mt5_balance_ops` — dedup a repeated deal ticket (Finding 2), and fail closed on a non-dict JSON
    body (Finding LOW). Isolation/transport are mocked; the parsing/dedup logic is what's under test."""
    import json as _json

    def _run(self, body_obj):
        import analytics.views_trade_history as V
        from types import SimpleNamespace
        acct = SimpleNamespace(id=1, account_number="1302575")
        body = self._json.dumps(body_obj)

        class _Resp:
            def __enter__(self_):
                return self_
            def __exit__(self_, *a):
                return False
            def read(self_):
                return body.encode()

        ok = SimpleNamespace(ok=True, base_url="http://tenant", reason_code="")
        idok = SimpleNamespace(ok=True, reason_code="")
        with patch.object(V, "_get_windows_agent_config", return_value=("http://g", "tok")), \
             patch("execution.snapshot_transport.resolve_account_snapshot_base", return_value=ok), \
             patch("execution.snapshot_transport.verify_snapshot_identity", return_value=idok), \
             patch("urllib.request.urlopen", return_value=_Resp()):
            return V._fetch_mt5_balance_ops(acct, "guvfx_u_28")

    def test_duplicate_deal_ticket_is_counted_once(self):
        dup = {"type": 2, "profit": 5000.0, "time": 1753783360, "ticket": 154997}
        res = self._run({"account_login": "1302575", "account_server": "IS6", "deals": [dup, dict(dup)]})
        self.assertIsNotNone(res)
        self.assertEqual(len(res["balance_ops"]), 1)               # de-duplicated by ticket, not doubled
        self.assertEqual(res["balance_ops"][0]["amount"], 5000.0)

    def test_non_dict_json_body_fails_closed(self):
        self.assertIsNone(self._run([1, 2, 3]))                    # top-level array -> None (no AttributeError)

    def test_credit_deal_captured_separately_not_as_funding(self):
        res = self._run({"account_login": "1302575", "account_server": "IS6", "deals": [
            {"type": 2, "profit": 10000.0, "time": 1753783360, "ticket": 1},
            {"type": 3, "profit": 250.0, "time": 1753783361, "ticket": 2},   # credit
        ]})
        self.assertEqual(len(res["balance_ops"]), 1)               # credit is NOT a funding op
        self.assertEqual(res["credit"], 250.0)


class CashFlowViewTests(APITestCase):
    """View-level: net_funding/trading_pnl are surfaced, isolation holds, and the page fails closed when the
    per-tenant snapshot (balance and/or cash flows) is unavailable."""
    _BAL = "analytics.views_trade_history._fetch_mt5_account_balance"
    _OPS = "analytics.views_trade_history._fetch_mt5_balance_ops"

    @classmethod
    def setUpTestData(cls):
        from decimal import Decimal
        from trading.models import Trade
        from terminal_provisioning.models import AccountProvisioning
        cls.owner = User.objects.create_user(username="cf", email="cf@x.invalid", password="x")
        cls.other = User.objects.create_user(username="cfo", email="cfo@x.invalid", password="x")
        cls.acct = TradingAccount.objects.create(user=cls.owner, name="Hosted Workspace",
                                                 account_number="1302575", broker_name="Hosted Workspace")
        AccountProvisioning.objects.create(trading_account=cls.acct, windows_username="guvfx_u_28",
                                           runtime_root=r"C:\GuvFX\accounts\28",
                                           status=AccountProvisioning.Status.PROVISIONED)
        base = _dt(2)
        for i, pnl in enumerate(["2.00", "-3.00", "5.00"]):     # +4 total
            Trade.objects.create(account=cls.acct, ticket=f"C{i}", symbol="XAUUSD", side="BUY",
                                 volume=Decimal("0.01"), open_time=base, close_time=_dt(2 + i, i + 1),
                                 open_price=Decimal("4431"), close_price=Decimal("4432"), profit=Decimal(pnl))

    def _get(self, user, balance, ops):
        with patch(self._BAL, return_value=({"balance": balance, "equity": balance, "currency": "USD"}
                                            if balance is not None else None)), \
             patch(self._OPS, return_value=ops):
            c = APIClient(); c.force_authenticate(user=user)
            return c.get(reverse("trade-history"),
                         {"account": self.acct.id, "mode": "roundtrip", "stage": "ALL"}, secure=True)

    def test_reconciled_surfaces_net_funding_and_trading_pnl(self):
        r = self._get(self.owner, 50004.0, {"balance_ops": [_op(1, 50000)], "credit": 0.0})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["opening_balance_source"], "reconciled_ledger")
        self.assertEqual(r.data["net_funding"], 50000.0)
        self.assertEqual(r.data["trading_pnl"], 4.0)             # deposits excluded from trading P&L
        self.assertEqual(r.data["credit"], 0.0)

    def test_s11_no_snapshot_fails_closed(self):
        r = self._get(self.owner, None, None)                    # neither balance nor ops available
        self.assertEqual(r.status_code, 200)                     # page still renders (honest, no crash)
        self.assertIsNone(r.data["net_funding"])
        self.assertEqual(r.data["opening_balance_source"], "fallback_10000")

    def test_s12_ops_unavailable_uses_previous_reconstruction(self):
        r = self._get(self.owner, 50004.0, None)                 # balance ok, cash flows unavailable (firewall/none)
        self.assertEqual(r.data["opening_balance_source"], "last_used")
        self.assertIsNone(r.data["net_funding"])
        self.assertEqual(r.data["opening_balance_used"], 50000.0)

    def test_s13_foreign_account_is_404_and_leaks_nothing(self):
        r = self._get(self.other, 50004.0, {"balance_ops": [_op(1, 50000)], "credit": 0.0})
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("net_funding", r.data)
