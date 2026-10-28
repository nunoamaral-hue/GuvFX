"""FX source/series + ingestion-health coherence (PD P1).

Guards three defects where the FX *rate* was correct but the disclosed/recorded SOURCE SERIES and the ingestion
HEALTH signal were incoherent with the governed source priority:

1. ``append_fx_observation`` hardcoded the Fed series (``DEXUSUK``) for every source, so BoE rows were stored as
   ``DEXUSUK`` instead of ``XUDLUSS``.
2. ``StoredDbFxRateSource`` provenance / ``get_rate`` disclosed the stored (possibly mislabelled) series; it now
   derives the series from the SELECTED source, correcting even historical mislabelled rows.
3. ``fx_ingestion_health`` only ever checked Fed H.10, so it reported STALE/convertible=false while the request
   path (BoE-first priority) was converting fine. It is now evaluated over the governed priority.

Existing weekday/weekend/holiday fallback, 7-day staleness refusal, missing-rate, already-USD identity, GBP
conversion, scope and aggregation behaviours are covered by tests_fx.py / tests_fx_boe.py and are unchanged.
"""
from __future__ import annotations

import tempfile
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from analytics import fx_boe as BOE
from analytics import fx_fred as FRED
from analytics.fx_fred import observation_checksum
from analytics.fx_ingest import append_fx_observation
from analytics.fx_source import StoredDbFxRateSource, fx_ingestion_health, series_for_source
from analytics.models import DailyFxObservation


def _ingest(source, series, d, rate, *, using="default"):
    r = Decimal(rate)
    return append_fx_observation(
        effective_date=d, rate=r,
        observation_checksum=observation_checksum(source, series, "GBP", "USD", d, r),
        raw_line=f"{d},{rate}", source_url="test", retrieved_at=timezone.now(),
        source=source, base="GBP", quote="USD", source_series=series, using=using)


class AppendSourceSeriesTests(TestCase):
    def test_default_source_series_is_fed(self):
        # The Fed caller omits source_series -> defaults to DEXUSUK (unchanged behaviour).
        r = Decimal("1.2700")
        append_fx_observation(
            effective_date=date(2026, 1, 2), rate=r,
            observation_checksum=observation_checksum(FRED.SOURCE, FRED.SERIES, "GBP", "USD", date(2026, 1, 2), r),
            raw_line="x", source_url="t", retrieved_at=timezone.now(), using="default")
        row = DailyFxObservation.objects.get(source=FRED.SOURCE, effective_date=date(2026, 1, 2))
        self.assertEqual(row.source_series, FRED.SERIES)  # DEXUSUK

    def test_boe_source_series_stamped_xudluss(self):
        _ingest(BOE.SOURCE, BOE.SERIES, date(2026, 10, 22), "1.3363")
        row = DailyFxObservation.objects.get(source=BOE.SOURCE, effective_date=date(2026, 10, 22))
        self.assertEqual(row.source_series, BOE.SERIES)  # XUDLUSS, not DEXUSUK

    def test_fetch_fx_boe_command_stamps_xudluss(self):
        csv = "DATE,XUDLUSS\n20 Oct 2026,1.3387\n21 Oct 2026,1.3384\n22 Oct 2026,1.3363\n"
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            fh.write(csv); path = fh.name
        with mock.patch(
            "analytics.management.commands.fetch_fx_boe.resolve_fx_ingest_alias", return_value="default"):
            out = StringIO()
            call_command("fetch_fx_boe", fixture=path, stdout=out)
        rows = DailyFxObservation.objects.filter(source=BOE.SOURCE)
        self.assertEqual(rows.count(), 3)
        self.assertTrue(all(r.source_series == BOE.SERIES for r in rows))  # all XUDLUSS


