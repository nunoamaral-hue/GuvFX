"""PR-A — BrokerProfile backend: per-broker allowlist/authserv config, seeded TradersWay + IS6, DARK.

Both brokers share one Gmail mailbox but remain independently authenticated (sender-domain allowlist) and attributed.
"""
from __future__ import annotations

from django.test import TestCase, override_settings

from broker_intelligence import broker_senders as BS
from broker_intelligence.gmail_source import _trusted_authserv_ids
from broker_intelligence.models import BrokerProfile, BrokerProfileAudit


class BrokerProfileSeedTests(TestCase):
    def test_both_p0_brokers_seeded(self):
        self.assertTrue(BrokerProfile.objects.filter(slug="tradersway", is_active=True).exists())
        self.assertTrue(BrokerProfile.objects.filter(slug="is6", is_active=True).exists())

    def test_allowlist_unions_active_profiles(self):
        allow = BS.sender_allowlist()
        self.assertIn("tradersway.com", allow)      # TradersWay (no DMARC; aligned-DKIM)
        self.assertIn("is6.com", allow)             # IS6 (DMARC pass) — shares the same mailbox
        self.assertTrue(BS.is_allowlisted_broker_sender("payments@tradersway.com"))
        self.assertTrue(BS.is_allowlisted_broker_sender("noreply@is6.com"))
        self.assertFalse(BS.is_allowlisted_broker_sender("mum@familymail.example"))

    def test_trusted_authserv_unions_profiles(self):
        self.assertIn("mx.google.com", _trusted_authserv_ids())

    def test_deactivating_a_profile_drops_its_domains(self):
        BrokerProfile.objects.filter(slug="is6").update(is_active=False)
        allow = BS.sender_allowlist()
        self.assertNotIn("is6.com", allow)          # inactive broker contributes nothing
        self.assertIn("tradersway.com", allow)      # the other broker is unaffected
        self.assertFalse(BS.is_allowlisted_broker_sender("noreply@is6.com"))

    @override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="extra.example")
    def test_env_allowlist_still_unions_with_profiles(self):
        allow = BS.sender_allowlist()
        self.assertIn("extra.example", allow)       # env is additive (backward-compatible)
        self.assertIn("is6.com", allow)
        self.assertIn("tradersway.com", allow)

    def test_exact_domain_match_not_substring(self):
        self.assertFalse(BS.is_allowlisted_broker_sender("x@evilis6.com"))
        self.assertFalse(BS.is_allowlisted_broker_sender("x@is6.com.evil.com"))
        self.assertTrue(BS.is_allowlisted_broker_sender("x@mail.is6.com"))   # subdomain OK

    def test_profile_with_audit_cannot_be_deleted(self):
        # PROTECT: a profile carrying append-only history cannot be destroyed (retire via is_active instead) — the
        # audit is never cascade-wiped.
        from django.db.models import ProtectedError
        bp = BrokerProfile.objects.get(slug="is6")   # created by the seed -> already has an audit row
        with self.assertRaises(ProtectedError):
            bp.delete()

    def test_admin_disables_profile_deletion(self):
        from broker_intelligence.admin import BrokerProfileAdmin
        self.assertFalse(BrokerProfileAdmin(BrokerProfile, None).has_delete_permission(request=None))

    def test_dmarc_only_broker_rejects_aligned_dkim_only(self):
        from broker_intelligence.gmail_source import parse_rfc822
        BrokerProfile.objects.filter(slug="is6").update(auth_policy=BrokerProfile.AuthPolicy.DMARC_ONLY)

        def verdict(ar):
            raw = (b"From: noreply@is6.com\r\nAuthentication-Results: " + ar.encode() + b"\r\nSubject: s\r\n\r\nx")
            return parse_rfc822(raw)["auth_verdict"]
        # aligned DKIM but NO DMARC pass -> rejected for a DMARC_ONLY broker (stricter)
        self.assertEqual(verdict("mx.google.com; dkim=pass header.d=is6.com; spf=pass"), "fail")
        # a genuine DMARC pass is still accepted
        self.assertEqual(verdict("mx.google.com; dkim=pass header.d=is6.com; dmarc=pass"), "pass")
        # a DMARC_OR_ALIGNED_DKIM broker (TradersWay) still accepts aligned DKIM without DMARC
        self.assertEqual(
            parse_rfc822(b"From: payments@tradersway.com\r\nAuthentication-Results: mx.google.com; "
                         b"dkim=pass header.d=tradersway.com; dmarc=fail\r\nSubject: s\r\n\r\nx")["auth_verdict"],
            "pass")

    def test_save_audit_within_atomic_ok(self):
        from django.db import transaction
        with transaction.atomic():
            bp = BrokerProfile.objects.create(name="X Broker", slug="xbroker", approved_sender_domains="x.example")
            self.assertTrue(BrokerProfileAudit.objects.filter(broker_profile=bp, action="CREATE").exists())
            self.assertTrue(BrokerProfile.objects.filter(pk=bp.pk).exists())   # enclosing txn healthy

    def test_config_change_is_audited_append_only(self):
        bp = BrokerProfile.objects.get(slug="is6")
        before = BrokerProfileAudit.objects.filter(broker_profile=bp).count()
        bp.server_names = "IS6-Live"
        bp.save()
        self.assertEqual(BrokerProfileAudit.objects.filter(broker_profile=bp).count(), before + 1)
        latest = BrokerProfileAudit.objects.filter(broker_profile=bp).order_by("-id").first()
        self.assertEqual(latest.action, "UPDATE")
        self.assertEqual(latest.snapshot.get("server_names"), "IS6-Live")
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            latest.delete()                         # append-only
