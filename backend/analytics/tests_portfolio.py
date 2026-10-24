"""Portfolio dashboard tests — FX normalisation, USD aggregation, portfolio metrics (from the trade set, not
averaged), scope/ownership (IDOR), and multi-account open-trades attribution. Observation-only; no bridge contact
(live fetchers are injected fakes)."""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from analytics import portfolio as PF
from analytics import portfolio_fx as FX
from trading.models import BrokerServer, Trade, TradingAccount

U = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return f"90{_n:05d}"


def _user(name):
    return U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")


def _acct(user, broker="Pepperstone", number=None, active=True, removed=False, currency=None):
    number = number or _uniq()
    srv, _ = BrokerServer.objects.get_or_create(server_name=f"{broker}-Demo-{number}")
    a = TradingAccount.objects.create(user=user, name=broker, broker_name=broker, account_number=number,
                                      is_demo=True, is_active=active, broker_server=srv)
    if currency:
        a.account_currency = currency
    if removed:
        a.disconnected_at = timezone.now()
        a.is_active = False
    a.save()
    return a


def _closed_trade(account, net, when=None, stage="UNKNOWN", profit_currency=None,
                  commission_currency=None, swap_currency=None):
    # Default stage UNKNOWN mirrors production reality: broker-ingested trades carry the default source_stage
    # (the comment tag is "WAY.../[tp .]", not "LIVE"). The analytics must count these (P0 2026-09-28).
    when = when or timezone.now()
    return Trade.objects.create(
        account=account, ticket=_uniq(), symbol="XAUUSD", side="BUY", volume=1,
        open_time=when - timedelta(hours=1), close_time=when, open_price=1, close_price=2,
        profit=net, commission=0, swap=0, source_stage=stage,
        profit_currency=profit_currency, commission_currency=commission_currency, swap_currency=swap_currency)


# ── FX layer ────────────────────────────────────────────────────────────────────────────────────────────────
class FxTests(TestCase):
    def test_usd_to_usd_is_identity(self):
        r = FX.to_usd(100.0, "USD", FX.NullFxRateSource())
        self.assertTrue(r["ok"]); self.assertEqual(r["usd"], 100.0); self.assertEqual(r["rate"], 1.0)

    def test_blank_currency_treated_as_usd(self):
        self.assertTrue(FX.to_usd(50.0, None, FX.NullFxRateSource())["ok"])

    def test_gbp_to_usd_direct(self):
        src = FX.MappingFxRateSource({("GBP", "USD"): 1.25})
        r = FX.to_usd(100.0, "GBP", src)
        self.assertTrue(r["ok"]); self.assertEqual(r["usd"], 125.0)

    def test_eur_to_usd_direct(self):
        src = FX.MappingFxRateSource({("EUR", "USD"): 1.10})
        self.assertAlmostEqual(FX.to_usd(200.0, "EUR", src)["usd"], 220.0, places=6)

    def test_jpy_to_usd_inverse(self):
        # Only USDJPY held (~150 JPY per USD) -> JPY->USD must invert.
        src = FX.MappingFxRateSource({("USD", "JPY"): 150.0})
        r = FX.to_usd(15000.0, "JPY", src)
        self.assertTrue(r["ok"]); self.assertAlmostEqual(r["usd"], 100.0, places=6)

    def test_missing_rate_not_converted(self):
        r = FX.to_usd(100.0, "GBP", FX.NullFxRateSource())
        self.assertFalse(r["ok"]); self.assertIsNone(r["usd"]); self.assertEqual(r["reason"], "rate_unavailable")

    def test_zero_and_negative_rate_rejected(self):
        for bad in (0.0, -1.0):
            r = FX.to_usd(100.0, "GBP", FX.MappingFxRateSource({("GBP", "USD"): bad}))
            self.assertFalse(r["ok"])

    def test_never_uses_rate_one_for_non_usd(self):
        r = FX.to_usd(100.0, "GBP", FX.NullFxRateSource())
        self.assertFalse(r["ok"])                                   # would be 100.0 if it wrongly used rate=1

    def test_aggregate_all_usd(self):
        agg = FX.aggregate_usd([{"account_id": 1, "amount": 10, "currency": "USD"},
                                {"account_id": 2, "amount": 20, "currency": "USD"}], FX.NullFxRateSource())
        self.assertEqual(agg.total_usd, 30); self.assertEqual(agg.basis, "USD"); self.assertEqual(agg.unconverted, [])

    def test_aggregate_mixed_is_partial_and_excludes_unconverted(self):
        agg = FX.aggregate_usd([{"account_id": 1, "amount": 10, "currency": "USD"},
                                {"account_id": 2, "amount": 20, "currency": "GBP"}], FX.NullFxRateSource())
        self.assertEqual(agg.total_usd, 10)                          # GBP excluded, never summed as if USD
        self.assertEqual(agg.basis, "PARTIAL")
        self.assertEqual(agg.unconverted[0]["account_id"], 2)


# ── Portfolio metrics (from the trade set, not averaged) ──────────────────────────────────────────────────────
class MetricsTests(TestCase):
    def test_win_rate_from_trades_not_averaged(self):
        u = _user("m1")
        a1, a2 = _acct(u), _acct(u)
        # a1: 3 wins, 1 loss (75%); a2: 0 wins, 2 loss (0%). Averaged=37.5%; true portfolio=3/6=50%.
        for _ in range(3):
            _closed_trade(a1, 10)
        _closed_trade(a1, -10)
        _closed_trade(a2, -5); _closed_trade(a2, -5)
        m = PF.portfolio_metrics([a1, a2])
        self.assertEqual(m["total_trades"], 6)
        self.assertEqual(m["wins"], 3)
        self.assertEqual(m["win_rate_pct"], 50.0)                    # NOT 37.5

    def test_profit_factor_from_sums(self):
        u = _user("m2"); a = _acct(u)
        _closed_trade(a, 30); _closed_trade(a, 30); _closed_trade(a, -20)
        m = PF.portfolio_metrics([a])
        self.assertEqual(m["gross_profit"], 60.0); self.assertEqual(m["gross_loss"], 20.0)
        self.assertEqual(m["profit_factor"], 3.0)                    # 60/20, not avg of ratios

    def test_profit_factor_infinite_when_no_losses(self):
        u = _user("m3"); a = _acct(u)
        _closed_trade(a, 10)
        m = PF.portfolio_metrics([a])
        self.assertIsNone(m["profit_factor"]); self.assertTrue(m["profit_factor_infinite"])

    def test_expectancy_and_breakeven(self):
        u = _user("m4"); a = _acct(u)
        _closed_trade(a, 10); _closed_trade(a, -4); _closed_trade(a, 0)
        m = PF.portfolio_metrics([a])
        self.assertEqual(m["breakeven"], 1); self.assertEqual(m["total_trades"], 3)
        self.assertEqual(m["expectancy"], round(6 / 3, 2))          # (10-4+0)/3 = 2.0

    def test_account_specific_matches_source(self):
        u = _user("m5"); a1 = _acct(u); a2 = _acct(u)
        _closed_trade(a1, 7); _closed_trade(a2, 100)
        self.assertEqual(PF.portfolio_metrics([a1])["net_pnl_total"], 7.0)

    def test_all_usd_basis_is_usd_no_exclusions(self):
        u = _user("m6"); a = _acct(u)
        _closed_trade(a, 10)
        m = PF.portfolio_metrics([a])
        self.assertEqual(m["basis"], "USD"); self.assertEqual(m["excluded_accounts"], [])


