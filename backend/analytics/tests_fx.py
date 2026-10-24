"""analytics.tests_fx — GBP->USD portfolio conversion (ADR-0058, DARK). Fixture-driven; NO live-FRED dependency.

Covers the PD Phase-4 matrix plus the F1–F5 hardening: DB-enforced append-only (negative controls for ORM/raw
UPDATE+DELETE and a SELECT-only runtime role), Europe/London previous-day boundary (today/future never selected,
DST, carry-forward, 7-day staleness from the target date), revision/concurrency + raw-line reproducibility,
DB-level check constraints, Decimal half-even rounding, API/UI provenance, flag-OFF identity + zero FX queries,
no network from a dashboard request, and no FX exception breaking the summary.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from datetime import timezone as dt_tz
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from django.conf import settings

from analytics import fx_fred as FRED
from analytics import portfolio as PF
from analytics import portfolio_fx as FX
from analytics.fx_db_router import FxIngestRouter
from analytics.fx_ingest import FX_INGEST_ALIAS, FxIngestConfigError, append_fx_observation, resolve_fx_ingest_alias
from analytics.fx_source import StoredFredFxRateSource, governed_today, select_observation
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
        revision_number=rev, acceptance_state=state, source_url="https://example/fredgraph.csv?id=DEXUSUK",
        raw_line=f"{eff.isoformat()},{r}")


def _valid_kwargs(**overrides):
    r = Decimal("1.2700")
    eff = overrides.pop("effective_date", date(2026, 1, 5))
    base = overrides.get("base_currency", "GBP")
    quote = overrides.get("quote_currency", "USD")
    kw = dict(base_currency=base, quote_currency=quote, effective_date=eff, rate=r, source=FRED.SOURCE,
              source_series=FRED.SERIES, retrieved_at=timezone.now(),
              observation_checksum=FRED.observation_checksum(FRED.SOURCE, FRED.SERIES, base, quote, eff, r),
              revision_number=1, acceptance_state=DailyFxObservation.STATE_ACCEPTED)
    kw.update(overrides)
    return kw


# ── Parser + validation ───────────────────────────────────────────────────────────────────────────────────────
class FxParserTests(TestCase):
    def test_parses_valid_skips_missing_and_keeps_raw_line(self):
        res = FRED.parse_dexusuk_csv("DATE,DEXUSUK\n2026-01-01,.\n2026-01-02,1.2650\n")
        self.assertEqual(len(res.accepted), 1)
        self.assertEqual(res.missing_count, 1)
        self.assertEqual(res.accepted[0].effective_date, date(2026, 1, 2))
        self.assertEqual(res.accepted[0].rate, Decimal("1.2650"))
        self.assertEqual(res.accepted[0].raw_line, "2026-01-02,1.2650")

    def test_wrong_series_raises(self):
        with self.assertRaises(FRED.FxParseError):
            FRED.parse_dexusuk_csv("DATE,DEXUSEU\n2026-01-02,1.10\n")

    def test_bad_header_raises(self):
        with self.assertRaises(FRED.FxParseError):
            FRED.parse_dexusuk_csv("FOO,BAR\n2026-01-02,1.10\n")

    def test_rejects_nonnumeric_zero_negative_implausible(self):
        res = FRED.parse_dexusuk_csv(
            "DATE,DEXUSUK\n2026-01-02,abc\n2026-01-05,0\n2026-01-06,-1.2\n2026-01-07,42.0\n2026-01-08,1.27\n")
        self.assertEqual(len(res.accepted), 1)
        self.assertEqual(res.accepted[0].rate, Decimal("1.27"))
        self.assertEqual({r.reason for r in res.rejected}, {"nonnumeric", "nonpositive", "implausible"})

    def test_checksum_per_observation_scope(self):
        a = FRED.observation_checksum(FRED.SOURCE, FRED.SERIES, "GBP", "USD", date(2026, 1, 2), Decimal("1.2650"))
        same = FRED.observation_checksum(FRED.SOURCE, FRED.SERIES, "GBP", "USD", date(2026, 1, 2), Decimal("1.2650"))
        revised = FRED.observation_checksum(FRED.SOURCE, FRED.SERIES, "GBP", "USD", date(2026, 1, 2), Decimal("1.2651"))
        other_day = FRED.observation_checksum(FRED.SOURCE, FRED.SERIES, "GBP", "USD", date(2026, 1, 3), Decimal("1.2650"))
        self.assertEqual(a, same)
        self.assertNotEqual(a, revised)   # a revised value for the SAME day differs
        self.assertNotEqual(a, other_day)  # checksum is per-observation (date-scoped), not whole-payload


# ── F2: governed timezone + previous-day + staleness ────────────────────────────────────────────────────────────
class FxSelectionTests(TestCase):
    def test_governed_today_uses_europe_london_dst(self):
        # Summer (BST = UTC+1): 23:30 UTC is already the NEXT London day — proves the governed tz shifts the date.
        self.assertEqual(governed_today(datetime(2026, 6, 15, 23, 30, tzinfo=dt_tz.utc)), date(2026, 6, 16))
        # Winter (GMT = UTC): same instant-of-day stays the same London date.
        self.assertEqual(governed_today(datetime(2026, 1, 15, 23, 30, tzinfo=dt_tz.utc)), date(2026, 1, 15))
        # Across the spring-forward boundary (last Sunday March 2026 = 29th): still the correct London date.
        self.assertEqual(governed_today(datetime(2026, 3, 29, 1, 30, tzinfo=dt_tz.utc)), date(2026, 3, 29))

    def test_today_exists_but_yesterday_selected(self):
        _obs(date(2026, 1, 5), "1.2700")   # yesterday
        _obs(date(2026, 1, 6), "9.9900")   # TODAY — must never be selected
        sel = select_observation("GBP", "USD", date(2026, 1, 6))
        self.assertEqual(sel.effective_date, date(2026, 1, 5))
        self.assertEqual(sel.rate, Decimal("1.2700"))

    def test_future_dated_never_selected(self):
        _obs(date(2026, 1, 10), "1.30")    # future relative to current_date 2026-01-06
        self.assertIsNone(select_observation("GBP", "USD", date(2026, 1, 6)))

    def test_monday_selects_friday(self):
        _obs(date(2026, 1, 2), "1.2650")   # Fri
        self.assertEqual(select_observation("GBP", "USD", date(2026, 1, 5)).effective_date, date(2026, 1, 2))  # Mon

    def test_tuesday_after_monday_us_holiday(self):
        _obs(date(2026, 1, 16), "1.30")    # Fri; Mon 01-19 holiday absent
        sel = select_observation("GBP", "USD", date(2026, 1, 20))  # Tue -> target Mon 01-19 (absent) -> Fri 01-16
        self.assertEqual(sel.effective_date, date(2026, 1, 16))

    def test_year_boundary_carry_forward(self):
        _obs(date(2025, 12, 31), "1.26")
        sel = select_observation("GBP", "USD", date(2026, 1, 2))  # 01-01 holiday absent -> carry across year
        self.assertEqual(sel.effective_date, date(2025, 12, 31))

    def test_leap_day(self):
        _obs(date(2028, 2, 29), "1.28")
        self.assertEqual(select_observation("GBP", "USD", date(2028, 3, 1)).effective_date, date(2028, 2, 29))

    def test_seven_day_boundary_from_target_ok(self):
        # current_date 2026-01-09 -> target 2026-01-08; eff 2026-01-01 -> age = 7 (within limit).
        _obs(date(2026, 1, 1), "1.25")
        sel = select_observation("GBP", "USD", date(2026, 1, 9))
        self.assertIsNotNone(sel); self.assertEqual(sel.age_days, 7)

    def test_older_than_seven_days_from_target_refused(self):
        # current_date 2026-01-10 -> target 2026-01-09; eff 2026-01-01 -> age = 8 -> refused.
        _obs(date(2026, 1, 1), "1.25")
        self.assertIsNone(select_observation("GBP", "USD", date(2026, 1, 10)))

    def test_empty_store_unavailable(self):
        self.assertIsNone(select_observation("GBP", "USD", date(2026, 1, 6)))

    def test_latest_accepted_revision_selected(self):
        _obs(date(2026, 1, 5), "1.2700", rev=1)
        _obs(date(2026, 1, 5), "1.2750", rev=2)
        sel = select_observation("GBP", "USD", date(2026, 1, 6))
        self.assertEqual(sel.revision_number, 2); self.assertEqual(sel.rate, Decimal("1.2750"))

    def test_rejected_revision_not_selected(self):
        _obs(date(2026, 1, 5), "1.2700", rev=1, state=DailyFxObservation.STATE_ACCEPTED)
        _obs(date(2026, 1, 5), "9.9900", rev=2, state=DailyFxObservation.STATE_REJECTED)
        sel = select_observation("GBP", "USD", date(2026, 1, 6))
        self.assertEqual(sel.revision_number, 1); self.assertEqual(sel.rate, Decimal("1.2700"))


# ── F1: database-enforced append-only (negative controls) ───────────────────────────────────────────────────────
class FxAppendOnlyTriggerTests(TestCase):
    def test_orm_bulk_update_blocked(self):
        o = _obs(date(2026, 1, 5), "1.27")
        with self.assertRaises(DatabaseError), transaction.atomic():
            DailyFxObservation.objects.filter(pk=o.pk).update(rate=Decimal("9.99"))

    def test_orm_delete_blocked(self):
        o = _obs(date(2026, 1, 5), "1.27")
        with self.assertRaises(DatabaseError), transaction.atomic():
            DailyFxObservation.objects.filter(pk=o.pk).delete()

    def test_raw_update_blocked(self):
        o = _obs(date(2026, 1, 5), "1.27")
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as c:
            c.execute("UPDATE analytics_dailyfxobservation SET rate = 9.99 WHERE id = %s", [o.pk])

    def test_raw_delete_blocked(self):
        o = _obs(date(2026, 1, 5), "1.27")
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as c:
            c.execute("DELETE FROM analytics_dailyfxobservation WHERE id = %s", [o.pk])

    def test_insert_new_revision_allowed(self):
        _obs(date(2026, 1, 5), "1.2700", rev=1)
        _obs(date(2026, 1, 5), "1.2750", rev=2)   # a correction is an INSERT — permitted
        self.assertEqual(DailyFxObservation.objects.filter(effective_date=date(2026, 1, 5)).count(), 2)


class FxRuntimeGrantTests(TransactionTestCase):
    """F1: prove the F-8 SELECT-only runtime boundary — a non-owner role granted only SELECT cannot
    INSERT/UPDATE/DELETE/TRUNCATE the FX store. Skipped (not failed) where the test DB user cannot CREATE ROLE;
    the grant boundary is then exercised under the F-8 cutover (ops/fx/grants_fx_observation.sql)."""

    def test_select_only_role_cannot_mutate_or_truncate(self):
        o = _obs(date(2026, 1, 5), "1.27")
        role = "guvfx_fx_ro_test"
        with connection.cursor() as c:
            try:
                c.execute("DROP ROLE IF EXISTS %s" % role)
                c.execute("CREATE ROLE %s NOLOGIN" % role)
            except Exception:
                self.skipTest("test DB user cannot CREATE ROLE; SELECT-only boundary verified under F-8 grants")
            try:
                c.execute("GRANT SELECT ON analytics_dailyfxobservation TO %s" % role)
                c.execute("SET ROLE %s" % role)
                c.execute("SELECT count(*) FROM analytics_dailyfxobservation")
                self.assertGreaterEqual(c.fetchone()[0], 1)                      # SELECT allowed
                for sql in [
                    "INSERT INTO analytics_dailyfxobservation (base_currency) VALUES ('GBP')",
                    "UPDATE analytics_dailyfxobservation SET rate = 9.99 WHERE id = %d" % o.pk,
                    "DELETE FROM analytics_dailyfxobservation WHERE id = %d" % o.pk,
                    "TRUNCATE analytics_dailyfxobservation",
                ]:
                    with self.assertRaises(Exception):
                        c.execute(sql)
                    c.execute("RESET ROLE"); c.execute("SET ROLE %s" % role)     # clear any aborted state
            finally:
                c.execute("RESET ROLE")
                c.execute("REVOKE ALL ON analytics_dailyfxobservation FROM %s" % role)
                c.execute("DROP ROLE IF EXISTS %s" % role)


# ── F3: revision + concurrency contract ─────────────────────────────────────────────────────────────────────────
class FxRevisionConcurrencyTests(TestCase):
    def setUp(self):
        # These tests exercise ingest LOGIC through the default test connection; route the command's ingestion
        # alias to 'default' (the dedicated-alias behaviour is covered by FxIngestAliasTests + the PG15 rehearsal).
        p = mock.patch("analytics.management.commands.fetch_fx_daily.resolve_fx_ingest_alias", return_value="default")
        p.start(); self.addCleanup(p.stop)

    def test_duplicate_identical_observation_is_idempotent(self):
        o1 = _obs(date(2026, 1, 5), "1.27", rev=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            # Same checksum (identical published value) + a new revision number still collides on the checksum
            # uniqueness — an identical observation can never create a second row.
            DailyFxObservation.objects.create(**_valid_kwargs(
                effective_date=date(2026, 1, 5), rate=Decimal("1.2700"),
                observation_checksum=o1.observation_checksum, revision_number=2))

    def test_revision_number_cannot_collide(self):
        _obs(date(2026, 1, 5), "1.2700", rev=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DailyFxObservation.objects.create(**_valid_kwargs(
                effective_date=date(2026, 1, 5), rate=Decimal("1.2750"),
                observation_checksum=FRED.observation_checksum(
                    FRED.SOURCE, FRED.SERIES, "GBP", "USD", date(2026, 1, 5), Decimal("1.2750")),
                revision_number=1))   # a different value but the SAME revision number -> rejected

    def test_raw_line_reconstructs_observation(self):
        out = StringIO()
        call_command("fetch_fx_daily", fixture=FIXTURE, stdout=out)
        row = DailyFxObservation.objects.get(effective_date=date(2026, 1, 2))
        self.assertEqual(row.raw_line, "2026-01-02,1.2650")
        self.assertTrue(row.source_url.startswith("fixture:"))
        d, v = row.raw_line.split(",")
        self.assertEqual(date.fromisoformat(d), row.effective_date)
        self.assertEqual(Decimal(v), row.rate)                      # reproduce the parsed observation from raw_line

    def test_unrelated_changed_row_does_not_revise_target(self):
        import tempfile
        a = "DATE,DEXUSUK\n2026-01-05,1.2600\n2026-01-06,1.2700\n"
        b = "DATE,DEXUSUK\n2026-01-05,1.2600\n2026-01-06,1.2800\n"   # only 01-06 changed
        for payload in (a, b):
            with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
                fh.write(payload); path = fh.name
            call_command("fetch_fx_daily", fixture=path, stdout=StringIO())
        self.assertEqual(DailyFxObservation.objects.filter(effective_date=date(2026, 1, 5)).count(), 1)  # no false revision
        self.assertEqual(DailyFxObservation.objects.filter(effective_date=date(2026, 1, 6)).count(), 2)  # legitimately revised


# ── F4: database check constraints ──────────────────────────────────────────────────────────────────────────────
class FxConstraintTests(TestCase):
    def _assert_rejected(self, **overrides):
        with self.assertRaises(IntegrityError), transaction.atomic():
            DailyFxObservation.objects.create(**_valid_kwargs(**overrides))

    def test_base_equals_quote_rejected(self):
        self._assert_rejected(base_currency="USD", quote_currency="USD")

    def test_unsupported_pair_rejected(self):
        self._assert_rejected(base_currency="EUR", quote_currency="USD")

    def test_nonpositive_rate_rejected(self):
        self._assert_rejected(rate=Decimal("0"))

    def test_nonpositive_revision_rejected(self):
        self._assert_rejected(revision_number=0)

    def test_bad_source_rejected(self):
        self._assert_rejected(source="HEARSAY")

    def test_bad_state_rejected(self):
        self._assert_rejected(acceptance_state="MAYBE")

    def test_bad_checksum_format_rejected(self):
        self._assert_rejected(observation_checksum="not-a-sha256")


# ── Stored source + provenance + no-network ─────────────────────────────────────────────────────────────────────
class FxStoredSourceTests(TestCase):
    def test_get_rate_gbp_usd_and_identity(self):
        _obs(date(2026, 1, 5), "1.2710")
        src = StoredFredFxRateSource(current_date=date(2026, 1, 6))
        fx = src.get_rate("GBP", "USD")
        self.assertIsNotNone(fx); self.assertAlmostEqual(fx.rate, 1.2710, places=6)
        self.assertEqual(fx.as_of, "2026-01-05")
        self.assertEqual(src.get_rate("USD", "USD").rate, 1.0)

    def test_get_rate_unavailable_returns_none(self):
        src = StoredFredFxRateSource(current_date=date(2026, 1, 6))
        self.assertIsNone(src.get_rate("GBP", "USD"))
        self.assertIsNone(src.get_rate("EUR", "USD"))

    def test_provenance_available_and_unavailable(self):
        p0 = StoredFredFxRateSource(current_date=date(2026, 1, 6)).provenance()
        self.assertFalse(p0["available"]); self.assertEqual(p0["governed_tz"], "Europe/London")
        _obs(date(2026, 1, 5), "1.2710")
        p1 = StoredFredFxRateSource(current_date=date(2026, 1, 6)).provenance()
        self.assertTrue(p1["available"]); self.assertEqual(p1["rate"], "1.271")
        self.assertEqual(p1["effective_date"], "2026-01-05"); self.assertEqual(p1["target_date"], "2026-01-05")
        self.assertTrue(p1["source_url"])

    def test_no_external_network_call_from_request_path(self):
        _obs(date(2026, 1, 5), "1.2710")
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network call in request path!")):
            src = StoredFredFxRateSource(current_date=date(2026, 1, 6))
            self.assertIsNotNone(src.get_rate("GBP", "USD"))
            self.assertTrue(src.provenance()["available"])

    def test_fx_read_failure_degrades_to_unavailable_not_raise(self):
        # F5: no FX fault can break the dashboard — a failed select degrades to honest unavailable.
        with mock.patch("analytics.fx_source.select_observation", side_effect=RuntimeError("db boom")):
            src = StoredFredFxRateSource(current_date=date(2026, 1, 6))
            self.assertIsNone(src.get_rate("GBP", "USD"))           # no exception escapes
            self.assertFalse(src.provenance()["available"])


# ── Fetch/backfill command ──────────────────────────────────────────────────────────────────────────────────────
class FxCommandTests(TestCase):
    def setUp(self):
        # Exercise command LOGIC via the default test connection; alias-routing is covered by FxIngestAliasTests.
        p = mock.patch("analytics.management.commands.fetch_fx_daily.resolve_fx_ingest_alias", return_value="default")
        p.start(); self.addCleanup(p.stop)

    def _run(self, **kw):
        out = StringIO(); call_command("fetch_fx_daily", stdout=out, **kw); return out.getvalue()

    def test_fixture_ingest_then_idempotent(self):
        self._run(fixture=FIXTURE)
        self.assertEqual(DailyFxObservation.objects.count(), 3)
        self._run(fixture=FIXTURE)
        self.assertEqual(DailyFxObservation.objects.count(), 3)

    def test_revised_value_creates_new_revision(self):
        self._run(fixture=FIXTURE)
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            fh.write("DATE,DEXUSUK\n2026-01-02,1.2699\n"); path = fh.name
        self._run(fixture=path)
        revs = DailyFxObservation.objects.filter(effective_date=date(2026, 1, 2)).order_by("revision_number")
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


# ── Decimal half-even rounding + GBP calc + mixed aggregation ───────────────────────────────────────────────────
class FxAggregateRoundingTests(TestCase):
    def test_money_half_even_banker_rounding(self):
        self.assertEqual(FX._money_half_even(0.125), 0.12)
        self.assertEqual(FX._money_half_even(0.375), 0.38)
        self.assertEqual(FX._money_half_even(0.625), 0.62)
        self.assertEqual(FX._money_half_even(1000.0), 1000.0)

    def test_gbp_to_usd_calculation_and_rounding(self):
        _obs(date(2026, 1, 5), "1.3400")
        src = StoredFredFxRateSource(current_date=date(2026, 1, 6))
        agg = FX.aggregate_usd([{"account_id": 1, "amount": 446.19, "currency": "GBP"}], src)
        self.assertEqual(agg.state, FX.STATE_OK)
        self.assertEqual(agg.as_dict()["total_usd"], 597.89)       # 446.19 * 1.34

    def test_mixed_gbp_usd_both_convert_when_rate_present(self):
        _obs(date(2026, 1, 5), "1.3000")
        src = StoredFredFxRateSource(current_date=date(2026, 1, 6))
        agg = FX.aggregate_usd([{"account_id": 1, "amount": 100.0, "currency": "USD"},
                                {"account_id": 2, "amount": 100.0, "currency": "GBP"}], src)
        self.assertEqual(agg.state, FX.STATE_OK)
        self.assertEqual(agg.as_dict()["total_usd"], 230.0); self.assertEqual(agg.as_dict()["included_count"], 2)

    def test_mixed_gbp_usd_partial_when_no_rate(self):
        src = StoredFredFxRateSource(current_date=date(2026, 1, 6))
        agg = FX.aggregate_usd([{"account_id": 1, "amount": 100.0, "currency": "USD"},
                                {"account_id": 2, "amount": 100.0, "currency": "GBP"}], src)
        self.assertEqual(agg.state, FX.STATE_PARTIAL)
        self.assertEqual(agg.as_dict()["total_usd"], 100.0); self.assertEqual(agg.as_dict()["excluded_count"], 1)


# ── View / API provenance + flag gating + F5 proofs ─────────────────────────────────────────────────────────────
class FxViewTests(TestCase):
    def setUp(self):
        self.u = U.objects.create_user(username="fxv", email="fxv@x.invalid", password="x")
        srv, _ = BrokerServer.objects.get_or_create(server_name="Pepperstone-Demo-fxv")
        TradingAccount.objects.create(user=self.u, name="P", broker_name="Pepperstone",
                                      account_number=_uniq(), is_demo=True, is_active=True, broker_server=srv)
        from rest_framework.test import APIClient
        self.client = APIClient(); self.client.force_authenticate(self.u)

    def test_flag_off_has_no_fx_key_and_zero_fx_queries(self):
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get("/api/analytics/portfolio/summary/?scope=ALL")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("fx", r.json())
        self.assertFalse(any("dailyfxobservation" in q["sql"].lower() for q in ctx.captured_queries))

    @override_settings(PORTFOLIO_FX_CONVERSION_ENABLED="1")
    def test_flag_on_surfaces_provenance(self):
        _obs(governed_today() - timedelta(days=1), "1.2710")    # yesterday (governed tz) -> selected
        r = self.client.get("/api/analytics/portfolio/summary/?scope=ALL")
        self.assertEqual(r.status_code, 200)
        fx = r.json().get("fx")
        self.assertIsNotNone(fx); self.assertTrue(fx["available"])
        self.assertEqual(fx["source"], "FED_H10"); self.assertEqual(fx["source_series"], "DEXUSUK")

    @override_settings(PORTFOLIO_FX_CONVERSION_ENABLED="1")
    def test_flag_on_no_rate_is_honest_unavailable(self):
        r = self.client.get("/api/analytics/portfolio/summary/?scope=ALL")
        self.assertEqual(r.status_code, 200)
        fx = r.json().get("fx")
        self.assertIsNotNone(fx); self.assertTrue(fx["enabled"]); self.assertFalse(fx["available"])

    @override_settings(PORTFOLIO_FX_CONVERSION_ENABLED="1")
    def test_no_network_from_summary_request(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network!")):
            r = self.client.get("/api/analytics/portfolio/summary/?scope=ALL")
        self.assertEqual(r.status_code, 200)

    @override_settings(PORTFOLIO_FX_CONVERSION_ENABLED="1")
    def test_fx_exception_does_not_break_summary(self):
        with mock.patch("analytics.fx_source.select_observation", side_effect=RuntimeError("boom")):
            r = self.client.get("/api/analytics/portfolio/summary/?scope=ALL")
        self.assertEqual(r.status_code, 200)   # summary still settles; honest unavailable

    @override_settings(PORTFOLIO_FX_CONVERSION_ENABLED="1")
    def test_equity_curve_endpoint_ok_under_flag(self):
        r = self.client.get("/api/analytics/portfolio/equity-curve/?scope=ALL")
        self.assertEqual(r.status_code, 200)   # curve stays on the USD-only source (point-in-time)


# ── F10: real concurrent-transaction races (threads + barrier + separate connections) ──────────────────────────
class _Rollback(Exception):
    pass


class FxConcurrencyTests(TransactionTestCase):
    """Two genuinely simultaneous writers (own DB connection each, synchronised by a Barrier) exercise the
    advisory-lock serialisation in ``append_fx_observation``. ``select_for_update`` cannot lock a not-yet-existing
    first row; the advisory transaction lock can, so a first-insert race yields exactly one row and corrections
    get distinct sequential revisions — with no duplicate revision number, no lost correction, and no unhandled
    IntegrityError reaching the caller."""

    def _run_pair(self, fn_a, fn_b):
        import threading
        barrier = threading.Barrier(2, timeout=20)
        results = {}

        def wrap(idx, fn):
            try:
                fn(barrier, results, idx)
            finally:
                connections.close_all()   # close THIS worker thread's connection

        ta = threading.Thread(target=wrap, args=(0, fn_a))
        tb = threading.Thread(target=wrap, args=(1, fn_b))
        ta.start(); tb.start(); ta.join(25); tb.join(25)
        return results

    @staticmethod
    def _ingest_fn(eff, rate_str):
        def _fn(barrier, results, idx):
            rate = Decimal(rate_str)
            cs = FRED.observation_checksum(FRED.SOURCE, FRED.SERIES, "GBP", "USD", eff, rate)
            barrier.wait()
            try:
                written, rev = append_fx_observation(
                    effective_date=eff, rate=rate, observation_checksum=cs, raw_line="%s,%s" % (eff, rate),
                    source_url="x", retrieved_at=timezone.now())
                results[idx] = ("ok", written, rev)
            except Exception as e:  # noqa: BLE001 — record any IntegrityError that escapes (should be none)
                results[idx] = ("err", type(e).__name__, str(e))
        return _fn

    def test_two_identical_first_ingestions_one_row(self):
        res = self._run_pair(self._ingest_fn(date(2026, 1, 5), "1.2600"),
                             self._ingest_fn(date(2026, 1, 5), "1.2600"))
        self.assertEqual(DailyFxObservation.objects.filter(effective_date=date(2026, 1, 5)).count(), 1)
        self.assertEqual(sorted(res.values()), sorted([("ok", True, 1), ("ok", False, None)]))

    def test_two_distinct_corrections_sequential_revisions(self):
        _obs(date(2026, 1, 5), "1.2600", rev=1)   # pre-existing accepted revision
        res = self._run_pair(self._ingest_fn(date(2026, 1, 5), "1.2700"),
                             self._ingest_fn(date(2026, 1, 5), "1.2800"))
        revs = sorted(DailyFxObservation.objects.filter(effective_date=date(2026, 1, 5))
                      .values_list("revision_number", flat=True))
        self.assertEqual(revs, [1, 2, 3])                               # distinct, sequential — no collision/loss
        self.assertEqual(sorted(r[2] for r in res.values()), [2, 3])
        self.assertFalse(any(r[0] == "err" for r in res.values()))      # no IntegrityError escaped

    def test_identical_duplicate_racing_a_correction(self):
        _obs(date(2026, 1, 5), "1.2600", rev=1)
        res = self._run_pair(self._ingest_fn(date(2026, 1, 5), "1.2600"),   # identical dup
                             self._ingest_fn(date(2026, 1, 5), "1.2900"))    # genuine correction
        self.assertEqual(DailyFxObservation.objects.filter(effective_date=date(2026, 1, 5)).count(), 2)
        rates = set(DailyFxObservation.objects.filter(effective_date=date(2026, 1, 5))
                    .values_list("rate", flat=True))
        self.assertEqual(rates, {Decimal("1.2600"), Decimal("1.2900")})

    def test_rollback_during_allocation_releases_lock(self):
        def roll(barrier, results, idx):
            eff = date(2026, 1, 5); rate = Decimal("9.9900")
            cs = FRED.observation_checksum(FRED.SOURCE, FRED.SERIES, "GBP", "USD", eff, rate)
            barrier.wait()
            try:
                # Allocate through the REAL path (advisory lock + next-revision), then roll the whole tx back —
                # the lock must release and leave no partial row, without any IntegrityError escaping.
                with transaction.atomic():
                    append_fx_observation(effective_date=eff, rate=rate, observation_checksum=cs,
                                          raw_line="%s,%s" % (eff, rate), source_url="x",
                                          retrieved_at=timezone.now())
                    raise _Rollback()
            except _Rollback:
                results[idx] = ("rolledback",)

        other = self._ingest_fn(date(2026, 1, 5), "1.2600")
        self._run_pair(roll, other)
        rows = DailyFxObservation.objects.filter(effective_date=date(2026, 1, 5))
        self.assertEqual(rows.count(), 1)                                # rolled-back insert left nothing
        self.assertEqual(rows.first().rate, Decimal("1.2600"))           # the committed writer landed one revision
        self.assertEqual(rows.first().revision_number, 1)


# ── A3: dedicated FX ingestion identity (fail-closed, routed, migration-guarded) ───────────────────────────────
class FxIngestAliasTests(TestCase):
    """The ingestion command must use the DEDICATED ``fx_ingest`` alias and FAIL CLOSED when it is absent — never
    silently fall back to the web-runtime ``default`` connection. (``patch.dict`` toggles the alias in
    ``settings.DATABASES`` without resetting live connections; the real end-to-end role behaviour is proven on the
    PG15 rehearsal.)"""

    def test_resolver_fail_closed_without_alias(self):
        with mock.patch.dict(settings.DATABASES):
            settings.DATABASES.pop(FX_INGEST_ALIAS, None)
            with self.assertRaises(FxIngestConfigError):
                resolve_fx_ingest_alias()

    def test_resolver_returns_dedicated_alias_when_configured(self):
        with mock.patch.dict(settings.DATABASES,
                             {FX_INGEST_ALIAS: {**settings.DATABASES["default"], "TEST": {"MIRROR": "default"}}}):
            self.assertEqual(resolve_fx_ingest_alias(), FX_INGEST_ALIAS)

    def test_command_refuses_to_write_without_alias(self):
        with mock.patch.dict(settings.DATABASES):
            settings.DATABASES.pop(FX_INGEST_ALIAS, None)
            with self.assertRaises(CommandError):
                call_command("fetch_fx_daily", fixture=FIXTURE, stdout=StringIO())
        self.assertEqual(DailyFxObservation.objects.count(), 0)          # fail-closed: nothing written

    def test_command_routes_through_ingestion_alias_never_default(self):
        seen = {}

        def _fake_append(**kw):
            seen["using"] = kw.get("using")
            return (True, 1)

        with mock.patch.dict(settings.DATABASES,
                             {FX_INGEST_ALIAS: {**settings.DATABASES["default"], "TEST": {"MIRROR": "default"}}}):
            with mock.patch("analytics.management.commands.fetch_fx_daily.append_fx_observation",
                            side_effect=_fake_append):
                call_command("fetch_fx_daily", fixture=FIXTURE, stdout=StringIO())
        self.assertEqual(seen.get("using"), FX_INGEST_ALIAS)            # routed to fx_ingest, never 'default'

    def test_dry_run_requires_no_alias(self):
        with mock.patch.dict(settings.DATABASES):
            settings.DATABASES.pop(FX_INGEST_ALIAS, None)
            call_command("fetch_fx_daily", fixture=FIXTURE, dry_run=True, stdout=StringIO())
        self.assertEqual(DailyFxObservation.objects.count(), 0)

    def test_router_blocks_migration_on_ingestion_alias(self):
        r = FxIngestRouter()
        self.assertIs(r.allow_migrate(FX_INGEST_ALIAS, "analytics"), False)   # never migrate through fx_ingest
        self.assertIsNone(r.allow_migrate("default", "analytics"))            # no opinion elsewhere
        self.assertIsNone(r.db_for_read(DailyFxObservation))                  # never auto-route to fx_ingest
        self.assertIsNone(r.db_for_write(DailyFxObservation))


# ── Runtime immutability (app-layer guard; DB triggers are authoritative) ───────────────────────────────────────
class FxModelGuardTests(TestCase):
    def test_delete_blocked_app_layer(self):
        o = _obs(date(2026, 1, 5), "1.27")
        with self.assertRaises(ValueError):
            o.delete()

    def test_inplace_edit_blocked_app_layer(self):
        o = _obs(date(2026, 1, 5), "1.27")
        o.rate = Decimal("9.99")
        with self.assertRaises(ValueError):
            o.save()
