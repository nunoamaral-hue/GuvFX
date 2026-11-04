"""GFX-DASHBOARD-DATA-INTEGRITY-001 — the durable equity-snapshot fallback for the portfolio summary.

Root cause under test: ``build_summary`` took per-account balance/equity ONLY from a live bridge read; when that
read was unavailable estate-wide the whole USD aggregate collapsed to UNAVAILABLE even though the durable,
identity-verified equity ledger held FRESH observations. The fix lets each account fall back to ITS OWN latest
fresh ``AccountEquitySnapshot`` so an independently-fresh account stays INCLUDED (truthful PARTIAL), with honest
stale/missing reasons, no fabricated zero, and no cross-tenant read. Observation-only; no bridge contact (fakes).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from analytics import portfolio as PF
from analytics import portfolio_fx as FX
from analytics.models import AccountEquitySnapshot
from analytics.tests_portfolio import _acct, _user
from trading.models import Trade

_DOWN = lambda a, timeout=None: None            # a bridge that cannot be read
_NO_POS = lambda a, timeout=None: []            # no open positions


def _snap(account, *, equity, balance=None, currency="USD", age_s=30):
    return AccountEquitySnapshot.objects.create(
        trading_account=account, observed_at=timezone.now() - timedelta(seconds=age_s),
        account_currency=currency, balance=(balance if balance is not None else equity), equity=equity)


def _summary(user, *, fetch_balance, rate_source=None):
    return PF.build_summary(user, "ALL", fetch_balance=fetch_balance, fetch_positions=_NO_POS,
                            rate_source=rate_source or FX.NullFxRateSource(), stage="ALL")[0]


class SnapshotFallbackRecoveryTests(TestCase):
    def test_down_bridge_recovers_from_fresh_snapshot(self):
        # delayed/unreachable bridge recovers: a fresh durable snapshot keeps the account INCLUDED, not stale.
        u = _user("recov")
        a = _acct(u)
        _snap(a, equity=516, balance=500, currency="USD", age_s=45)
        p = _summary(u, fetch_balance=_DOWN)
        eq = p["aggregate"]["equity_usd"]
        self.assertEqual(eq["state"], "OK")
        self.assertEqual(eq["included_count"], 1)
        self.assertEqual(eq["total_usd"], 516.0)
        acct = p["accounts"][0]
        self.assertFalse(acct["stale"])
        self.assertEqual(acct["equity_native"], 516.0)
        self.assertEqual(acct["equity_source"], "snapshot")
        self.assertIsNotNone(acct["observed_as_of"])

    def test_live_read_preferred_over_snapshot(self):
        # No regression: when the live read succeeds, the FRESHEST (live) value wins; the snapshot is not consulted.
        u = _user("live1")
        a = _acct(u)
        _snap(a, equity=100, currency="USD", age_s=10)   # stale-but-fresh snapshot with a DIFFERENT value
        p = _summary(u, fetch_balance=lambda x, timeout=None: {"balance": 999, "equity": 999, "currency": "USD"})
        acct = p["accounts"][0]
        self.assertEqual(acct["equity_native"], 999)
        self.assertEqual(acct["equity_source"], "live")
        self.assertIsNone(acct["observed_as_of"])


class PartialTruthfulAggregationTests(TestCase):
    def test_one_stale_account_does_not_exclude_healthy(self):
        # The core invariant: one unreadable account must NOT erase a healthy account's equity from the aggregate.
        u = _user("mix")
        healthy = _acct(u, number="90811001")
        broken = _acct(u, number="90811002")   # live down AND no snapshot
        _snap(healthy, equity=200, currency="USD", age_s=30)
        p = _summary(u, fetch_balance=_DOWN)
        eq = p["aggregate"]["equity_usd"]
        self.assertEqual(eq["state"], "PARTIAL")
        self.assertEqual(eq["included_count"], 1)
        self.assertEqual(eq["excluded_count"], 1)
        self.assertEqual(eq["total_usd"], 200.0)   # measured over the INCLUDED subset, never contaminated by the excluded
        excl = {x["account_id"]: x["category"] for x in eq["unconverted"]}
        self.assertEqual(excl[broken.id], "missing_snapshot")

    def test_all_unreadable_is_unavailable_never_zero(self):
        # When NOTHING is fresh, the aggregate is UNAVAILABLE with a null total — never a misleading measured $0.
        u = _user("none")
        _acct(u, number="90812001")
        _acct(u, number="90812002")
        p = _summary(u, fetch_balance=_DOWN)
        eq = p["aggregate"]["equity_usd"]
        self.assertEqual(eq["state"], "UNAVAILABLE")
        self.assertIsNone(eq["total_usd"])
        self.assertEqual(eq["included_count"], 0)
        self.assertEqual(eq["excluded_count"], 2)

    def test_stale_snapshot_is_not_resurrected(self):
        # A snapshot older than the freshness window is NOT used (honest stale), while a fresh one IS included.
        u = _user("stale")
        fresh = _acct(u, number="90813001")
        old = _acct(u, number="90813002")
        _snap(fresh, equity=300, currency="USD", age_s=60)
        _snap(old, equity=777, currency="USD", age_s=PF._SUMMARY_SNAPSHOT_FRESHNESS_S + 120)  # beyond 15min
        p = _summary(u, fetch_balance=_DOWN)
        eq = p["aggregate"]["equity_usd"]
        self.assertEqual(eq["state"], "PARTIAL")
        self.assertEqual(eq["included_count"], 1)
        self.assertEqual(eq["total_usd"], 300.0)
        excl = {x["account_id"]: x["category"] for x in eq["unconverted"]}
        self.assertEqual(excl[old.id], "missing_snapshot")  # live None + stale snapshot => amount_unavailable


class TenantIsolationTests(TestCase):
    def test_fallback_reads_only_the_accounts_own_snapshot(self):
        # The fallback must never source another account's row. Account with NO own snapshot => None even when a
        # different account has a fresh one.
        u = _user("iso")
        mine = _acct(u, number="90814001")
        other = _acct(u, number="90814002")
        _snap(other, equity=12345, currency="USD", age_s=20)
        self.assertIsNone(PF._equity_snapshot_fallback(mine, now=timezone.now()))
        got = PF._equity_snapshot_fallback(other, now=timezone.now())
        self.assertEqual(got["equity"], 12345.0)


class CurrencyHonestyTests(TestCase):
    def test_gbp_snapshot_converts_via_rate_source(self):
        # A GBP snapshot must convert through the injected rate source — never be silently summed as USD.
        u = _user("gbp")
        a = _acct(u, broker="Pepperstone", currency="GBP")
        _snap(a, equity=500, currency="GBP", age_s=30)
        rs = FX.MappingFxRateSource({("GBP", "USD"): 1.3416})
        p = _summary(u, fetch_balance=_DOWN, rate_source=rs)
        eq = p["aggregate"]["equity_usd"]
        self.assertEqual(eq["state"], "OK")
        self.assertEqual(eq["total_usd"], 670.8)   # 500 * 1.3416, Decimal half-even
        self.assertEqual(p["accounts"][0]["currency"], "GBP")

    def test_gbp_snapshot_excluded_when_no_rate_never_silent_usd(self):
        # Same GBP snapshot with the USD-only Null source: EXCLUDED as currency_unconvertible, never counted as USD.
        u = _user("gbp2")
        a = _acct(u, broker="Pepperstone", currency="GBP")
        _snap(a, equity=500, currency="GBP", age_s=30)
        p = _summary(u, fetch_balance=_DOWN, rate_source=FX.NullFxRateSource())
        eq = p["aggregate"]["equity_usd"]
        self.assertEqual(eq["state"], "UNAVAILABLE")
        self.assertIsNone(eq["total_usd"])
        self.assertEqual(eq["unconverted"][0]["category"], "currency_unconvertible")


class PnlReconciliationTests(TestCase):
    def test_profit_commission_swap_composition(self):
        # Ticket 55371350 reconciliation: realized net = profit + commission + swap = 50.31 + 0.00 + (-2.29) = 48.02.
        u = _user("pnl")
        a = _acct(u, currency="USD")
        Trade.objects.create(account=a, ticket="55371350", symbol="XAUUSD_SB", side="BUY", volume=1,
                             open_time=timezone.now() - timedelta(days=2), close_time=timezone.now(),
                             open_price=Decimal("3964.98"), close_price=Decimal("4015.29"),
                             profit=Decimal("50.31"), commission=Decimal("0.00"), swap=Decimal("-2.29"),
                             source_stage="UNKNOWN")
        val, basis = PF.daily_realized_pnl([a], stage="ALL", observed_nonusd_ids=set(),
                                           rate_source=FX.NullFxRateSource(), observed_ccy={a.id: "USD"})
        self.assertEqual(basis, "USD")
        self.assertEqual(round(float(val), 2), 48.02)


class CurrencyFailClosedTests(TestCase):
    def test_blank_currency_snapshot_excluded_not_summed_as_usd(self):
        # A fresh snapshot whose currency the bridge did not report is UNKNOWN, not USD: it must be EXCLUDED as
        # currency_unconvertible (fail closed), never silently summed into the USD total.
        u = _user("blank")
        a = _acct(u)  # account_currency unpopulated
        _snap(a, equity=100, balance=100, currency="", age_s=30)   # blank snapshot currency
        p = _summary(u, fetch_balance=_DOWN)
        eq = p["aggregate"]["equity_usd"]
        self.assertEqual(eq["state"], "UNAVAILABLE")
        self.assertIsNone(eq["total_usd"])   # NOT 100.0
        self.assertEqual(eq["unconverted"][0]["category"], "currency_unconvertible")


class ValueEmptyLiveRecoveryTests(TestCase):
    def test_value_empty_live_dict_recovers_from_snapshot(self):
        # A live response that is a dict but carries null balance AND equity is as useless as None → fall back to
        # the fresh durable snapshot rather than excluding a recoverable account.
        u = _user("empty")
        a = _acct(u)
        _snap(a, equity=300, currency="USD", age_s=40)
        p = _summary(u, fetch_balance=lambda x, timeout=None: {"balance": None, "equity": None, "currency": "USD"})
        eq = p["aggregate"]["equity_usd"]
        self.assertEqual(eq["state"], "OK")
        self.assertEqual(eq["total_usd"], 300.0)
        acct = p["accounts"][0]
        self.assertEqual(acct["equity_source"], "snapshot")
        self.assertFalse(acct["stale"])


class FreshnessCouplingTests(TestCase):
    def test_summary_freshness_matches_ledger_window(self):
        # The summary fallback window must track the equity-ledger staleness window; a silent divergence would
        # include snapshots the ledger considers stale (or vice-versa).
        from analytics import equity_snapshots as EQ
        self.assertEqual(PF._SUMMARY_SNAPSHOT_FRESHNESS_S, EQ.DEFAULT_FRESHNESS_S)


class ObservationOnlyTests(TestCase):
    def test_summary_with_fallback_places_no_order(self):
        # Zero-order assertion: building the summary (incl. the snapshot fallback) creates no execution job.
        u = _user("noord")
        a = _acct(u)
        _snap(a, equity=100, currency="USD", age_s=20)
        try:
            from execution.models import ExecutionJob
        except Exception:  # noqa: BLE001
            ExecutionJob = None
        before = ExecutionJob.objects.count() if ExecutionJob else 0
        _summary(u, fetch_balance=_DOWN)
        after = ExecutionJob.objects.count() if ExecutionJob else 0
        self.assertEqual(before, after)