# ── Currency-honest realized path (H1): never sum mixed denominations ─────────────────────────────────────────
class RealizedCurrencyHonestyTests(TestCase):
    def test_non_usd_account_excluded_and_partial(self):
        u = _user("h1")
        usd = _acct(u, currency="USD"); gbp = _acct(u, currency="GBP")
        _closed_trade(usd, 10); _closed_trade(usd, 10)
        _closed_trade(gbp, 999)                                   # must NOT be summed with the USD account
        m = PF.portfolio_metrics([usd, gbp])
        self.assertEqual(m["basis"], "PARTIAL")
        self.assertEqual(m["excluded_accounts"], [gbp.id])
        self.assertEqual(m["net_pnl_total"], 20.0)               # only the USD account, never 1019
        self.assertEqual(m["total_trades"], 2)                   # GBP account's trades excluded from the sum

    def test_mixed_trade_currency_withholds_money_keeps_count(self):
        u = _user("h2"); a = _acct(u, currency="USD")
        # A trade whose commission is in a different currency than profit: _net would sum mixed denominations.
        _closed_trade(a, 10, profit_currency="USD", commission_currency="EUR")
        m = PF.portfolio_metrics([a])
        self.assertEqual(m["basis"], "PARTIAL")
        self.assertEqual(m["total_trades"], 1)                   # count is currency-agnostic
        self.assertIsNone(m["net_pnl_total"]); self.assertIsNone(m["win_rate_pct"])   # money/classified withheld

    def test_daily_realized_returns_value_and_basis(self):
        u = _user("h3"); a = _acct(u, currency="USD")
        _closed_trade(a, 5)
        val, basis = PF.daily_realized_pnl([a])
        self.assertEqual(val, 5.0); self.assertEqual(basis, "USD")

    def test_daily_realized_partial_when_non_usd(self):
        u = _user("h4")
        usd = _acct(u, currency="USD"); gbp = _acct(u, currency="GBP")
        _closed_trade(usd, 5); _closed_trade(gbp, 7)
        val, basis = PF.daily_realized_pnl([usd, gbp])
        self.assertEqual(val, 5.0); self.assertEqual(basis, "PARTIAL")   # gbp excluded, not summed

    def test_account_net_pnl_none_for_non_usd(self):
        u = _user("h5"); gbp = _acct(u, currency="GBP")
        _closed_trade(gbp, 50)
        self.assertIsNone(PF.account_net_pnl(gbp))               # never a mixed/unconverted figure


# ── source_stage must NOT hide real trades (P0 2026-09-28 regression) ─────────────────────────────────────────
class StageFilterRegressionTests(TestCase):
    """The dashboard filtered source_stage='LIVE' and reported $0 while real UNKNOWN-stage broker trades existed.
    Analytics must count all closed trades regardless of source_stage (a comment-tag classifier, not performance)."""

    def test_unknown_stage_trades_are_counted(self):
        u = _user("sf1"); a = _acct(u, currency="USD")
        # The exact Taurex incident: three closed XAUUSD trades, source_stage UNKNOWN.
        for p in (1.19, 3.07, 5.39):
            _closed_trade(a, p, stage="UNKNOWN")
        m = PF.portfolio_metrics([a])                                  # default stage = ALL
        self.assertEqual(m["total_trades"], 3)                          # NOT 0
        self.assertEqual(m["net_pnl_total"], 9.65)                      # matches broker: 1.19+3.07+5.39
        self.assertEqual(m["wins"], 3); self.assertEqual(m["win_rate_pct"], 100.0)
        val, basis = PF.daily_realized_pnl([a])
        self.assertEqual(val, 9.65); self.assertEqual(basis, "USD")
        self.assertEqual(PF.account_net_pnl(a), 9.65)

    def test_explicit_live_filter_still_narrows(self):
        # The stage param still works when a caller explicitly asks for exactly LIVE/TEST.
        u = _user("sf2"); a = _acct(u, currency="USD")
        _closed_trade(a, 10.0, stage="UNKNOWN"); _closed_trade(a, 5.0, stage="LIVE")
        self.assertEqual(PF.portfolio_metrics([a])["total_trades"], 2)                 # ALL default
        self.assertEqual(PF.portfolio_metrics([a], stage="LIVE")["total_trades"], 1)   # explicit LIVE

    def test_endpoint_counts_unknown_stage_trades(self):
        u = _user("sf3"); a = _acct(u, currency="USD")
        for p in (1.19, 3.07, 5.39):
            _closed_trade(a, p, stage="UNKNOWN")
        c = APIClient(); c.force_authenticate(u)
        d = c.get("/api/analytics/portfolio/summary/").json()
        self.assertEqual(d["aggregate"]["metrics"]["total_trades"], 3)
        self.assertEqual(d["aggregate"]["metrics"]["net_pnl_total"], 9.65)


# ── Observed-currency guard (H-CURRENCY) + drawdown determinism (adversarial review 2026-09-28) ───────────────
class ObservedCurrencyTests(TestCase):
    """account_currency is never populated in prod (NULL->USD), so the realized path must use the OBSERVED snapshot
    currency (as the balance path does) — else a non-USD account is silently summed as USD with basis='USD'."""

    def test_observed_nonusd_account_excluded_from_realized(self):
        u = _user("oc1")
        usd = _acct(u); eur = _acct(u)                     # both account_currency NULL (the inert-guard reality)
        _closed_trade(usd, 10.0); _closed_trade(eur, 999.0)
        bal = {usd.id: {"balance": 1000, "equity": 1000, "currency": "USD"},
               eur.id: {"balance": 800, "equity": 800, "currency": "EUR"}}   # observed EUR
        payload, _ = PF.build_summary(u, "ALL", fetch_balance=lambda a, timeout=None: bal.get(a.id),
                                      fetch_positions=_fake_positions({usd.id: [], eur.id: []}),
                                      rate_source=FX.NullFxRateSource())
        m = payload["aggregate"]["metrics"]
        self.assertEqual(m["basis"], "PARTIAL")            # NOT falsely 'USD'
        self.assertIn(eur.id, m["excluded_accounts"])      # EUR account excluded from the realized sum
        self.assertEqual(m["net_pnl_total"], 10.0)         # only the USD account, never 1009
        self.assertEqual(payload["aggregate"]["daily_realized_pnl_basis"], "PARTIAL")
        rows = {r["account_id"]: r for r in payload["accounts"]}
        self.assertIsNone(rows[eur.id]["net_pnl"])         # per-account realized withheld for the EUR account
        self.assertEqual(rows[usd.id]["net_pnl"], 10.0)

    def test_all_usd_observed_is_complete(self):
        u = _user("oc2"); a = _acct(u)
        _closed_trade(a, 9.65)
        bal = {a.id: {"balance": 50009.65, "equity": 50009.65, "currency": "USD"}}
        payload, _ = PF.build_summary(u, "ALL", fetch_balance=lambda x, timeout=None: bal.get(x.id),
                                      fetch_positions=_fake_positions({a.id: []}), rate_source=FX.NullFxRateSource())
        m = payload["aggregate"]["metrics"]
        self.assertEqual(m["basis"], "USD"); self.assertEqual(m["excluded_accounts"], [])
        self.assertEqual(m["net_pnl_total"], 9.65)