class ProvenanceSeriesTests(TestCase):
    CUR = date(2026, 10, 25)   # target = 2026-10-24

    def test_boe_selection_discloses_xudluss(self):
        _ingest(BOE.SOURCE, BOE.SERIES, date(2026, 10, 22), "1.3363")
        prov = StoredDbFxRateSource(current_date=self.CUR, priority=("BOE", "FED_H10")).provenance()
        self.assertTrue(prov["available"])
        self.assertEqual(prov["selected_source"], "BOE")
        self.assertEqual(prov["source_series"], BOE.SERIES)  # XUDLUSS

    def test_legacy_mislabelled_boe_row_still_discloses_xudluss(self):
        # Simulate a historical row written BEFORE the ingest fix: BoE source but stored as the Fed series.
        _ingest(BOE.SOURCE, FRED.SERIES, date(2026, 10, 22), "1.3363")
        prov = StoredDbFxRateSource(current_date=self.CUR, priority=("BOE", "FED_H10")).provenance()
        self.assertEqual(prov["selected_source"], "BOE")
        # Disclosed series is derived from the SOURCE, correcting the stored mislabel.
        self.assertEqual(prov["source_series"], BOE.SERIES)

    def test_get_rate_source_string_names_correct_series(self):
        _ingest(BOE.SOURCE, FRED.SERIES, date(2026, 10, 22), "1.3363")  # stored mislabelled
        rate = StoredDbFxRateSource(current_date=self.CUR, priority=("BOE", "FED_H10")).get_rate("GBP", "USD")
        self.assertIn("BOE:XUDLUSS", rate.source)

    def test_unavailable_provenance_series_is_priority_head(self):
        prov = StoredDbFxRateSource(current_date=self.CUR, priority=("BOE", "FED_H10")).provenance()
        self.assertFalse(prov["available"])
        self.assertEqual(prov["source"], "BOE")
        self.assertEqual(prov["source_series"], BOE.SERIES)
        self.assertEqual(prov["reason"], "no_rate_within_staleness")

    def test_usd_identity_not_rescaled_no_double_conversion(self):
        _ingest(BOE.SOURCE, BOE.SERIES, date(2026, 10, 22), "1.3363")
        src = StoredDbFxRateSource(current_date=self.CUR, priority=("BOE", "FED_H10"))
        usd = src.get_rate("USD", "USD")
        self.assertEqual(usd.rate, 1.0)
        self.assertEqual(usd.source, "identity")
        gbp = src.get_rate("GBP", "USD")
        self.assertAlmostEqual(gbp.rate, 1.3363, places=4)  # GBP converts once; USD untouched


class IngestionHealthPriorityTests(TestCase):
    CUR = date(2026, 10, 25)   # target = 2026-10-24

    def test_healthy_via_boe_when_fed_stale(self):
        # The Sponsor scenario: Fed stale (10-16, 8 days), BoE fresh (10-22, 2 days), priority BoE-first.
        _ingest(FRED.SOURCE, FRED.SERIES, date(2026, 10, 16), "1.3330")
        _ingest(BOE.SOURCE, BOE.SERIES, date(2026, 10, 22), "1.3363")
        h = fx_ingestion_health(current_date=self.CUR, priority=("BOE", "FED_H10"))
        self.assertEqual(h["status"], "HEALTHY")
        self.assertTrue(h["convertible"])
        self.assertEqual(h["source"], "BOE")
        self.assertEqual(h["series"], BOE.SERIES)
        self.assertEqual(h["latest_effective_date"], "2026-10-22")

    def test_fed_only_default_reports_stale(self):
        _ingest(FRED.SOURCE, FRED.SERIES, date(2026, 10, 16), "1.3330")
        _ingest(BOE.SOURCE, BOE.SERIES, date(2026, 10, 22), "1.3363")  # fresh, but NOT in default priority
        h = fx_ingestion_health(current_date=self.CUR)  # default priority = Fed-only
        self.assertEqual(h["status"], "STALE")
        self.assertFalse(h["convertible"])
        self.assertEqual(h["source"], "FED_H10")
        self.assertEqual(h["series"], FRED.SERIES)

    def test_explicit_source_override_checks_only_that_source(self):
        _ingest(FRED.SOURCE, FRED.SERIES, date(2026, 10, 16), "1.3330")  # stale
        _ingest(BOE.SOURCE, BOE.SERIES, date(2026, 10, 22), "1.3363")    # fresh but ignored by override
        h = fx_ingestion_health(current_date=self.CUR, source="FED_H10")
        self.assertEqual(h["status"], "STALE")
        self.assertEqual(h["source"], "FED_H10")

    def test_empty_when_no_rows(self):
        h = fx_ingestion_health(current_date=self.CUR, priority=("BOE", "FED_H10"))
        self.assertEqual(h["status"], "EMPTY")
        self.assertEqual(h["accepted_count"], 0)
        self.assertEqual(h["source"], "BOE")
        self.assertEqual(h["series"], BOE.SERIES)

    def test_series_for_source_helper(self):
        self.assertEqual(series_for_source("BOE"), BOE.SERIES)
        self.assertEqual(series_for_source("FED_H10"), FRED.SERIES)
        self.assertEqual(series_for_source("UNKNOWN", "fallback"), "fallback")
