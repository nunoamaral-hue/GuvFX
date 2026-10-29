"""Migration 0022 — additive PepperstoneUK-Live BrokerServerCatalogue registration.

Verifies the catalogue row is created verified+live and that the pre-existing seeded rows (IS6FX / TradersWay /
Vantage from 0020/0021) are untouched (the migration is add-only). Runs against the fully-migrated test DB."""
from django.test import TestCase

from trading.models import BrokerServerCatalogue


class PepperstoneCatalogueMigrationTests(TestCase):
    def test_pepperstone_uk_live_registered_verified_live(self):
        row = BrokerServerCatalogue.objects.filter(name_key="pepperstoneuk-live").first()
        self.assertIsNotNone(row, "PepperstoneUK-Live must be registered in the catalogue")
        self.assertEqual(row.server_name, "PepperstoneUK-Live")
        self.assertEqual(row.environment, "live")
        self.assertTrue(row.is_verified)
        self.assertEqual(row.broker_display_name, "Pepperstone")

    def test_additive_existing_rows_untouched(self):
        # 0020/0021 seeded rows must still be present + verified (0022 never removes or alters them).
        for key in ("is6technologies-live", "tradersway-live", "vantagemarkets-live 10"):
            self.assertTrue(
                BrokerServerCatalogue.objects.filter(name_key=key, is_verified=True).exists(),
                f"pre-existing verified catalogue row missing: {key}")