class MaxDrawdownDeterminismTests(TestCase):
    def test_same_second_cluster_is_deterministic(self):
        u = _user("dd1"); a = _acct(u)
        t = timezone.now()
        _closed_trade(a, 100.0, when=t); _closed_trade(a, -100.0, when=t); _closed_trade(a, -100.0, when=t)
        m1 = PF.portfolio_metrics([a]); m2 = PF.portfolio_metrics([a])
        self.assertEqual(m1["max_drawdown_money"], m2["max_drawdown_money"])   # reproducible (close_time,id tiebreak)
        self.assertEqual(m1["max_drawdown_money"], 200.0)                      # id order: 100,0,-100 -> peak-trough 200


# ── Scope / ownership (IDOR) ──────────────────────────────────────────────────────────────────────────────────
class ScopeTests(TestCase):
    def test_all_scope_lists_owned_non_decommissioned(self):
        u = _user("s1")
        a1 = _acct(u); a2 = _acct(u); _acct(u, removed=True)         # tombstone excluded
        accts, err = PF.resolve_scope(u, "ALL")
        self.assertIsNone(err); self.assertEqual({a.id for a in accts}, {a1.id, a2.id})

    def test_single_owned_scope(self):
        u = _user("s2"); a = _acct(u)
        accts, err = PF.resolve_scope(u, str(a.id))
        self.assertIsNone(err); self.assertEqual([x.id for x in accts], [a.id])

    def test_foreign_account_is_404(self):
        u = _user("s3"); other = _user("s3b"); foreign = _acct(other)
        accts, err = PF.resolve_scope(u, str(foreign.id))
        self.assertIsNone(accts); self.assertEqual(err, 404)

    def test_nonexistent_account_is_404(self):
        u = _user("s4")
        _, err = PF.resolve_scope(u, "99999999")
        self.assertEqual(err, 404)

    def test_malformed_scope_is_400(self):
        u = _user("s5")
        _, err = PF.resolve_scope(u, "not-a-number")
        self.assertEqual(err, 400)

    def test_decommissioned_excluded_from_owned(self):
        u = _user("s6"); _acct(u, removed=True)
        self.assertEqual(list(PF.owned_accounts(u)), [])


# ── Open positions (multi-account, isolation) ────────────────────────────────────────────────────────────────
def _fake_positions(mapping):
    """mapping: {account_id: [position dicts]}; returns a fetch_positions(account, *, timeout) fake. The
    aggregation passes a deadline-aware ``timeout``; the fake accepts and ignores it (no real socket)."""
    def _f(account, timeout=None):
        return mapping.get(account.id)
    return _f


class OpenTradesTests(TestCase):
    def test_multi_account_attribution_and_scoped_ids(self):
        u = _user("o1"); a1 = _acct(u, broker="Taurex"); a2 = _acct(u, broker="Pepperstone")
        fetch = _fake_positions({
            a1.id: [{"ticket": 123, "symbol": "XAUUSD", "side": "BUY", "volume": 0.01,
                     "price_open": 2400, "price_current": 2412, "profit": 12.45, "magic": 0}],
            a2.id: [{"ticket": 123, "symbol": "EURUSD", "side": "SELL", "volume": 0.02,
                     "price_open": 1.1, "price_current": 1.101, "profit": -3.14, "magic": 0}],
        })
        rows, per = PF.open_positions([a1, a2], fetch_positions=fetch, rate_source=FX.NullFxRateSource(),
                                      observed_currency={a1.id: "USD", a2.id: "USD"})   # USD accounts (explicit)
        self.assertEqual(len(rows), 2)
        ids = {r["position_id"] for r in rows}
        self.assertEqual(ids, {f"{a1.id}:123", f"{a2.id}:123"})     # same ticket, distinct account-scoped id
        by_acct = {r["account_id"]: r for r in rows}
        self.assertEqual(by_acct[a1.id]["broker"], "Taurex")
        self.assertEqual(by_acct[a1.id]["pl_usd"], 12.45)
        self.assertEqual(by_acct[a2.id]["broker"], "Pepperstone")

    def test_same_symbol_stays_distinct(self):
        u = _user("o2"); a1 = _acct(u); a2 = _acct(u)
        fetch = _fake_positions({
            a1.id: [{"ticket": 1, "symbol": "XAUUSD", "side": "BUY", "volume": 0.4, "profit": 84.21, "magic": 0}],
            a2.id: [{"ticket": 2, "symbol": "XAUUSD", "side": "BUY", "volume": 0.01, "profit": 1.0, "magic": 0}],
        })
        rows, _ = PF.open_positions([a1, a2], fetch_positions=fetch, rate_source=FX.NullFxRateSource())
        self.assertEqual(len({r["position_id"] for r in rows}), 2)  # not merged by symbol

    def test_empty_positions(self):
        u = _user("o3"); a = _acct(u)
        rows, per = PF.open_positions([a], fetch_positions=_fake_positions({a.id: []}),
                                      rate_source=FX.NullFxRateSource())
        self.assertEqual(rows, []); self.assertEqual(per[a.id]["open_count"], 0)

    def test_fetch_failure_marks_stale_not_dropped(self):
        u = _user("o4"); a = _acct(u)
        rows, per = PF.open_positions([a], fetch_positions=_fake_positions({}),   # returns None
                                      rate_source=FX.NullFxRateSource())
        self.assertEqual(rows, []); self.assertTrue(per[a.id]["stale"])

    def test_aggregation_deadline_bounds_fan_out(self):
        # Overall wall-clock budget: once the deadline is passed, remaining accounts are marked STALE WITHOUT
        # another ~10s live read. Healthy accounts already fetched still contribute; the slow ones are named
        # (present + stale), never dropped — so the dashboard settles in bounded time with a labelled partial.
        u = _user("o_deadline"); a1 = _acct(u); a2 = _acct(u); a3 = _acct(u)
        calls = []

        def fetch(account, timeout=None):
            calls.append(account.id)
            return [{"ticket": 1, "symbol": "X", "side": "BUY", "volume": 1, "profit": 10.0, "magic": 0}]

        # deadline=100.0; monotonic() returns 50 (a1 in budget → fetched), then 150, 151 (a2, a3 past → stale).
        with mock.patch("analytics.portfolio.time.monotonic", side_effect=[50.0, 150.0, 151.0]):
            rows, per = PF.open_positions([a1, a2, a3], fetch_positions=fetch,
                                          rate_source=FX.NullFxRateSource(), deadline=100.0)
        self.assertEqual(calls, [a1.id])                                  # only the in-budget account was read
        self.assertFalse(per[a1.id]["stale"]); self.assertEqual(per[a1.id]["open_count"], 1)
        self.assertTrue(per[a2.id]["stale"]); self.assertTrue(per[a3.id]["stale"])   # named, not dropped
        self.assertEqual(set(per.keys()), {a1.id, a2.id, a3.id})
        self.assertEqual(len(rows), 1)                                    # a1's position contributed

    def test_open_pl_aggregation(self):
        u = _user("o5"); a1 = _acct(u); a2 = _acct(u)
        fetch = _fake_positions({
            a1.id: [{"ticket": 1, "symbol": "X", "side": "BUY", "volume": 1, "profit": 10.0, "magic": 0}],
            a2.id: [{"ticket": 2, "symbol": "Y", "side": "SELL", "volume": 1, "profit": -4.0, "magic": 0}],
        })
        _, per = PF.open_positions([a1, a2], fetch_positions=fetch, rate_source=FX.NullFxRateSource(),
                                   observed_currency={a1.id: "USD", a2.id: "USD"})   # USD accounts (explicit)
        self.assertEqual(per[a1.id]["open_pl_usd"], 10.0); self.assertEqual(per[a2.id]["open_pl_usd"], -4.0)

    def test_top_level_open_pl_marks_stale_partial(self):
        # M1: a total that dropped an unreadable account is PARTIAL and surfaces that account — never "USD complete".
        u = _user("o6"); ok = _acct(u); down = _acct(u)
        fetch = _fake_positions({ok.id: [{"ticket": 1, "symbol": "X", "side": "BUY", "volume": 1,
                                          "profit": 10.0, "magic": 0}]})   # down returns None (stale)
        _, per = PF.open_positions([ok, down], fetch_positions=fetch, rate_source=FX.NullFxRateSource(),
                                   observed_currency={ok.id: "USD"})   # ok is a USD account (explicit)
        agg, stale = PF.portfolio_open_pl(per, FX.NullFxRateSource())
        self.assertEqual(agg.total_usd, 10.0); self.assertEqual(agg.basis, "PARTIAL")
        self.assertEqual(stale, [down.id])
        self.assertIn(down.id, [x["account_id"] for x in agg.unconverted])

    def test_top_level_open_pl_currency_partial(self):
        # M1: a non-USD position with no rate degrades the TOTAL to PARTIAL (not summed as if USD).
        u = _user("o7"); a = _acct(u, currency="GBP")
        fetch = _fake_positions({a.id: [{"ticket": 1, "symbol": "X", "side": "BUY", "volume": 1,
                                         "profit": 5.0, "magic": 0}]})
        _, per = PF.open_positions([a], fetch_positions=fetch, rate_source=FX.NullFxRateSource())
        agg, stale = PF.portfolio_open_pl(per, FX.NullFxRateSource())
        self.assertEqual(agg.basis, "PARTIAL"); self.assertEqual(agg.total_usd, 0.0)


