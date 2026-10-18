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


def _category(*, min_contributors=5, min_samples=20, max_share=40, version=1):
    return BrokerMetricCategory.objects.create(
        key=BrokerMetricCategory.Key.WITHDRAWAL_PROCESSING, status=BrokerMetricCategory.Status.ACTIVE,
        methodology_version=version, methodology="median broker-reported processing time; dedup + winsorised",
        min_contributors=min_contributors, min_samples=min_samples, max_contributor_share_pct=max_share)


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
        _category(min_contributors=1, min_samples=1)
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

    def test_per_contributor_cap_limits_domination(self):
        b = _broker()
        _category(min_contributors=2, min_samples=2, max_share=40)
        a1, a2 = _acct(), _acct()
        for _ in range(10):
            _wd(a1)
        for _ in range(10):
            _wd(a2)
        r = compute_withdrawal_processing(b, certified_brokers=CERT)   # total 20, cap=40% of 20=8 per owner
        self.assertEqual(r["sample_size"], 16)                          # 8 + 8, not 20 (winsorised)

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
        self.assertEqual(m["stats"], {})
        self.assertIn("sample_size", m)
        self.assertIn("methodology_version", m)

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
