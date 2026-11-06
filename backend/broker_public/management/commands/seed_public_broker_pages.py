"""Seed the DARK public broker-page foundation (Stream C / ADR-0080).

Idempotent, additive, operator-run. Creates (via get_or_create):

* one ``PublicBroker`` row per foundation broker — Pepperstone, Dominion Markets, Vantage, TradersWay — each
  ``is_published=False`` so it is NOT listed in the directory and its page stays DARK until an operator publishes it;
* one ACTIVE ``BrokerMetricCategory`` for ``WITHDRAWAL_INTELLIGENCE`` carrying the Stream C disclosure thresholds
  (>= 3 distinct contributors, >= 5 observations, >= 5 completed observations for a speed figure).

It NEVER certifies a broker (``CERTIFIED_BROKERS`` stays empty), NEVER publishes a page, NEVER touches member data,
Gmail, a broker portal, or any credential, and performs NO network or financial action. Re-running it changes nothing
that already exists. Not wired into any migration, cron, or deploy — it is run deliberately in an isolated environment.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from broker_public.models import BrokerMetricCategory, PublicBroker
from broker_public.withdrawal_intelligence import CATEGORY_KEY, METHODOLOGY, _FALLBACK

# (public slug, display name, source_slug) — source_slug is the authoritative internal key matched (case-insensitively)
# against TradingAccount.broker_name; it is NOT a public display value.
_BROKERS = [
    ("pepperstone", "Pepperstone", "Pepperstone"),
    ("dominion-markets", "Dominion Markets", "Dominion Markets"),
    ("vantage", "Vantage", "Vantage"),
    ("tradersway", "TradersWay", "TradersWay"),
]


class Command(BaseCommand):
    help = ("Seed the DARK (unpublished) public broker pages + the WITHDRAWAL_INTELLIGENCE metric category. "
            "Idempotent; creates nothing that already exists; publishes/certifies nothing.")

    @transaction.atomic
    def handle(self, *args, **options):
        created_brokers = 0
        for slug, display_name, source_slug in _BROKERS:
            _, created = PublicBroker.objects.get_or_create(
                slug=slug,
                defaults={"display_name": display_name, "source_slug": source_slug,
                          "summary": "", "website": "", "is_published": False},
            )
            created_brokers += 1 if created else 0

        category_created = False
        if not BrokerMetricCategory.objects.filter(
                key=CATEGORY_KEY, status=BrokerMetricCategory.Status.ACTIVE).exists():
            BrokerMetricCategory.objects.create(
                key=CATEGORY_KEY,
                status=BrokerMetricCategory.Status.ACTIVE,
                methodology_version=_FALLBACK["methodology_version"],
                methodology=METHODOLOGY,
                min_contributors=_FALLBACK["min_contributors"],
                min_samples=_FALLBACK["min_samples"],
                min_completed_samples=_FALLBACK["min_completed_samples"],
                max_contributor_share_pct=_FALLBACK["max_contributor_share_pct"],
                min_bucket_size=_FALLBACK["min_bucket_size"],
            )
            category_created = True

        self.stdout.write(
            "seed_public_broker_pages: brokers +%d (of %d, is_published=False), "
            "WITHDRAWAL_INTELLIGENCE category %s. DARK: nothing certified, nothing published."
            % (created_brokers, len(_BROKERS), "created" if category_created else "already present"))