# ── build_summary (aggregation incl. stopped accounts) ───────────────────────────────────────────────────────
class SummaryTests(TestCase):
    def test_aggregates_multiple_accounts_including_stopped(self):
        u = _user("b1")
        a1 = _acct(u, broker="IS6", active=True)
        a2 = _acct(u, broker="Pepperstone", active=True)
        a3 = _acct(u, broker="Taurex", active=False)                 # stopped — still contributes
        bal = {a1.id: {"balance": 1000, "equity": 1010, "currency": "USD"},
               a2.id: {"balance": 2000, "equity": 1990, "currency": "USD"},
               a3.id: {"balance": 500, "equity": 500, "currency": "USD"}}
        payload, err = PF.build_summary(
            u, "ALL", fetch_balance=lambda a, timeout=None: bal.get(a.id),
            fetch_positions=_fake_positions({}), rate_source=FX.NullFxRateSource())
        self.assertIsNone(err)
        self.assertEqual(payload["account_count"], 3)
        self.assertEqual(payload["aggregate"]["balance_usd"]["total_usd"], 3500.0)   # includes stopped a3
        self.assertEqual(payload["aggregate"]["balance_usd"]["basis"], "USD")

    def test_balance_fetch_failure_marks_account_stale(self):
        u = _user("b2"); a = _acct(u)
        payload, _ = PF.build_summary(u, "ALL", fetch_balance=lambda x, timeout=None: None,
                                      fetch_positions=_fake_positions({}), rate_source=FX.NullFxRateSource())
        self.assertTrue(payload["accounts"][0]["stale"])

    def test_mixed_currency_total_is_partial(self):
        u = _user("b3")
        a1 = _acct(u); a2 = _acct(u)
        bal = {a1.id: {"balance": 1000, "equity": 1000, "currency": "USD"},
               a2.id: {"balance": 800, "equity": 800, "currency": "GBP"}}
        payload, _ = PF.build_summary(u, "ALL", fetch_balance=lambda a, timeout=None: bal.get(a.id),
                                      fetch_positions=_fake_positions({}), rate_source=FX.NullFxRateSource())
        agg = payload["aggregate"]["balance_usd"]
        self.assertEqual(agg["total_usd"], 1000.0); self.assertEqual(agg["basis"], "PARTIAL")

    def test_summary_carries_daily_basis_and_stale_accounts(self):
        u = _user("b4"); a = _acct(u)
        payload, _ = PF.build_summary(u, "ALL", fetch_balance=lambda x, timeout=None: {"balance": 1, "equity": 1, "currency": "USD"},
                                      fetch_positions=_fake_positions({a.id: []}),   # healthy: reachable, no positions
                                      rate_source=FX.NullFxRateSource())
        self.assertEqual(payload["aggregate"]["daily_realized_pnl_basis"], "USD")
        self.assertEqual(payload["stale_accounts"], [])           # reachable -> nothing dropped
        self.assertIn("metrics", payload["aggregate"])
        self.assertEqual(payload["aggregate"]["metrics"]["basis"], "USD")


