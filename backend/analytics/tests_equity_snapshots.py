"""Equity snapshot ledger + curves + broker-time normalisation tests. Observation-only; no bridge contact."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as _tz
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from analytics import equity_snapshots as EQ
from analytics import portfolio_fx as FX
from analytics.models import AccountEquitySnapshot
from analytics.broker_time import deal_time_to_true_utc, broker_offset_hours, broker_wall_to_true_utc
from trading.models import BrokerServer, TradingAccount

U = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return f"70{_n:05d}"


def _user(name):
    return U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")


def _acct(user, broker="Pepperstone", number=None, currency=None, server=None):
    number = number or _uniq()
    srv, _ = BrokerServer.objects.get_or_create(server_name=server or f"{broker}-Demo-{number}")
    a = TradingAccount.objects.create(user=user, name=broker, broker_name=broker, account_number=number,
                                      is_demo=True, is_active=True, broker_server=srv)
    if currency:
        a.account_currency = currency
        a.save()
    return a


def _snap(account, *, at, equity, balance=None, currency="USD", source="test"):
    """Create a snapshot directly (identity pre-verified) for curve tests."""
    return AccountEquitySnapshot.objects.create(
        trading_account_id=account.id, observed_at=at, account_currency=currency,
        balance=Decimal(str(balance if balance is not None else equity)), equity=Decimal(str(equity)),
        floating_pnl=Decimal("0"), observed_login=account.account_number,
        observed_server=account.broker_server.server_name, source=source)


# ── Snapshot capture (authority / identity / idempotency / Decimal) ───────────────────────────────────────────
class CaptureTests(TestCase):
    def test_authoritative_observation_creates_snapshot(self):
        a = _acct(_user("c1"), server="IS6-Demo")
        row, reason = EQ.capture_account_snapshot(
            a, balance=1000, equity=1012.5, currency="USD",
            observed_login=a.account_number, observed_server="IS6-Demo")
        self.assertEqual(reason, "created")
        self.assertEqual(row.equity, Decimal("1012.50"))
        self.assertEqual(row.balance, Decimal("1000.00"))
        self.assertEqual(row.floating_pnl, Decimal("12.50"))     # equity - balance
        self.assertIsInstance(row.equity, Decimal)               # no float persistence

    def test_wrong_account_login_rejected(self):
        a = _acct(_user("c2"), number="1000", server="IS6-Demo")
        row, reason = EQ.capture_account_snapshot(
            a, balance=1, equity=1, observed_login="9999", observed_server="IS6-Demo")
        self.assertIsNone(row); self.assertEqual(reason, "identity_refused")
        self.assertEqual(AccountEquitySnapshot.objects.count(), 0)

    def test_wrong_server_rejected(self):
        a = _acct(_user("c3"), number="1000", server="IS6-Demo")
        row, reason = EQ.capture_account_snapshot(
            a, balance=1, equity=1, observed_login="1000", observed_server="WRONG-Demo")
        self.assertIsNone(row); self.assertEqual(reason, "identity_refused")

    def test_stale_or_duplicate_is_throttled_idempotent(self):
        a = _acct(_user("c4"), server="IS6-Demo")
        r1, s1 = EQ.capture_account_snapshot(a, balance=1, equity=1, observed_login=a.account_number,
                                             observed_server="IS6-Demo")
        r2, s2 = EQ.capture_account_snapshot(a, balance=2, equity=2, observed_login=a.account_number,
                                             observed_server="IS6-Demo")
        self.assertEqual(s1, "created"); self.assertEqual(s2, "throttled")
        self.assertEqual(AccountEquitySnapshot.objects.filter(trading_account=a).count(), 1)

    def test_no_values_not_written(self):
        a = _acct(_user("c5"), server="IS6-Demo")
        row, reason = EQ.capture_account_snapshot(a, balance=None, equity=None,
                                                  observed_login=a.account_number, observed_server="IS6-Demo")
        self.assertIsNone(row); self.assertEqual(reason, "no_values")

    def test_non_usd_snapshot_stores_native_currency(self):
        a = _acct(_user("c6"), currency="GBP", server="Pepper-Demo")
        row, reason = EQ.capture_account_snapshot(a, balance=800, equity=800, currency="GBP",
                                                  observed_login=a.account_number, observed_server="Pepper-Demo")
        self.assertEqual(reason, "created"); self.assertEqual(row.account_currency, "GBP")


# ── Account curve ─────────────────────────────────────────────────────────────────────────────────────────────
class AccountCurveTests(TestCase):
    def test_zero_points_building(self):
        a = _acct(_user("ac0"))
        c = EQ.account_equity_curve(a)
        self.assertEqual(c["state"], "BUILDING"); self.assertEqual(c["points"], [])

    def test_one_point_single(self):
        a = _acct(_user("ac1"))
        _snap(a, at=timezone.now(), equity=1000)
        c = EQ.account_equity_curve(a)
        self.assertEqual(c["state"], "SINGLE"); self.assertEqual(len(c["points"]), 1)

    def test_multiple_points_ok_chronological(self):
        a = _acct(_user("ac2"))
        t = timezone.now() - timedelta(minutes=20)
        for i, eq in enumerate([1000, 1010, 1005]):
            _snap(a, at=t + timedelta(minutes=5 * i), equity=eq)
        c = EQ.account_equity_curve(a)
        self.assertEqual(c["state"], "OK"); self.assertEqual([p["equity"] for p in c["points"]], [1000.0, 1010.0, 1005.0])

    def test_currency_from_snapshot_not_account_field(self):
        # account_currency NULL but snapshot rows are GBP -> label GBP, never mislabel USD.
        a = _acct(_user("ac3"))
        _snap(a, at=timezone.now(), equity=800, currency="GBP")
        self.assertEqual(EQ.account_equity_curve(a)["currency"], "GBP")


# ── Portfolio curve (alignment / PARTIAL / STALE / FX / no interpolation) ─────────────────────────────────────
class PortfolioCurveTests(TestCase):
    def test_zero_points_building(self):
        u = _user("pc0"); a1 = _acct(u); a2 = _acct(u)
        c = EQ.portfolio_equity_curve([a1, a2], rate_source=FX.NullFxRateSource())
        self.assertEqual(c["state"], "BUILDING"); self.assertEqual(c["points"], [])

    def test_two_accounts_complete_point_sums_usd(self):
        u = _user("pc1"); a1 = _acct(u); a2 = _acct(u)
        t = timezone.now()
        _snap(a1, at=t, equity=1000); _snap(a2, at=t, equity=2000)
        c = EQ.portfolio_equity_curve([a1, a2], rate_source=FX.NullFxRateSource())
        self.assertTrue(c["points"])
        p = c["points"][-1]
        self.assertEqual(p["equity_usd"], 3000.0); self.assertEqual(p["basis"], "USD"); self.assertTrue(p["complete"])

    def test_missing_account_makes_point_partial(self):
        u = _user("pc2"); a1 = _acct(u); a2 = _acct(u)
        t = timezone.now()
        _snap(a1, at=t, equity=1000)                              # a2 has NO snapshot near t
        c = EQ.portfolio_equity_curve([a1, a2], rate_source=FX.NullFxRateSource())
        p = c["points"][-1]
        self.assertEqual(p["basis"], "PARTIAL"); self.assertFalse(p["complete"])
        self.assertIn(a2.id, p["stale_accounts"]); self.assertEqual(p["equity_usd"], 1000.0)

    def test_stale_account_excluded_and_flagged(self):
        u = _user("pc3"); a1 = _acct(u); a2 = _acct(u)
        t = timezone.now()
        _snap(a1, at=t, equity=1000)
        _snap(a2, at=t - timedelta(hours=2), equity=5000)        # a2's only snapshot is far stale for bucket t
        c = EQ.portfolio_equity_curve([a1, a2], rate_source=FX.NullFxRateSource())
        last = c["points"][-1]
        self.assertEqual(last["basis"], "PARTIAL"); self.assertIn(a2.id, last["stale_accounts"])
        self.assertEqual(last["equity_usd"], 1000.0)             # stale a2 NOT summed

    def test_non_usd_without_rate_is_partial(self):
        u = _user("pc4"); usd = _acct(u); gbp = _acct(u, currency="GBP")
        t = timezone.now()
        _snap(usd, at=t, equity=1000); _snap(gbp, at=t, equity=800, currency="GBP")
        c = EQ.portfolio_equity_curve([usd, gbp], rate_source=FX.NullFxRateSource())
        p = c["points"][-1]
        self.assertEqual(p["basis"], "PARTIAL"); self.assertIn(gbp.id, p["stale_accounts"])
        self.assertEqual(p["equity_usd"], 1000.0)                # GBP excluded (no historical rate), never summed

    def test_nonusd_snapshot_partial_when_account_currency_unset(self):
        # PROD reality (H-CURRENCY): account_currency is NULL, but the SNAPSHOT row carries GBP. The curve must
        # convert using the snapshot's native currency and degrade to PARTIAL, not fabricate GBP as USD.
        u = _user("pc6"); usd = _acct(u); gbp = _acct(u)      # both account_currency NULL (not set)
        t = timezone.now()
        _snap(usd, at=t, equity=1000); _snap(gbp, at=t, equity=800, currency="GBP")
        c = EQ.portfolio_equity_curve([usd, gbp], rate_source=FX.NullFxRateSource())
        p = c["points"][-1]
        self.assertEqual(p["basis"], "PARTIAL"); self.assertIn(gbp.id, p["stale_accounts"])
        self.assertEqual(p["equity_usd"], 1000.0)             # GBP snapshot excluded, never summed as USD

    def test_newer_balance_only_row_does_not_shadow_older_equity(self):
        u = _user("pc7"); a = _acct(u)
        t = timezone.now()
        _snap(a, at=t - timedelta(minutes=5), equity=1000)    # older full observation
        AccountEquitySnapshot.objects.create(                 # newer balance-only (equity=None) row
            trading_account_id=a.id, observed_at=t, account_currency="USD",
            balance=Decimal("1000"), equity=None, observed_login=a.account_number,
            observed_server=a.broker_server.server_name, source="test")
        c = EQ.portfolio_equity_curve([a], rate_source=FX.NullFxRateSource())
        self.assertTrue(c["points"]); self.assertTrue(all(p["equity_usd"] == 1000.0 for p in c["points"]))
        self.assertTrue(all(p["present"] == 1 for p in c["points"]))   # usable equity NOT dropped as missing

    def test_state_partial_when_snaps_exist_but_unusable(self):
        u = _user("pc8"); a = _acct(u)                        # account_currency NULL
        _snap(a, at=timezone.now(), equity=800, currency="GBP")  # unconvertible -> no usable point
        c = EQ.portfolio_equity_curve([a], rate_source=FX.NullFxRateSource())
        self.assertEqual(c["points"], []); self.assertEqual(c["state"], "PARTIAL")   # NOT "BUILDING"

    def test_no_fake_interpolation_between_points(self):
        # Two observations 30min apart -> at most a handful of buckets; equity values are only the observed ones.
        u = _user("pc5"); a = _acct(u)
        t = timezone.now() - timedelta(minutes=30)
        _snap(a, at=t, equity=1000); _snap(a, at=t + timedelta(minutes=30), equity=1000)
        c = EQ.portfolio_equity_curve([a], rate_source=FX.NullFxRateSource())
        self.assertTrue(all(p["equity_usd"] == 1000.0 for p in c["points"]))  # never a fabricated in-between value


# ── Endpoint (IDOR / scope) ───────────────────────────────────────────────────────────────────────────────────
class CurveEndpointTests(TestCase):
    def setUp(self):
        self.u = _user("e1"); self.other = _user("e1b")
        self.a = _acct(self.u); self.foreign = _acct(self.other)
        self.c = APIClient(); self.c.force_authenticate(self.u)

    def test_all_scope_building_when_empty(self):
        r = self.c.get("/api/analytics/portfolio/equity-curve/")
        self.assertEqual(r.status_code, 200); self.assertEqual(r.json()["state"], "BUILDING")

    def test_account_scope_isolation(self):
        _snap(self.a, at=timezone.now(), equity=500)
        r = self.c.get(f"/api/analytics/portfolio/equity-curve/?scope={self.a.id}")
        self.assertEqual(r.status_code, 200); self.assertEqual(r.json()["account_id"], self.a.id)

    def test_foreign_account_404(self):
        r = self.c.get(f"/api/analytics/portfolio/equity-curve/?scope={self.foreign.id}")
        self.assertEqual(r.status_code, 404)


# ── Broker-time normalisation (DST-aware; fail-safe) ──────────────────────────────────────────────────────────
class BrokerTimeTests(TestCase):
    TZ = "Europe/Bucharest"          # EET (UTC+2 winter) / EEST (UTC+3 summer) — the classic MT5 EET server

    def test_unset_tz_fails_safe_none(self):
        self.assertIsNone(deal_time_to_true_utc(1_790_584_502, ""))
        self.assertIsNone(deal_time_to_true_utc(1_790_584_502, None))
        self.assertIsNone(broker_offset_hours(""))

    def test_summer_offset_is_plus_3(self):
        # 2026-09-28 is EEST (summer, UTC+3).
        at = datetime(2026, 9, 28, 12, 0, tzinfo=_tz.utc)
        self.assertEqual(broker_offset_hours(self.TZ, at), 3.0)

    def test_winter_offset_is_plus_2_dst(self):
        # 2026-01-15 is EET (winter, UTC+2) — proves DST awareness, not a fixed +3.
        at = datetime(2026, 1, 15, 12, 0, tzinfo=_tz.utc)
        self.assertEqual(broker_offset_hours(self.TZ, at), 2.0)

    def test_deal_time_broker_wall_to_true_utc_summer(self):
        # A broker wall-clock of 08:35:02 on 2026-09-28 (EEST +3) -> true UTC 05:35:02.
        import calendar
        wall = datetime(2026, 9, 28, 8, 35, 2)
        ts = calendar.timegm(wall.timetuple())               # unix that utcfromtimestamp decodes back to the wall clock
        got = deal_time_to_true_utc(ts, self.TZ)
        self.assertEqual(got, datetime(2026, 9, 28, 5, 35, 2, tzinfo=_tz.utc))

    def test_deal_time_true_utc_winter(self):
        import calendar
        wall = datetime(2026, 1, 15, 8, 35, 0)               # EET +2 -> true UTC 06:35
        ts = calendar.timegm(wall.timetuple())
        self.assertEqual(deal_time_to_true_utc(ts, self.TZ), datetime(2026, 1, 15, 6, 35, tzinfo=_tz.utc))

    def test_no_double_conversion(self):
        # Converting a value already in true UTC via broker_wall_to_true_utc uses the wall clock exactly once.
        wall_mislabeled_utc = datetime(2026, 9, 28, 8, 35, 2, tzinfo=_tz.utc)   # GuvFX's stored (mislabeled) value
        got = broker_wall_to_true_utc(wall_mislabeled_utc, self.TZ)
        self.assertEqual(got, datetime(2026, 9, 28, 5, 35, 2, tzinfo=_tz.utc))  # shifted once by +3, not twice

    def test_midnight_boundary_moves_day_summer(self):
        import calendar
        wall = datetime(2026, 9, 29, 0, 30, 0)               # broker 00:30 -> true UTC 2026-09-28 21:30 (prev day)
        ts = calendar.timegm(wall.timetuple())
        got = deal_time_to_true_utc(ts, self.TZ)
        self.assertEqual(got.date(), datetime(2026, 9, 28).date())
        self.assertEqual(got, datetime(2026, 9, 28, 21, 30, tzinfo=_tz.utc))
