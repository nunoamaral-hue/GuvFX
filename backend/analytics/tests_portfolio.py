"""Portfolio dashboard tests — FX normalisation, USD aggregation, portfolio metrics (from the trade set, not
averaged), scope/ownership (IDOR), and multi-account open-trades attribution. Observation-only; no bridge contact
(live fetchers are injected fakes)."""
from __future__ import annotations

from datetime import timedelta

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


def _closed_trade(account, net, when=None, stage="LIVE"):
    when = when or timezone.now()
    return Trade.objects.create(
        account=account, ticket=_uniq(), symbol="XAUUSD", side="BUY", volume=1,
        open_time=when - timedelta(hours=1), close_time=when, open_price=1, close_price=2,
        profit=net, commission=0, swap=0, source_stage=stage)


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
    """mapping: {account_id: [position dicts]}; returns a fetch_positions(account) fake."""
    def _f(account):
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
        rows, per = PF.open_positions([a1, a2], fetch_positions=fetch, rate_source=FX.NullFxRateSource())
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

    def test_open_pl_aggregation(self):
        u = _user("o5"); a1 = _acct(u); a2 = _acct(u)
        fetch = _fake_positions({
            a1.id: [{"ticket": 1, "symbol": "X", "side": "BUY", "volume": 1, "profit": 10.0, "magic": 0}],
            a2.id: [{"ticket": 2, "symbol": "Y", "side": "SELL", "volume": 1, "profit": -4.0, "magic": 0}],
        })
        _, per = PF.open_positions([a1, a2], fetch_positions=fetch, rate_source=FX.NullFxRateSource())
        self.assertEqual(per[a1.id]["open_pl_usd"], 10.0); self.assertEqual(per[a2.id]["open_pl_usd"], -4.0)


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
            u, "ALL", fetch_balance=lambda a: bal.get(a.id),
            fetch_positions=_fake_positions({}), rate_source=FX.NullFxRateSource())
        self.assertIsNone(err)
        self.assertEqual(payload["account_count"], 3)
        self.assertEqual(payload["aggregate"]["balance_usd"]["total_usd"], 3500.0)   # includes stopped a3
        self.assertEqual(payload["aggregate"]["balance_usd"]["basis"], "USD")

    def test_balance_fetch_failure_marks_account_stale(self):
        u = _user("b2"); a = _acct(u)
        payload, _ = PF.build_summary(u, "ALL", fetch_balance=lambda x: None,
                                      fetch_positions=_fake_positions({}), rate_source=FX.NullFxRateSource())
        self.assertTrue(payload["accounts"][0]["stale"])

    def test_mixed_currency_total_is_partial(self):
        u = _user("b3")
        a1 = _acct(u); a2 = _acct(u)
        bal = {a1.id: {"balance": 1000, "equity": 1000, "currency": "USD"},
               a2.id: {"balance": 800, "equity": 800, "currency": "GBP"}}
        payload, _ = PF.build_summary(u, "ALL", fetch_balance=lambda a: bal.get(a.id),
                                      fetch_positions=_fake_positions({}), rate_source=FX.NullFxRateSource())
        agg = payload["aggregate"]["balance_usd"]
        self.assertEqual(agg["total_usd"], 1000.0); self.assertEqual(agg["basis"], "PARTIAL")


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
        self.assertEqual(r.json()["reporting_currency"], "USD")

    def test_endpoints_are_get_only_no_mutation(self):
        # Observation-only: POST must not be accepted (no execution/mutation surface on these endpoints).
        self.assertEqual(self.c.post("/api/analytics/portfolio/summary/").status_code, 405)
        self.assertEqual(self.c.post("/api/analytics/portfolio/open-trades/").status_code, 405)