# ── R1 incident regression: a hung bridge must not starve healthy accounts or blow the client deadline ────────
class BoundedReadFanOutTests(TestCase):
    """The R1 incident: ONE hung bridge (TradersWay/46 in prod, ~10s per live read) left the whole ALL-scope
    summary slower than the client's deadline AND starved the healthy accounts' balance reads (they were skipped
    as 'budget exhausted' -> stale -> equity UNAVAILABLE). Root cause: the summary does TWO serial live passes
    (positions, then balances) and, with no per-read cap, the hung account cost the full timeout in EACH pass.
    The fix gives every live read a deadline-aware timeout capped at _PER_READ_TIMEOUT_SECONDS, with
    2*cap < _AGG_BUDGET_SECONDS so the balance pass still has budget for the fast accounts after the hung one
    fails. This test DISCRIMINATES: on the pre-fix code the fetchers received no timeout (None), the hung account
    consumed the budget across both passes, the healthy balances were skipped (stale), and the simulated clock
    blew past the budget — so every assertion below would fail."""

    def test_hung_account_does_not_starve_healthy_or_blow_budget(self):
        u = _user("bf1")
        hung = _acct(u); h1 = _acct(u); h2 = _acct(u)
        # A mutable simulated monotonic clock shared by portfolio.py and the fakes. Each live read advances the
        # clock by min(latency, timeout) and RAISES once it reaches its timeout — exactly how urlopen(timeout=t)
        # behaves against a half-open bridge. hung_latency (8s) models a bridge slower than the per-read cap but,
        # when UNCAPPED (timeout=None -> the fetchers' own 10s default), still "succeeds" late and eats the budget.
        clock = {"t": 1000.0}
        seen = []

        def _read(account, timeout):
            seen.append(timeout)
            t = timeout if timeout is not None else 10.0
            lat = 8.0 if account.id == hung.id else 0.05
            if lat > t:
                clock["t"] += t
                raise TimeoutError("simulated hung bridge")
            clock["t"] += lat

        def bal(account, timeout=None):
            _read(account, timeout)
            return {"balance": 100.0, "equity": 100.0, "currency": "USD"}

        def pos(account, timeout=None):
            _read(account, timeout)
            return []

        # hung is FIRST (worst case: read before the healthy accounts in BOTH passes).
        with mock.patch("analytics.portfolio.resolve_scope", return_value=([hung, h1, h2], None)), \
                mock.patch("analytics.portfolio.time.monotonic", side_effect=lambda: clock["t"]):
            payload, err = PF.build_summary(u, "ALL", fetch_balance=bal, fetch_positions=pos,
                                            rate_source=FX.NullFxRateSource())
        self.assertIsNone(err)
        # (1) Every live read got a bounded, deadline-aware timeout — never uncapped (pre-fix passed None).
        self.assertTrue(seen)
        self.assertTrue(all(t is not None and 0 < t <= PF._PER_READ_TIMEOUT_SECONDS for t in seen))
        # (2) Total simulated wall-clock stayed within the budget (pre-fix: ~2x the hung reads blows past it).
        self.assertLessEqual(clock["t"] - 1000.0, PF._AGG_BUDGET_SECONDS)
        acct = {a["account_id"]: a for a in payload["accounts"]}
        # (3) The hung account reads stale; the HEALTHY accounts are NOT starved.
        self.assertTrue(acct[hung.id]["stale"])
        self.assertFalse(acct[h1.id]["stale"]); self.assertFalse(acct[h2.id]["stale"])
        self.assertEqual(acct[h1.id]["equity_native"], 100.0); self.assertEqual(acct[h2.id]["equity_native"], 100.0)
        # (4) The USD aggregate is a measured PARTIAL over the healthy subset — never UNAVAILABLE, never a fake 0.
        eq = payload["aggregate"]["equity_usd"]
        self.assertEqual(eq["state"], "PARTIAL")
        self.assertEqual(eq["included_count"], 2); self.assertEqual(eq["excluded_count"], 1)


