"""broker_public — seed_public_broker_pages management-command tests (Stream C / ADR-0080).

Proves the DARK seed is additive, idempotent, and never publishes/certifies anything.
"""
from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from broker_intelligence.external_withdrawal_classifier import CERTIFIED_BROKERS
from broker_public.models import BrokerMetricCategory, PublicBroker
from broker_public.withdrawal_intelligence import CATEGORY_KEY


class SeedPublicBrokerPages(TestCase):
    def test_seeds_four_dark_brokers_and_the_category(self):
        call_command("seed_public_broker_pages", stdout=StringIO())
        slugs = set(PublicBroker.objects.values_list("slug", flat=True))
        self.assertEqual(slugs, {"pepperstone", "dominion-markets", "vantage", "tradersway"})
        # every seeded broker is DARK (unpublished) — none appears in the directory
        self.assertEqual(PublicBroker.objects.filter(is_published=True).count(), 0)
        cat = BrokerMetricCategory.objects.get(key=CATEGORY_KEY, status=BrokerMetricCategory.Status.ACTIVE)
        self.assertEqual((cat.min_contributors, cat.min_samples, cat.min_completed_samples), (3, 5, 5))

    def test_idempotent(self):
        call_command("seed_public_broker_pages", stdout=StringIO())
        call_command("seed_public_broker_pages", stdout=StringIO())   # second run creates nothing
        self.assertEqual(PublicBroker.objects.count(), 4)
        self.assertEqual(BrokerMetricCategory.objects.filter(
            key=CATEGORY_KEY, status=BrokerMetricCategory.Status.ACTIVE).count(), 1)

    def test_seed_certifies_nothing(self):
        call_command("seed_public_broker_pages", stdout=StringIO())
        # the seed must NEVER certify a broker — the public surface stays UNAVAILABLE until a governance certification
        self.assertEqual(CERTIFIED_BROKERS, frozenset())
