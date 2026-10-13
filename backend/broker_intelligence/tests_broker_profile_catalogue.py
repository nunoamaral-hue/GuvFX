"""PR3 — BrokerProfile supported-categories + provenance + BrokerServerCatalogue reconciliation + audited changed_by.

Pins: the pilot brokers DECLARE exactly the categories their deterministic parser emits and NEVER declare
EXTERNAL_WITHDRAWAL (no withdrawal can be surfaced without a certified positive classifier); declared category tokens
are valid; the catalogue (authoritative) reconciliation flags a server a profile names but the catalogue has not
verified, without mutating anything; and a config edit records WHO made it in the append-only audit.
"""
from django.test import TestCase

from broker_intelligence.broker_profile_catalogue import (category_capability_violations, reconcile_profile)
from broker_intelligence.models import BrokerProfile, BrokerProfileAudit, TransactionCategory
from trading.models import BrokerServerCatalogue


class SupportedCategoriesTests(TestCase):
    def test_pilot_profiles_declare_their_parser_categories(self):
        tw = BrokerProfile.objects.get(slug="tradersway")
        is6 = BrokerProfile.objects.get(slug="is6")
        self.assertEqual(set(tw.supported_category_list()), {"INTERNAL_TRANSFER", "UNKNOWN"})
        self.assertEqual(set(is6.supported_category_list()), {"DEPOSIT", "INTERNAL_TRANSFER", "UNKNOWN"})

    def test_no_active_broker_declares_external_withdrawal(self):
        # The load-bearing invariant: no pilot broker can surface a withdrawal (neither parser emits
        # EXTERNAL_WITHDRAWAL, and neither profile declares it) — so no withdrawal is surfaceable yet.
        for p in BrokerProfile.objects.filter(is_active=True):
            self.assertNotIn(TransactionCategory.EXTERNAL_WITHDRAWAL, p.supported_category_list(),
                             f"{p.slug} must not declare EXTERNAL_WITHDRAWAL")

    def test_declared_categories_must_be_valid(self):
        is6 = BrokerProfile.objects.get(slug="is6")
        self.assertEqual(category_capability_violations(is6), [])        # real config is clean
        bogus = BrokerProfile(slug="x", name="X", supported_categories="DEPOSIT, NOT_A_CATEGORY")
        self.assertEqual(category_capability_violations(bogus), ["NOT_A_CATEGORY"])

    def test_provenance_defaults_seed(self):
        self.assertEqual(BrokerProfile.objects.get(slug="is6").provenance, BrokerProfile.ConfigProvenance.SEED)


class CatalogueReconciliationTests(TestCase):
    # Fresh, non-seeded server names so the reconcile logic is tested independently of the catalogue entries that
    # trading migration 0020 seeds (IS6Technologies-Live / TradersWay-Live).
    def test_reconcile_flags_server_not_verified_in_catalogue(self):
        BrokerServerCatalogue.objects.create(server_name="ReconBroker-Live", environment="live",
                                             is_verified=True, broker_display_name="ReconFX")
        p = BrokerProfile.objects.create(slug="recon1", name="Recon One",
                                         server_names="ReconBroker-Live, Ghost-Server-X")
        rep = reconcile_profile(p)
        self.assertEqual(rep["declared_servers"], 2)
        by_name = {s["server_name"]: s for s in rep["servers"]}
        self.assertTrue(by_name["ReconBroker-Live"]["in_catalogue"])
        self.assertEqual(by_name["ReconBroker-Live"]["verified_environment"], "live")    # catalogue authoritative
        self.assertFalse(by_name["Ghost-Server-X"]["in_catalogue"])                       # flagged, never guessed
        self.assertEqual(rep["unknown_servers"], ["Ghost-Server-X"])
        self.assertFalse(rep["all_in_catalogue"])

    def test_reconcile_is_read_only(self):
        before_servers = BrokerServerCatalogue.objects.count()
        p = BrokerProfile.objects.create(slug="recon_ro", name="Recon RO", server_names="Unlisted-Server-Z")
        reconcile_profile(p)
        self.assertEqual(BrokerServerCatalogue.objects.count(), before_servers)   # never auto-adds / never mutates

    def test_all_in_catalogue_true_only_when_every_server_verified(self):
        BrokerServerCatalogue.objects.create(server_name="ReconBroker2-Demo", environment="demo", is_verified=True)
        p = BrokerProfile.objects.create(slug="recon2", name="Recon Two", server_names="ReconBroker2-Demo")
        self.assertTrue(reconcile_profile(p)["all_in_catalogue"])

    def test_unverified_catalogue_entry_is_not_authoritative(self):
        # An UNVERIFIED catalogue row must not count as in-catalogue (resolve_verified_environment requires verified).
        BrokerServerCatalogue.objects.create(server_name="ReconBroker3-Live", environment="live", is_verified=False)
        p = BrokerProfile.objects.create(slug="recon3", name="Recon Three", server_names="ReconBroker3-Live")
        rep = reconcile_profile(p)
        self.assertFalse(rep["servers"][0]["in_catalogue"])
        self.assertEqual(rep["unknown_servers"], ["ReconBroker3-Live"])


class ConfigAuditChangedByTests(TestCase):
    def test_changed_by_is_recorded_when_set(self):
        p = BrokerProfile.objects.create(slug="audx", name="Aud X")
        p._changed_by = "ops@guvfx.com"
        p.supported_categories = "DEPOSIT,UNKNOWN"
        p.save()
        latest = BrokerProfileAudit.objects.filter(broker_profile=p, action="UPDATE").latest("id")
        self.assertEqual(latest.changed_by, "ops@guvfx.com")

    def test_programmatic_save_has_blank_changed_by(self):
        p = BrokerProfile.objects.create(slug="audy", name="Aud Y")   # no _changed_by set
        self.assertEqual(BrokerProfileAudit.objects.filter(broker_profile=p, action="CREATE").latest("id").changed_by, "")
