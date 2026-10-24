"""analytics/management/commands/fx_ingestion_health.py — print FX ingestion readiness (ops; read-only).

HEALTHY when a usable (within 7-day staleness) Fed H.10 DEXUSUK rate exists for GBP->USD; STALE when the latest
accepted observation is too old to convert; EMPTY when nothing has been ingested. Reads only the DB.
"""
import json

from django.core.management.base import BaseCommand

from analytics.fx_source import fx_ingestion_health


class Command(BaseCommand):
    help = "Print Fed H.10 DEXUSUK FX ingestion readiness (HEALTHY/STALE/EMPTY). Read-only; DB only."

    def handle(self, *args, **opts):
        self.stdout.write(json.dumps(fx_ingestion_health()))
