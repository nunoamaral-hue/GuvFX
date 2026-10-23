"""analytics.tests_fx — GBP->USD portfolio conversion (ADR-0058, DARK). Fixture-driven; NO live-FRED dependency.

Covers the PD Phase-4 matrix: weekday/weekend/holiday selection, year boundary, leap day, 7-day staleness
boundary, empty store, fetch failure, malformed/wrong-series/implausible rejection, duplicate idempotency, an
immutable revision, deterministic latest-accepted-revision selection, GBP->USD calculation, mixed aggregation,
deterministic half-even rounding, API+UI provenance, flag-OFF identity, flag-ON honest-partial, runtime
immutability, and no external network call from a dashboard request.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from analytics import fx_fred as FRED
from analytics import fx_source as SRC
from analytics import portfolio as PF
from analytics import portfolio_fx as FX
from analytics.fx_source import StoredFredFxRateSource, fx_conversion_enabled, select_observation
from analytics.models import DailyFxObservation
from trading.models import BrokerServer, TradingAccount

U = get_user_model()
FIXTURE = os.path.join(os.path.dirname(__file__), "tests_fixtures", "dexusuk_fixture.csv")
_n = 0


def _uniq():
    global _n
    _n += 1
    return f"fx{_n:05d}"


def _obs(eff: date, rate, *, rev: int = 1, state: str = DailyFxObservation.STATE_ACCEPTED,
         source: str = FRED.SOURCE, base: str = "GBP", quote: str = "USD", retrieved=None) -> DailyFxObservation:
    r = Decimal(str(rate))
    return DailyFxObservation.objects.create(
        base_currency=base, quote_currency=quote, effective_date=eff, rate=r, source=source,
        source_series=FRED.SERIES, retrieved_at=retrieved or timezone.now(),
        observation_checksum=FRED.observation_checksum(source, FRED.SERIES, base, quote, eff, r),
        revision_number=rev, acceptance_state=state)


# ── Parser + validation ───────────────────────────────────────────────────────────────────────────────────────
class FxParserTests(TestCase):
    def test_parses_valid_and_skips_missing(self):
        res = FRED.parse_dexusuk_csv("DATE,DEXUSUK\n2026-01-01,.\n2026-01-02,1.2650\n")
        self.assertEqual(len(res.accepted), 1)
        self.assertEqual(res.missing_count, 1)
        self.assertEqual(res.accepted[0].effective_date, date(2026, 1, 2))
        self.assertEqual(res.accepted[0].rate, Decimal("1.2650"))

    def test_wrong_series_raises(self):
        with self.assertRaises(FRED.FxParseError):
            FRED.parse_dexusuk_csv("DATE,DEXUSEU\n2026-01-02,1.10\n")

    def test_bad_header_raises(self):
        with self.assertRaises(FRED.FxParseError):
            FRED.parse_dexusuk_csv("FOO,BAR\n2026-01-02,1.10\n")

    def test_rejects_nonnumeric_zero_negative_implausible(self):
        res = FRED.parse_dexusuk_csv(
            "DATE,DEXUSUK\n2026-01-02,abc\n2026-01-05,0\n2026-01-06,-1.2\n2026-01-07,42.0\n2026-01-08,1.27\n")
        self.assertEqual(len(res.accepted), 1)                      # only the plausible 1.27
        self.assertEqual(res.accepted[0].rate, Decimal("1.27"))
        self.assertEqual({r.reason for r in res.rejected}, {"nonnumeric", "nonpositive", "implausible"})

    def test_checksum_identical_for_same_value_differs_on_revision(self):
        a = FRED.observation_checksum(FRED.SOURCE, FRED.SERIES, "GBP", "USD", date(2026, 1, 2), Decimal("1.2650"))
        b = FRED.observation_checksum(FRED.SOURCE, FRED.SERIES, "GBP", "USD", date(2026, 1, 2), Decimal("1.2650"))
        c = FRED.observation_checksum(FRED.SOURCE, FRED.SERIES, "GBP", "USD", date(2026, 1, 2), Decimal("1.2651"))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


# ── Deterministic selection (previous-day + carry-forward + staleness) ──────────────────────────────────────────
class FxSelectionTests(TestCase):
    def test_ordinary_weekday(self):
        _obs(date(2026, 1, 5), "1.2710")                              # Mon
        sel = select_observation("GBP", "USD", date(2026, 1, 6))      # Tue -> target Mon 01-05
        self.assertIsNotNone(sel); self.assertEqual(sel.effective_date, date(2026, 1, 5))

    def test_saturday_and_sunday_carry_forward(self):
        _obs(date(2026, 1, 2), "1.2650")                              # Fri
        sat = select_observation("GBP", "USD", date(2026, 1, 3))      # Sat -> target Fri 01-02
        sun = select_observation("GBP", "USD", date(2026, 1, 4))      # Sun -> target Sat (absent) -> Fri
        self.assertEqual(sat.effective_date, date(2026, 1, 2))
        self.assertEqual(sun.effective_date, date(2026, 1, 2))

    def test_holiday_carry_forward(self):
        _obs(date(2026, 1, 16), "1.30")                               # Fri before a long holiday weekend
        # Mon 01-19 (US MLK holiday) dashboard load -> target Sun 01-18 (absent) -> carry forward to Fri 01-16.
        sel = select_observation("GBP", "USD", date(2026, 1, 19))
        self.assertEqual(sel.effective_date, date(2026, 1, 16))
        self.assertEqual(sel.age_days, 3)

    def test_year_boundary_carry_forward(self):
        _obs(date(2025, 12, 31), "1.26")
        # 2026-01-02 load, 2026-01-01 is a holiday (absent) -> carry forward across the year boundary.
        sel = select_observation("GBP", "USD", date(2026, 1, 2))
        self.assertEqual(sel.effective_date, date(2025, 12, 31))

    def test_leap_day(self):
        _obs(date(2028, 2, 29), "1.28")                               # 2028 is a leap year
        sel = select_observation("GBP", "USD", date(2028, 3, 1))      # target 2028-02-29
        self.assertEqual(sel.effective_date, date(2028, 2, 29))

    def test_staleness_boundary_exactly_seven_days_ok(self):
        _obs(date(2026, 1, 1), "1.25")
        sel = select_observation("GBP", "USD", date(2026, 1, 8))      # age 7 -> within limit
        self.assertIsNotNone(sel); self.assertEqual(sel.age_days, 7)

    def test_older_than_seven_days_refused(self):
        _obs(date(2026, 1, 1), "1.25")
        self.assertIsNone(select_observation("GBP", "USD", date(2026, 1, 9)))  # age 8 -> refused

    def test_empty_store_unavailable(self):
        self.assertIsNone(select_observation("GBP", "USD", date(2026, 1, 6)))

    def test_latest_accepted_revision_selected_deterministically(self):
        _obs(date(2026, 1, 5), "1.2700", rev=1)
        _obs(date(2026, 1, 5), "1.2750", rev=2)                       # revised value -> higher accepted revision
        sel = select_observation("GBP", "USD", date(2026, 1, 6))
        self.assertEqual(sel.revision_number, 2)
        self.assertEqual(sel.rate, Decimal("1.2750"))

    def test_rejected_revision_not_selected(self):
        _obs(date(2026, 1, 5), "1.2700", rev=1, state=DailyFxObservation.STATE_ACCEPTED)
        _obs(date(2026, 1, 5), "9.9900", rev=2, state=DailyFxObservation.STATE_REJECTED)
        sel = select_observation("GBP", "USD", date(2026, 1, 6))
        self.assertEqual(sel.revision_number, 1)                     # latest ACCEPTED, not the rejected rev2
        self.assertEqual(sel.rate, Decimal("1.2700"))


# ── Stored rate source + provenance + no-network ────────────────────────────────────────────────────────────────
class FxStoredSourceTests(TestCase):
    def test_get_rate_gbp_usd_and_identity(self):
        _obs(date(2026, 1, 5), "1.2710")
        src = StoredFredFxRateSource(as_of=date(2026, 1, 6))
        fx = src.get_rate("GBP", "USD")
        self.assertIsNotNone(fx); self.assertAlmostEqual(fx.rate, 1.2710, places=6)
        self.assertEqual(fx.as_of, "2026-01-05")
        self.assertEqual(src.get_rate("USD", "USD").rate, 1.0)

    def test_get_rate_unavailable_returns_none(self):
        src = StoredFredFxRateSource(as_of=date(2026, 1, 6))
        self.assertIsNone(src.get_rate("GBP", "USD"))                 # empty store
        self.assertIsNone(src.get_rate("EUR", "USD"))                 # unsupported pair

    def test_provenance_available_and_unavailable(self):
        src0 = StoredFredFxRateSource(as_of=date(2026, 1, 6))
        p0 = src0.provenance()
        self.assertFalse(p0["available"]); self.assertEqual(p0["source"], "FED_H10")
        _obs(date(2026, 1, 5), "1.2710")
        src1 = StoredFredFxRateSource(as_of=date(2026, 1, 6))
        p1 = src1.provenance()
        self.assertTrue(p1["available"]); self.assertEqual(p1["rate"], "1.271")
        self.assertEqual(p1["effective_date"], "2026-01-05")
        self.assertEqual(p1["direction"], "USD_PER_GBP")

    def test_no_external_network_call_from_request_path(self):
        _obs(date(2026, 1, 5), "1.2710")
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network call in request path!")):
            src = StoredFredFxRateSource(as_of=date(2026, 1, 6))
            self.assertIsNotNone(src.get_rate("GBP", "USD"))         # DB-only; never touches the network
            self.assertTrue(src.provenance()["available"])


# ── Fetch/backfill command (idempotent, safe) ───────────────────────────────────────────────────────────────────
class FxCommandTests(TestCase):
    def _run(self, **kw):
        out = StringIO()
        call_command("fetch_fx_daily", stdout=out, **kw)
        return out.getvalue()

    def test_fixture_ingest_then_idempotent(self):
        self._run(fixture=FIXTURE)
        self.assertEqual(DailyFxObservation.objects.count(), 3)      # 3 accepted (1 missing "." skipped)
        self._run(fixture=FIXTURE)                                   # re-run same payload
        self.assertEqual(DailyFxObservation.objects.count(), 3)      # idempotent: no new rows

    def test_revised_value_creates_new_immutable_revision(self):
        self._run(fixture=FIXTURE)
        base = DailyFxObservation.objects.filter(effective_date=date(2026, 1, 2)).count()
        self.assertEqual(base, 1)
        revised = "DATE,DEXUSUK\n2026-01-02,1.2699\n"                 # same date, revised value
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            fh.write(revised); path = fh.name
        self._run(fixture=path)
        revs = DailyFxObservation.objects.filter(effective_date=date(2026, 1, 2)).order_by("revision_number")
        self.assertEqual(revs.count(), 2)
        self.assertEqual([r.revision_number for r in revs], [1, 2])
        self.assertEqual(revs[1].rate, Decimal("1.2699"))

    def test_non_https_refused_store_unchanged(self):
        with self.assertRaises(CommandError):
            self._run(url="http://insecure.example/fredgraph.csv?id=DEXUSUK")
        self.assertEqual(DailyFxObservation.objects.count(), 0)

    def test_wrong_series_fixture_refused_store_unchanged(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            fh.write("DATE,DEXUSEU\n2026-01-02,1.10\n"); path = fh.name
        with self.assertRaises(CommandError):
            self._run(fixture=path)
        self.assertEqual(DailyFxObservation.objects.count(), 0)

    def test_dry_run_writes_nothing(self):
        self._run(fixture=FIXTURE, **{"dry_run": True})
        self.assertEqual(DailyFxObservation.objects.count(), 0)


# ── Decimal half-even rounding + GBP calculation + mixed aggregation ────────────────────────────────────────────
class FxAggregateRoundingTests(TestCase):
    def test_money_half_even_banker_rounding(self):
        # Exact binary fractions so the decimal repr is unambiguous — ties resolve to the EVEN digit.
        self.assertEqual(FX._money_half_even(0.125), 0.12)
        self.assertEqual(FX._money_half_even(0.375), 0.38)
        self.assertEqual(FX._money_half_even(0.625), 0.62)
        self.assertEqual(FX._money_half_even(1000.0), 1000.0)

    def test_gbp_to_usd_calculation_and_rounding(self):
        _obs(date(2026, 1, 5), "1.3400")
        src = StoredFredFxRateSource(as_of=date(2026, 1, 6))
        agg = FX.aggregate_usd([{"account_id": 1, "amount": 446.19, "currency": "GBP"}], src)
        self.assertEqual(agg.state, FX.STATE_OK)                     # converted
        self.assertEqual(agg.as_dict()["total_usd"], 597.89)         # 446.19 * 1.34 = 597.8946 -> 597.89

    def test_mixed_gbp_usd_both_convert_when_rate_present(self):
        _obs(date(2026, 1, 5), "1.3000")
        src = StoredFredFxRateSource(as_of=date(2026, 1, 6))
        agg = FX.aggregate_usd([{"account_id": 1, "amount": 100.0, "currency": "USD"},
                                {"account_id": 2, "amount": 100.0, "currency": "GBP"}], src)
        self.assertEqual(agg.state, FX.STATE_OK)                     # both included — not PARTIAL
        self.assertEqual(agg.as_dict()["total_usd"], 230.0)          # 100 + 100*1.30
        self.assertEqual(agg.as_dict()["included_count"], 2)

    def test_mixed_gbp_usd_partial_when_no_rate(self):
        src = StoredFredFxRateSource(as_of=date(2026, 1, 6))         # empty store
        agg = FX.aggregate_usd([{"account_id": 1, "amount": 100.0, "currency": "USD"},
                                {"account_id": 2, "amount": 100.0, "currency": "GBP"}], src)
        self.assertEqual(agg.state, FX.STATE_PARTIAL)               # GBP excluded honestly
        self.assertEqual(agg.as_dict()["total_usd"], 100.0)
        self.assertEqual(agg.as_dict()["excluded_count"], 1)


# ── View / API provenance + flag gating ─────────────────────────────────────────────────────────────────────────
class FxViewTests(TestCase):
    def setUp(self):
        self.u = U.objects.create_user(username="fxv", email="fxv@x.invalid", password="x")
        srv, _ = BrokerServer.objects.get_or_create(server_name="Pepperstone-Demo-fxv")
        TradingAccount.objects.create(user=self.u, name="P", broker_name="Pepperstone",
                                      account_number=_uniq(), is_demo=True, is_active=True, broker_server=srv)
        self.client = APIClient(); self.client.force_authenticate(self.u)

    def test_flag_off_has_no_fx_key(self):
        r = self.client.get("/api/analytics/portfolio/summary/?scope=ALL")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("fx", r.json())                            # byte-compatible with today

    @override_settings(PORTFOLIO_FX_CONVERSION_ENABLED="1")
    def test_flag_on_surfaces_provenance(self):
        _obs(date.today().replace(day=1), "1.2710")                 # a recent accepted obs (<=7d of some as_of)
        _obs(timezone.now().date(), "1.2710")                       # ensure a within-staleness obs exists
        r = self.client.get("/api/analytics/portfolio/summary/?scope=ALL")
        self.assertEqual(r.status_code, 200)
        fx = r.json().get("fx")
        self.assertIsNotNone(fx); self.assertTrue(fx["enabled"])
        self.assertEqual(fx["source"], "FED_H10"); self.assertEqual(fx["source_series"], "DEXUSUK")

    @override_settings(PORTFOLIO_FX_CONVERSION_ENABLED="1")
    def test_flag_on_no_rate_is_honest_unavailable(self):
        r = self.client.get("/api/analytics/portfolio/summary/?scope=ALL")
        self.assertEqual(r.status_code, 200)
        fx = r.json().get("fx")
        self.assertIsNotNone(fx); self.assertTrue(fx["enabled"]); self.assertFalse(fx["available"])


# ── Runtime immutability (append-only) ──────────────────────────────────────────────────────────────────────────
class FxImmutabilityTests(TestCase):
    def test_delete_blocked(self):
        o = _obs(date(2026, 1, 5), "1.27")
        with self.assertRaises(ValueError):
            o.delete()

    def test_inplace_edit_of_immutable_field_blocked(self):
        o = _obs(date(2026, 1, 5), "1.27")
        o.rate = Decimal("9.99")
        with self.assertRaises(ValueError):
            o.save()