# ── Endpoints (ownership + default scope) ────────────────────────────────────────────────────────────────────
class ViewTests(TestCase):
    def setUp(self):
        self.u = _user("v1"); self.other = _user("v1b")
        self.a = _acct(self.u); self.foreign = _acct(self.other)
        self.c = APIClient(); self.c.force_authenticate(self.u)

    def test_summary_default_scope_all_owned_only(self):
        r = self.c.get("/api/analytics/portfolio/summary/")
        self.assertEqual(r.status_code, 200)
        ids = {row["account_id"] for row in r.json()["accounts"]}
        self.assertIn(self.a.id, ids); self.assertNotIn(self.foreign.id, ids)

    def test_summary_foreign_account_404(self):
        r = self.c.get(f"/api/analytics/portfolio/summary/?scope={self.foreign.id}")
        self.assertEqual(r.status_code, 404)

    def test_open_trades_foreign_account_404(self):
        r = self.c.get(f"/api/analytics/portfolio/open-trades/?scope={self.foreign.id}")
        self.assertEqual(r.status_code, 404)

    def test_open_trades_default_ok(self):
        r = self.c.get("/api/analytics/portfolio/open-trades/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["reporting_currency"], "USD")
        self.assertFalse(body["truncated"])                       # M3: honest row-cap flag, false at normal sizes
        self.assertIn("stale_accounts", body)

    def test_endpoints_are_get_only_no_mutation(self):
        # Observation-only: POST must not be accepted (no execution/mutation surface on these endpoints).
        self.assertEqual(self.c.post("/api/analytics/portfolio/summary/").status_code, 405)
        self.assertEqual(self.c.post("/api/analytics/portfolio/open-trades/").status_code, 405)


# ── Honest reporting when all eligible accounts are excluded (no measured $0) ─────────────────────────────────
# A USD aggregate / statistics set that dropped EVERY eligible account (FX-unconvertible, or no live observation)
# must surface an explicit state — Unavailable / Partial — never a measured 0 / 0% / $0.00 that reads as real.
# Each test is DISCRIMINATING: deleting the honesty guard flips the assertion back to the old measured-zero.
class AggregateStateTests(TestCase):
    _NULL = FX.NullFxRateSource()

    def test_all_usd_aggregate_is_ok_byte_for_byte(self):
        # Requirement 8: a pure, fresh, convertible USD portfolio is preserved EXACTLY — existing keys unchanged,
        # new state keys are the no-exclusion identity (OK / 0 excluded / no reason).
        agg = FX.aggregate_usd([{"account_id": 1, "amount": 100.0, "currency": "USD"},
                                {"account_id": 2, "amount": 250.5, "currency": "USD"}], self._NULL)
        d = agg.as_dict()
        self.assertEqual(d["total_usd"], 350.5)                 # existing value unchanged
        self.assertEqual(d["basis"], "USD")
        self.assertEqual(d["converted_count"], 2)
        self.assertEqual(d["unconverted"], [])
        self.assertEqual(d["state"], "OK")                      # additive, no-exclusion identity
        self.assertIsNone(d["reason"])
        self.assertEqual(d["included_count"], 2)
        self.assertEqual(d["excluded_count"], 0)

    def test_all_excluded_currency_is_unavailable_not_zero(self):
        # Scenario: all accounts excluded (every figure non-USD, no rate). UNAVAILABLE, null total — NOT a measured 0.
        agg = FX.aggregate_usd([{"account_id": 1, "amount": 100.0, "currency": "GBP"},
                                {"account_id": 2, "amount": 200.0, "currency": "EUR"}], self._NULL)
        d = agg.as_dict()
        self.assertIsNone(d["total_usd"])                       # DISCRIMINATING: old behaviour serialised 0.0
        self.assertEqual(d["state"], "UNAVAILABLE")
        self.assertEqual(d["reason"], "currency_unconvertible")
        self.assertEqual(d["included_count"], 0)
        self.assertEqual(d["excluded_count"], 2)
        self.assertEqual(agg.total_usd, 0.0)                    # the raw running sum is 0.0; only the guard nulls it
        self.assertTrue(all(u.get("category") == "currency_unconvertible" for u in d["unconverted"]))

    def test_partial_mixed_is_measured_over_included_subset(self):
        # Scenario: mixed USD + GBP. A measured total over the INCLUDED (USD) subset, flagged PARTIAL — the GBP
        # figure is never summed as if USD, and the counts expose the split (requirement 3).
        agg = FX.aggregate_usd([{"account_id": 1, "amount": 100.0, "currency": "USD"},
                                {"account_id": 2, "amount": 999.0, "currency": "GBP"}], self._NULL)
        d = agg.as_dict()
        self.assertEqual(d["total_usd"], 100.0)                 # GBP excluded, never summed as if USD
        self.assertEqual(d["state"], "PARTIAL")
        self.assertEqual(d["reason"], "currency_unconvertible")
        self.assertEqual(d["included_count"], 1)
        self.assertEqual(d["excluded_count"], 1)

    def test_empty_aggregate_is_empty_state(self):
        # No items at all (no accounts) is EMPTY, not UNAVAILABLE: genuinely nothing to report (keeps 0.0).
        agg = FX.aggregate_usd([], self._NULL)
        d = agg.as_dict()
        self.assertEqual(d["state"], "EMPTY"); self.assertEqual(d["total_usd"], 0.0); self.assertIsNone(d["reason"])

    def test_vetted_rate_source_makes_gbp_available_opt_in(self):
        # Opt-in governance path: a vetted MappingFxRateSource (operator-provided, never default 1.0) converts the
        # SAME GBP figure that NullFxRateSource (the default) must degrade. Proves the honest-degrade is a missing
        # feed, not a code floor, and that wiring a rate source is the single lever that flips UNAVAILABLE -> OK.
        items = [{"account_id": 1, "amount": 100.0, "currency": "GBP"}]
        self.assertEqual(FX.aggregate_usd(items, self._NULL).as_dict()["state"], "UNAVAILABLE")
        vetted = FX.MappingFxRateSource({("GBP", "USD"): 1.25}, as_of="2026-10-22T00:00:00Z", source="operator")
        d = FX.aggregate_usd(items, vetted).as_dict()
        self.assertEqual(d["state"], "OK"); self.assertEqual(d["total_usd"], 125.0)

    def test_reason_category_mixed_when_currency_and_stale(self):
        # Stale (open-P/L) + currency exclusions in one aggregate -> reason "mixed" (UI maps mixed to stale/missing).
        agg = FX.UsdAggregate()
        agg.unconverted = [{"account_id": 1, "category": "currency_unconvertible"},
                           {"account_id": 2, "category": "stale_observation"}]
        self.assertEqual(agg.reason, "mixed")


class BuildSummaryHonestStateTests(TestCase):
    """build_summary end-to-end: the dashboard payload must carry explicit states, never measured zeros, whenever
    every eligible account is excluded — and must keep NATIVE figures for a single-account scope."""

    def test_all_gbp_portfolio_unavailable_everywhere(self):
        # Scenario: the production shape (account 49 is GBP). ALL accounts non-USD -> balance/equity UNAVAILABLE
        # (null, never $0), metrics UNAVAILABLE (net/total_trades null, never 0 / 0%), daily withheld (None).
        u = _user("hs1"); g1 = _acct(u); g2 = _acct(u)
        _closed_trade(g1, 123.0); _closed_trade(g2, 456.0)       # real GBP trades that must NOT surface as USD
        bal = {g1.id: {"balance": 1000, "equity": 1010, "currency": "GBP"},
               g2.id: {"balance": 2000, "equity": 2005, "currency": "GBP"}}
        payload, _ = PF.build_summary(u, "ALL", fetch_balance=lambda a, timeout=None: bal.get(a.id),
                                      fetch_positions=_fake_positions({g1.id: [], g2.id: []}), rate_source=FX.NullFxRateSource())
        agg = payload["aggregate"]
        self.assertIsNone(agg["balance_usd"]["total_usd"]); self.assertEqual(agg["balance_usd"]["state"], "UNAVAILABLE")
        self.assertEqual(agg["balance_usd"]["reason"], "currency_unconvertible")
        self.assertEqual(agg["balance_usd"]["excluded_count"], 2); self.assertEqual(agg["balance_usd"]["included_count"], 0)
        self.assertIsNone(agg["equity_usd"]["total_usd"]); self.assertEqual(agg["equity_usd"]["state"], "UNAVAILABLE")
        m = agg["metrics"]
        self.assertEqual(m["state"], "UNAVAILABLE")
        self.assertIsNone(m["net_pnl_total"]); self.assertIsNone(m["total_trades"]); self.assertIsNone(m["win_rate_pct"])
        self.assertIsNone(agg["daily_realized_pnl_usd"]); self.assertEqual(agg["daily_realized_pnl_basis"], "PARTIAL")

    def test_missing_balance_observation_is_unavailable_missing_snapshot(self):
        # Scenario: missing/stale snapshot — the only account's live balance read fails. The balance aggregate is
        # UNAVAILABLE with a SAFE category (no credentials/account-number/login), and the account is marked stale.
        u = _user("hs2"); a = _acct(u)
        payload, _ = PF.build_summary(u, "ALL", fetch_balance=lambda x, timeout=None: None,
                                      fetch_positions=_fake_positions({a.id: []}), rate_source=FX.NullFxRateSource())
        bal = payload["aggregate"]["balance_usd"]
        self.assertIsNone(bal["total_usd"]); self.assertEqual(bal["state"], "UNAVAILABLE")
        self.assertEqual(bal["reason"], "missing_snapshot")
        self.assertTrue(payload["accounts"][0]["stale"])

    def test_stale_open_positions_is_unavailable_stale_observation(self):
        # Scenario: stale observation — the only account's live POSITIONS read fails. The floating-P/L aggregate is
        # UNAVAILABLE with the stale_observation category (distinct safe code from the currency/missing ones).
        u = _user("hs3"); a = _acct(u)
        payload, _ = PF.build_summary(u, "ALL", fetch_balance=lambda x, timeout=None: {"balance": 10, "equity": 10, "currency": "USD"},
                                      fetch_positions=_fake_positions({}),   # None -> stale
                                      rate_source=FX.NullFxRateSource())
        opl = payload["aggregate"]["open_pl_usd"]
        self.assertIsNone(opl["total_usd"]); self.assertEqual(opl["state"], "UNAVAILABLE")
        self.assertEqual(opl["reason"], "stale_observation")

    def test_partial_portfolio_shows_included_and_flags(self):
        # Scenario: partial portfolio (one USD + one GBP). Measured USD over the included account, PARTIAL, counts
        # expose 1 included / 1 excluded; metrics likewise measured over the USD account only.
        u = _user("hs4"); usd = _acct(u); gbp = _acct(u)
        _closed_trade(usd, 10.0); _closed_trade(gbp, 999.0)
        bal = {usd.id: {"balance": 1000, "equity": 1000, "currency": "USD"},
               gbp.id: {"balance": 800, "equity": 800, "currency": "GBP"}}
        payload, _ = PF.build_summary(u, "ALL", fetch_balance=lambda a, timeout=None: bal.get(a.id),
                                      fetch_positions=_fake_positions({usd.id: [], gbp.id: []}), rate_source=FX.NullFxRateSource())
        agg = payload["aggregate"]
        self.assertEqual(agg["balance_usd"]["total_usd"], 1000.0); self.assertEqual(agg["balance_usd"]["state"], "PARTIAL")
        self.assertEqual(agg["balance_usd"]["included_count"], 1); self.assertEqual(agg["balance_usd"]["excluded_count"], 1)
        m = agg["metrics"]
        self.assertEqual(m["state"], "PARTIAL"); self.assertEqual(m["net_pnl_total"], 10.0); self.assertEqual(m["included_count"], 1)

    def test_single_account_native_gbp_shown_not_blanked_or_converted(self):
        # Requirement 5: a single-account GBP scope must show the NATIVE currency and NATIVE balance where
        # authoritative data exists — never blanked, never forced through USD (the USD aggregate is Unavailable).
        u = _user("hs5"); gbp = _acct(u)
        payload, err = PF.build_summary(u, str(gbp.id),
                                        fetch_balance=lambda a, timeout=None: {"balance": 812.34, "equity": 820.0, "currency": "GBP"},
                                        fetch_positions=_fake_positions({gbp.id: []}), rate_source=FX.NullFxRateSource())
        self.assertIsNone(err)
        row = payload["accounts"][0]
        self.assertEqual(row["currency"], "GBP")
        self.assertEqual(row["balance_native"], 812.34)         # native figure preserved, not blanked
        self.assertEqual(row["equity_native"], 820.0)
        self.assertEqual(payload["aggregate"]["balance_usd"]["state"], "UNAVAILABLE")   # USD aggregate honest-unavailable

    def test_all_usd_portfolio_regression_unchanged(self):
        # Requirement 8 end-to-end: a fully valid USD portfolio reports measured numbers exactly as before, with the
        # additive state = OK (DISCRIMINATING: the all-excluded guard must not touch the healthy path).
        u = _user("hs6"); a = _acct(u)
        _closed_trade(a, 9.65)
        payload, _ = PF.build_summary(u, "ALL",
                                      fetch_balance=lambda x, timeout=None: {"balance": 50009.65, "equity": 50009.65, "currency": "USD"},
                                      fetch_positions=_fake_positions({a.id: []}), rate_source=FX.NullFxRateSource())
        agg = payload["aggregate"]
        self.assertEqual(agg["balance_usd"]["total_usd"], 50009.65); self.assertEqual(agg["balance_usd"]["state"], "OK")
        self.assertEqual(agg["metrics"]["state"], "OK"); self.assertEqual(agg["metrics"]["net_pnl_total"], 9.65)
        self.assertEqual(agg["daily_realized_pnl_basis"], "USD")

    def test_no_safe_reason_leaks_identifiers(self):
        # Requirement 4: exclusion reasons are safe CATEGORY CODES only — never a login/account-number/credential.
        u = _user("hs7"); gbp = _acct(u, number="1302599")
        payload, _ = PF.build_summary(u, "ALL",
                                      fetch_balance=lambda a, timeout=None: {"balance": 1, "equity": 1, "currency": "GBP"},
                                      fetch_positions=_fake_positions({gbp.id: []}), rate_source=FX.NullFxRateSource())
        import json as _json
        blob = _json.dumps(payload["aggregate"]["balance_usd"]["unconverted"])
        self.assertNotIn("1302599", blob)                       # the broker account number never appears
        for u_entry in payload["aggregate"]["balance_usd"]["unconverted"]:
            self.assertIn(u_entry["category"], {"currency_unconvertible", "stale_observation",
                                                "missing_snapshot", "read_refused"})


# ── Currency-integrity: open-position P/L is labelled by the OBSERVED currency, never defaulted to USD ─────────
# Confirmed prod defect: an account with a NULL stored currency had its open-position P/L defaulted to USD
# (normalise_currency(None)->USD) and silently mixed into the All-Accounts USD total (a GBP -12.24 summed with a
# USD +13.83 as "+$1.50"). Each test DISCRIMINATES: reverting the fix (default unknown->USD) flips the assertion.
class CurrencyIntegrityTests(TestCase):
    _NULL = FX.NullFxRateSource()

    def _pos(self, profit, symbol="XAUUSD"):
        return {"ticket": 1, "symbol": symbol, "side": "BUY", "volume": 1.0,
                "price_open": 4000, "price_current": 4000, "profit": profit, "magic": 0}

    # resolve_position_currency ---------------------------------------------------------------
    def test_resolve_prefers_observed_then_stored_then_unknown(self):
        self.assertEqual(FX.resolve_position_currency("GBP", "USD"), "GBP")            # observed wins
        self.assertEqual(FX.resolve_position_currency(None, "EUR"), "EUR")             # fall back to stored
        self.assertEqual(FX.resolve_position_currency("", "  "), FX.UNKNOWN_CURRENCY)  # neither -> UNKNOWN
        self.assertEqual(FX.resolve_position_currency(None, None), FX.UNKNOWN_CURRENCY)
        self.assertNotEqual(FX.resolve_position_currency(None, None), FX.USD)          # NEVER defaults to USD

    # open_positions currency labelling -------------------------------------------------------
    def test_gbp_observed_position_excluded_not_usd(self):
        u = _user("ci1"); a = _acct(u, broker="Pepperstone")                           # stored currency null
        rows, per = PF.open_positions([a], fetch_positions=_fake_positions({a.id: [self._pos(-12.24)]}),
                                      rate_source=self._NULL, observed_currency={a.id: "GBP"})
        self.assertEqual(rows[0]["pl_native_currency"], "GBP")
        self.assertEqual(rows[0]["pl_native"], -12.24)
        self.assertIsNone(rows[0]["pl_usd"])                                           # NOT summed as USD
        self.assertEqual(per[a.id]["open_pl_basis"], "PARTIAL")

    def test_null_currency_position_is_unknown_not_usd(self):
        # The exact prod regression: null stored currency + no observed entry -> UNKNOWN, excluded (pre-fix: USD).
        u = _user("ci2"); a = _acct(u)                                                 # account_currency null
        rows, per = PF.open_positions([a], fetch_positions=_fake_positions({a.id: [self._pos(50.0)]}),
                                      rate_source=self._NULL)
        self.assertEqual(rows[0]["pl_native_currency"], FX.UNKNOWN_CURRENCY)
        self.assertIsNone(rows[0]["pl_usd"])
        self.assertEqual(per[a.id]["open_pl_basis"], "PARTIAL")

    def test_usd_observed_position_included(self):
        u = _user("ci3"); a = _acct(u)
        rows, per = PF.open_positions([a], fetch_positions=_fake_positions({a.id: [self._pos(13.83)]}),
                                      rate_source=self._NULL, observed_currency={a.id: "USD"})
        self.assertEqual(rows[0]["pl_native_currency"], "USD")
        self.assertEqual(rows[0]["pl_usd"], 13.83)
        self.assertEqual(per[a.id]["open_pl_basis"], "USD")

    # the "+$1.50" mixed-currency aggregate regression ---------------------------------------
    def test_mixed_usd_gbp_all_accounts_is_partial_usd_only(self):
        u = _user("ci4"); usd = _acct(u, broker="TradersWay"); gbp = _acct(u, broker="Pepperstone")
        fetch = _fake_positions({usd.id: [self._pos(13.83)], gbp.id: [self._pos(-12.24), self._pos(-0.09)]})
        _, per = PF.open_positions([usd, gbp], fetch_positions=fetch, rate_source=self._NULL,
                                   observed_currency={usd.id: "USD", gbp.id: "GBP"})
        d, _stale = PF.portfolio_open_pl(per, self._NULL)
        dd = d.as_dict()
        self.assertEqual(dd["total_usd"], 13.83)                 # USD-only, NOT 13.83-12.24-0.09
        self.assertNotEqual(dd["total_usd"], 1.5)                # the exact contamination value, rejected
        self.assertEqual(dd["state"], "PARTIAL")
        self.assertEqual(dd["excluded_count"], 2)                # both GBP positions excluded
        self.assertTrue(all(x["native_currency"] == "GBP" for x in dd["unconverted"]))
        self.assertEqual(d.reason, "currency_unconvertible")

    # observed_currency_map reads the verified equity ledger ---------------------------------
    def test_observed_currency_map_from_equity_ledger(self):
        from analytics.models import AccountEquitySnapshot
        u = _user("ci5"); a_usd = _acct(u); a_gbp = _acct(u); a_none = _acct(u)
        now = timezone.now()
        AccountEquitySnapshot.objects.create(trading_account=a_usd, observed_at=now, account_currency="USD",
                                             balance=100, equity=100)
        AccountEquitySnapshot.objects.create(trading_account=a_gbp, observed_at=now - timedelta(hours=2),
                                             account_currency="USD", balance=1, equity=1)   # older WRONG value
        AccountEquitySnapshot.objects.create(trading_account=a_gbp, observed_at=now, account_currency="GBP",
                                             balance=400, equity=400)
        m = PF.observed_currency_map([a_usd, a_gbp, a_none])
        self.assertEqual(m.get(a_usd.id), "USD")
        self.assertEqual(m.get(a_gbp.id), "GBP")                 # latest snapshot wins over the older USD
        self.assertNotIn(a_none.id, m)                           # no snapshot -> absent (caller falls back)

    def test_build_summary_excludes_gbp_via_ledger_no_fabricated_zero(self):
        from analytics.models import AccountEquitySnapshot
        u = _user("ci6"); gbp = _acct(u, broker="Pepperstone")
        AccountEquitySnapshot.objects.create(trading_account=gbp, observed_at=timezone.now(),
                                             account_currency="GBP", balance=400, equity=400)
        payload, _ = PF.build_summary(
            u, "ALL", fetch_balance=lambda a, timeout=None: {"balance": 400, "equity": 400, "currency": "GBP"},
            fetch_positions=_fake_positions({gbp.id: [self._pos(-12.24)]}), rate_source=self._NULL)
        opl = payload["aggregate"]["open_pl_usd"]
        self.assertEqual(opl["state"], "UNAVAILABLE")            # the ONLY account is GBP -> no measured USD number
        self.assertIsNone(opl["total_usd"])                      # never a fabricated measured 0.0
        self.assertEqual(opl["excluded_count"], 1)

    # the live open-trades VIEW: mixed scope is honest ---------------------------------------
    def test_open_trades_view_mixed_currency_partial(self):
        from analytics.models import AccountEquitySnapshot
        u = _user("ci7"); usd = _acct(u, broker="TradersWay"); gbp = _acct(u, broker="Pepperstone")
        now = timezone.now()
        AccountEquitySnapshot.objects.create(trading_account=usd, observed_at=now, account_currency="USD",
                                             balance=250, equity=264)
        AccountEquitySnapshot.objects.create(trading_account=gbp, observed_at=now, account_currency="GBP",
                                             balance=446, equity=432)
        pos = {usd.id: [self._pos(13.83)], gbp.id: [self._pos(-12.24), self._pos(-0.09)]}

        def _fake(account, windows_username, timeout=10.0):
            return pos.get(account.id)
        c = APIClient(); c.force_authenticate(u)
        with mock.patch("analytics.views_portfolio._fetch_mt5_open_positions", side_effect=_fake), \
                mock.patch("analytics.views_portfolio._account_windows_username", return_value="wu"):
            r = c.get("/api/analytics/portfolio/open-trades/?scope=ALL")
        self.assertEqual(r.status_code, 200)
        body = r.json(); opl = body["open_pl_usd"]
        self.assertEqual(opl["total_usd"], 13.83)                # USD-only; GBP P/L is NOT mixed in
        self.assertEqual(opl["state"], "PARTIAL")
        self.assertEqual(opl["excluded_count"], 2)
        gbp_row = next(t for t in body["trades"] if t["account_id"] == gbp.id and t["pl_native"] == -12.24)
        self.assertEqual(gbp_row["pl_native_currency"], "GBP")
        self.assertIsNone(gbp_row["pl_usd"])                     # UI renders "-12.24 GBP", never "$"

    # no FX enablement + tenant isolation ----------------------------------------------------
    def test_no_fx_enablement_gbp_stays_excluded(self):
        # The fix must NOT silently enable FX conversion: with the default source a GBP figure is EXCLUDED
        # (unconvertible), never converted into the USD total.
        from analytics.views_portfolio import _rate_source
        rec = FX.to_usd(-12.24, "GBP", _rate_source())
        self.assertFalse(rec["ok"]); self.assertIsNone(rec["usd"])

    def test_tenant_isolation_foreign_gbp_not_in_aggregate(self):
        from analytics.models import AccountEquitySnapshot
        u = _user("ci8"); other = _user("ci8b")
        mine = _acct(u, broker="TradersWay"); foreign = _acct(other, broker="Pepperstone")
        now = timezone.now()
        AccountEquitySnapshot.objects.create(trading_account=mine, observed_at=now, account_currency="USD",
                                             balance=250, equity=264)
        AccountEquitySnapshot.objects.create(trading_account=foreign, observed_at=now, account_currency="GBP",
                                             balance=400, equity=400)
        pos = {mine.id: [self._pos(13.83)], foreign.id: [self._pos(-99.0)]}

        def _fake(account, windows_username, timeout=10.0):
            return pos.get(account.id)
        c = APIClient(); c.force_authenticate(u)
        with mock.patch("analytics.views_portfolio._fetch_mt5_open_positions", side_effect=_fake), \
                mock.patch("analytics.views_portfolio._account_windows_username", return_value="wu"):
            r = c.get("/api/analytics/portfolio/open-trades/?scope=ALL")
        body = r.json()
        acct_ids = {t["account_id"] for t in body["trades"]}
        self.assertIn(mine.id, acct_ids); self.assertNotIn(foreign.id, acct_ids)       # IDOR: foreign excluded
        self.assertEqual(body["open_pl_usd"]["total_usd"], 13.83)                      # only my USD position
