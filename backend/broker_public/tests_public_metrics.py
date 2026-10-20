"""broker_public — public directory + privacy-safe aggregation tests (MULTI_ACCOUNT §E/§F/§J).

Covers: DARK gating (404 while off), published-only directory, aggregation thresholds (INSUFFICIENT_DATA below),
certified+genuine-only eligibility (synthetic/sanitised + uncertified excluded), per-contributor cap, no-PII payload,
and unauthenticated (AllowAny) access when armed.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from broker_intelligence.models import Provenance, Withdrawal
from broker_public.aggregation import compute_withdrawal_processing, upsert_withdrawal_processing
from broker_public.models import BrokerMetricAggregate, BrokerMetricCategory, PublicBroker
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "90%04d" % _n


def _broker(slug="tradersway", source="TradersWay", published=True):
    return PublicBroker.objects.create(slug=slug, display_name="TradersWay", source_slug=source,
                                       is_published=published)


def _category(*, min_contributors=5, min_samples=20, max_share=40, min_completed=5, version=1):
    return BrokerMetricCategory.objects.create(
        key=BrokerMetricCategory.Key.WITHDRAWAL_PROCESSING, status=BrokerMetricCategory.Status.ACTIVE,
        methodology_version=version, methodology="median broker-reported processing time; dedup + winsorised",
        min_contributors=min_contributors, min_samples=min_samples, max_contributor_share_pct=max_share,
        min_completed_samples=min_completed)


def _acct(broker="TradersWay"):
    login = _uniq()
    u = User.objects.create_user(username="pub%s" % login, email="%s@x.invalid" % login, password="x")
    return TradingAccount.objects.create(user=u, name="A", broker_name=broker, account_number=login,
                                         is_demo=False, is_active=True)


def _wd(acct, *, status=Withdrawal.Status.COMPLETED, amount="100.00", currency="USD",
        provenance=Provenance.REAL, hours=2, ref=""):
    now = timezone.now()
    return Withdrawal.objects.create(
        trading_account=acct, status=status, provenance=provenance, broker_reference_id=ref,
        amount=Decimal(amount) if amount is not None else None, currency=currency,
        requested_at=now - timedelta(hours=hours), completed_at=now if status == Withdrawal.Status.COMPLETED else None)


CERT = frozenset({"TradersWay"})   # certified-broker override for tests (production default is empty)


class Aggregation(TestCase):
    def test_uncertified_broker_yields_insufficient_data(self):
        b = _broker()
        _category(min_contributors=1, min_samples=1)
        a = _acct(); _wd(a); _wd(_acct())
        r = compute_withdrawal_processing(b, certified_brokers=frozenset())   # not certified (prod default)
        self.assertEqual(r["state"], BrokerMetricAggregate.State.INSUFFICIENT_DATA)
        self.assertEqual(r["sample_size"], 0)

    def test_below_thresholds_is_insufficient_data(self):
        b = _broker()
        _category(min_contributors=5, min_samples=20)
        for _ in range(3):   # 3 contributors, 3 samples — below both thresholds
            _wd(_acct())
        r = compute_withdrawal_processing(b, certified_brokers=CERT)
        self.assertEqual(r["state"], BrokerMetricAggregate.State.INSUFFICIENT_DATA)
        self.assertEqual(r["stats"], {})

    def test_synthetic_and_sanitised_excluded_from_public_aggregate(self):
        b = _broker()
        _category(min_contributors=1, min_samples=1, max_share=100)   # max_share=100 -> isolate the exclusion logic
        a = _acct()
        _wd(a, provenance=Provenance.REAL, ref="genuine")
        _wd(_acct(), provenance=Provenance.SYNTHETIC, ref="synthetic")   # excluded
        _wd(_acct(), provenance=Provenance.SANITISED, ref="sanitised")   # excluded
        r = compute_withdrawal_processing(b, certified_brokers=CERT)
        self.assertEqual(r["sample_size"], 1)                 # only the genuine one
        self.assertEqual(r["contributor_count"], 1)

    def test_published_aggregate_has_pii_free_stats(self):
        b = _broker()
        _category(min_contributors=2, min_samples=2, max_share=100)
        a1, a2 = _acct(), _acct()
        _wd(a1, amount="250.00", currency="USD", hours=3)
        _wd(a2, amount="99.99", currency="EUR", hours=1)
        agg = upsert_withdrawal_processing(b, certified_brokers=CERT)
        self.assertEqual(agg.state, BrokerMetricAggregate.State.PUBLISHED)
        self.assertEqual(agg.contributor_count, 2)
        self.assertEqual(agg.sample_size, 2)
        body = str(agg.stats)
        # aggregate-only: lifecycle counts + processing time + per-currency COUNTS; never amounts/ids/emails/refs
        self.assertIn("lifecycle_counts", agg.stats)
        self.assertIn("per_currency_counts", agg.stats)
        for leak in ("250.00", "99.99", str(a1.account_number), str(a2.account_number), a1.user.email):
            self.assertNotIn(leak, body, leak)

    def test_single_contributor_cannot_dominate_a_published_statistic(self):
        # DEFECT-3 regression: a dominator (100 vs four others' 1 each) must NOT reach PUBLISHED. The water-filling
        # winsorisation bounds each contributor against the FINAL sample, so the dominator is pared back and the
        # sample collapses below min_samples -> INSUFFICIENT_DATA (never a single-member-driven public statistic).
        b = _broker()
        _category(min_contributors=5, min_samples=20, max_share=40)
        dom = _acct()
        for _ in range(100):
            _wd(dom)
        for _ in range(4):
            _wd(_acct())
        r = compute_withdrawal_processing(b, certified_brokers=CERT)
        self.assertEqual(r["state"], BrokerMetricAggregate.State.INSUFFICIENT_DATA)
        self.assertEqual(r["stats"], {})

    def test_balanced_sample_publishes_with_bounded_shares(self):
        # The counterpart: a BALANCED cross-member sample (5 members x 5 each) publishes, and no contributor exceeds
        # the share cap (each is 20% < 40%).
        b = _broker()
        _category(min_contributors=5, min_samples=20, max_share=40, min_completed=5)
        for _ in range(5):
            a = _acct()
            for _ in range(5):
                _wd(a)
        r = compute_withdrawal_processing(b, certified_brokers=CERT)
        self.assertEqual(r["state"], BrokerMetricAggregate.State.PUBLISHED)
        self.assertEqual(r["contributor_count"], 5)
        self.assertEqual(r["sample_size"], 25)                          # nothing winsorised (balanced)

    def test_freshness_is_coarsened_and_withheld_below_threshold(self):
        # DEFECT-1 regression: data_freshness must never be a raw per-record timestamp, and must be withheld on the
        # INSUFFICIENT_DATA path. Published: coarsened to midnight (no sub-day precision). Insufficient: None.
        b = _broker()
        _category(min_contributors=5, min_samples=5, max_share=40, min_completed=5)
        for _ in range(5):
            _wd(_acct(), hours=3)
        pub = compute_withdrawal_processing(b, certified_brokers=CERT)
        self.assertEqual(pub["state"], BrokerMetricAggregate.State.PUBLISHED)
        fr = pub["data_freshness_at"]
        self.assertIsNotNone(fr)
        self.assertEqual((fr.hour, fr.minute, fr.second, fr.microsecond), (0, 0, 0, 0))   # date-coarsened, no time
        # a below-threshold broker yields no freshness at all
        b2 = _broker(slug="is6", source="IS6")
        _wd(_acct("IS6"))
        r2 = compute_withdrawal_processing(b2, certified_brokers=frozenset({"IS6"}))
        self.assertEqual(r2["state"], BrokerMetricAggregate.State.INSUFFICIENT_DATA)
        self.assertIsNone(r2["data_freshness_at"])

    def test_processing_time_suppressed_below_completed_floor(self):
        # DEFECT-2 regression: with fewer than min_completed completed observations, processing_time is withheld, so
        # the published median can never BE a single member's exact duration.
        b = _broker()
        _category(min_contributors=5, min_samples=5, max_share=100, min_completed=5)
        # 5 contributors (meets sample/contributor thresholds) but only ONE completed -> processing_time must be None
        _wd(_acct(), status=Withdrawal.Status.COMPLETED, hours=7)
        for _ in range(4):
            _wd(_acct(), status=Withdrawal.Status.PROCESSING)
        r = compute_withdrawal_processing(b, certified_brokers=CERT)
        self.assertEqual(r["state"], BrokerMetricAggregate.State.PUBLISHED)
        self.assertIsNone(r["stats"]["processing_time"])               # < min_completed -> no distribution exposed

    def test_completed_is_reported_not_claimed_received(self):
        b = _broker()
        _category(min_contributors=1, min_samples=1, max_share=100)
        _wd(_acct(), status=Withdrawal.Status.COMPLETED)
        r = compute_withdrawal_processing(b, certified_brokers=CERT)
        lc = r["stats"]["lifecycle_counts"]
        self.assertEqual(lc.get("broker_reported_completed"), 1)        # broker REPORTED completion...
        self.assertEqual(lc.get("receipt_confirmed_if_ever_available"), 0)   # ...never "funds received"


@override_settings(BROKER_PUBLIC_DIRECTORY_ENABLED="1")
class PublicApiArmed(TestCase):
    def setUp(self):
        self.c = APIClient()   # NO authentication — the public surface must work unauthenticated

    def test_directory_lists_published_only(self):
        _broker(slug="tradersway", published=True)
        _broker(slug="hidden", source="Hidden", published=False)
        r = self.c.get("/api/public/brokers/")
        self.assertEqual(r.status_code, 200)
        slugs = [b["slug"] for b in r.json()["brokers"]]
        self.assertIn("tradersway", slugs)
        self.assertNotIn("hidden", slugs)

    def test_profile_shows_insufficient_data_and_no_member_data(self):
        b = _broker()
        _category(min_contributors=5, min_samples=20)
        upsert_withdrawal_processing(b, certified_brokers=CERT)   # no data -> INSUFFICIENT_DATA
        r = self.c.get("/api/public/brokers/tradersway/")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["slug"], "tradersway")
        m = next(x for x in data["metrics"] if x["category"] == "WITHDRAWAL_PROCESSING")
        self.assertEqual(m["state"], "INSUFFICIENT_DATA")
        self.assertIn("methodology_version", m)
        # PD review §6: the INSUFFICIENT_DATA HTTP payload leaks NO exact contributor/observation counts, NO window,
        # NO transaction-derived timestamp — only the truthful generic stub. (Previously this asserted
        # `assertIn("sample_size", m)` / `stats == {}`, which LOCKED IN the leak; "all tests pass" was not proof the
        # leak was closed until these assertions were flipped.)
        for leaked in ("sample_size", "contributor_count", "observation_window", "last_update",
                       "data_freshness", "stats"):
            self.assertNotIn(leaked, m, leaked)
        self.assertEqual(set(m.keys()), {"category", "state", "methodology_version"})

    def test_published_profile_payload_includes_aggregate_fields(self):
        # The counterpart regression: a PUBLISHED metric DOES expose the aggregate-only fields (so the suppression
        # above is not over-broad). 5 members x 5 completed each -> PUBLISHED.
        b = _broker()
        _category(min_contributors=5, min_samples=20, max_share=40, min_completed=5)
        for _ in range(5):
            a = _acct()
            for _ in range(5):
                _wd(a)
        upsert_withdrawal_processing(b, certified_brokers=CERT)
        r = self.c.get("/api/public/brokers/tradersway/")
        self.assertEqual(r.status_code, 200)
        m = next(x for x in r.json()["metrics"] if x["category"] == "WITHDRAWAL_PROCESSING")
        self.assertEqual(m["state"], "PUBLISHED")
        for field in ("sample_size", "contributor_count", "observation_window", "last_update", "stats"):
            self.assertIn(field, m)

    def test_public_anon_throttle_configured_and_enforced(self):
        # The dedicated anonymous throttle is CONFIGURED (resolves a rate — the prior AnonRateThrottle
        # ImproperlyConfigured blocker is gone) and ENFORCED on the public surface.
        from unittest.mock import patch
        from django.core.cache import cache
        from core.throttling import GuvFXPublicAnonRateThrottle
        self.assertIsNotNone(GuvFXPublicAnonRateThrottle().get_rate())   # 'public' scope resolves (no misconfig)
        _broker(slug="tradersway", published=True)
        cache.clear()
        with patch.object(GuvFXPublicAnonRateThrottle, "get_rate", return_value="1/min"):
            first = self.c.get("/api/public/brokers/")
            second = self.c.get("/api/public/brokers/")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)   # anonymous ceiling enforced

    def test_unknown_or_unpublished_broker_is_404(self):
        _broker(slug="hidden", published=False)
        self.assertEqual(self.c.get("/api/public/brokers/nope/").status_code, 404)
        self.assertEqual(self.c.get("/api/public/brokers/hidden/").status_code, 404)


class PublicApiDark(TestCase):
    def test_endpoints_404_while_flag_off(self):
        _broker()
        c = APIClient()
        self.assertEqual(c.get("/api/public/brokers/").status_code, 404)
        self.assertEqual(c.get("/api/public/brokers/tradersway/").status_code, 404)
