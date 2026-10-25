"""BoE XUDLUSS provider adapter: parser (direction/plausibility/formats/wrong-series), governed source priority,
deterministic failover selection, DARK default (Fed-only), and provenance disclosing the selected source."""
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from analytics import fx_boe as BOE
from analytics import fx_fred as FRED
from analytics.fx_fred import FxParseError, observation_checksum
from analytics.fx_ingest import append_fx_observation
from analytics.fx_source import (
    StoredDbFxRateSource, fx_source_priority, select_observation, select_prioritised,
)
from analytics.models import DailyFxObservation

_CSV = "DATE,XUDLUSS\n01 Oct 2026,1.3212\n02 Oct 2026,1.3249\n03 Oct 2026,\n"


class BoeParserTests(SimpleTestCase):
    def test_parses_valid_and_counts_missing(self):
        r = BOE.parse_xudluss_csv(_CSV)
        self.assertEqual([str(o.rate) for o in r.accepted], ["1.3212", "1.3249"])
        self.assertEqual(r.missing_count, 1)
        self.assertEqual(r.accepted[0].effective_date, date(2026, 10, 1))

    def test_accepts_slash_date_format(self):
        r = BOE.parse_xudluss_csv("DATE,XUDLUSS\n22/Oct/2026,1.3363\n")
        self.assertEqual(r.accepted[0].effective_date, date(2026, 10, 22))

    def test_wrong_series_raises(self):
        with self.assertRaises(FxParseError):
            BOE.parse_xudluss_csv("DATE,DEXUSUK\n01 Oct 2026,1.32\n")

    def test_rejects_nonnumeric_nonpositive_implausible_baddate(self):
        r = BOE.parse_xudluss_csv(
            "DATE,XUDLUSS\n01 Oct 2026,abc\n02 Oct 2026,-1\n03 Oct 2026,0.5\n99 Zzz 2026,1.33\n")
        reasons = sorted(x.reason for x in r.rejected)
        self.assertEqual(reasons, ["bad_date", "implausible", "nonnumeric", "nonpositive"])
        self.assertEqual(r.accepted, [])   # inverted GBP-per-USD ~0.5 rejected (direction guard via plausibility)

    def test_direction_and_orientation_constants(self):
        self.assertEqual((BOE.BASE, BOE.QUOTE, BOE.DIRECTION), ("GBP", "USD", "USD_PER_GBP"))
        self.assertEqual(BOE.SOURCE, DailyFxObservation.SOURCE_BOE)


class SourcePriorityTests(SimpleTestCase):
    def test_default_is_fed_only(self):
        self.assertEqual(fx_source_priority(), ("FED_H10",))

    @override_settings(PORTFOLIO_FX_SOURCE_PRIORITY="BOE,FED_H10")
    def test_parses_priority_order(self):
        self.assertEqual(fx_source_priority(), ("BOE", "FED_H10"))

    @override_settings(PORTFOLIO_FX_SOURCE_PRIORITY=" nonsense , BOE , FED_H10 , BOE ")
    def test_drops_unknown_and_dedupes(self):
        self.assertEqual(fx_source_priority(), ("BOE", "FED_H10"))

    @override_settings(PORTFOLIO_FX_SOURCE_PRIORITY="xxx")
    def test_invalid_falls_back_to_fed(self):
        self.assertEqual(fx_source_priority(), ("FED_H10",))


def _obs(source, series, d, rate):
    r = Decimal(rate)
    append_fx_observation(
        effective_date=d, rate=r, observation_checksum=observation_checksum(source, series, "GBP", "USD", d, r),
        raw_line=f"{d},{rate}", source_url="test", retrieved_at=timezone.now(),
        source=source, base="GBP", quote="USD", using="default")


class PrioritisedSelectionTests(TestCase):
    CUR = date(2026, 10, 25)   # target = 2026-10-24

    def test_boe_preferred_when_fresh(self):
        _obs("FED_H10", "DEXUSUK", date(2026, 10, 16), "1.3330")   # Fed: 8 days stale (refused alone)
        _obs("BOE", "XUDLUSS", date(2026, 10, 22), "1.3363")       # BoE: 2 days — fresh
        sel = select_prioritised("GBP", "USD", self.CUR, ("BOE", "FED_H10"))
        self.assertIsNotNone(sel)
        self.assertEqual(sel.source, "BOE")
        self.assertEqual(sel.effective_date, date(2026, 10, 22))

    def test_failover_to_fed_when_boe_absent(self):
        _obs("FED_H10", "DEXUSUK", date(2026, 10, 23), "1.3300")   # Fed fresh, no BoE rows
        sel = select_prioritised("GBP", "USD", self.CUR, ("BOE", "FED_H10"))
        self.assertEqual(sel.source, "FED_H10")

    def test_none_when_all_sources_stale(self):
        _obs("BOE", "XUDLUSS", date(2026, 10, 10), "1.33")         # 14 days stale
        self.assertIsNone(select_prioritised("GBP", "USD", self.CUR, ("BOE", "FED_H10")))

    def test_dark_default_fed_only_ignores_boe(self):
        _obs("BOE", "XUDLUSS", date(2026, 10, 22), "1.3363")       # BoE fresh but NOT in default priority
        # Default priority is Fed-only: with no Fed row the prioritised select is unavailable (BoE ignored).
        self.assertIsNone(select_prioritised("GBP", "USD", self.CUR, fx_source_priority()))

    def test_stored_db_source_provenance_names_selected_source(self):
        _obs("BOE", "XUDLUSS", date(2026, 10, 22), "1.3363")
        src = StoredDbFxRateSource(current_date=self.CUR, priority=("BOE", "FED_H10"))
        prov = src.provenance()
        self.assertTrue(prov["available"])
        self.assertEqual(prov["source"], "BOE")
        self.assertEqual(prov["selected_source"], "BOE")
        self.assertEqual(prov["effective_date"], "2026-10-22")
        self.assertEqual(prov["source_priority"], ["BOE", "FED_H10"])
        rate = src.get_rate("GBP", "USD")
        self.assertAlmostEqual(rate.rate, 1.3363, places=4)
