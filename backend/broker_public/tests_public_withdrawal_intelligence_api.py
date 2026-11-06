"""broker_public — Stream C public withdrawal-intelligence API tests (ADR-0080).

Proves the DARK posture at the HTTP boundary: direct URL access returns 404 while the flag is OFF (the payload is
never served — not even in a client bundle), an unknown/unpublished broker is indistinguishable from DARK, an armed
but UNCERTIFIED broker returns a reason-coded UNAVAILABLE (never member data or a fabricated zero), the certified
published path returns aggregate-only data with no PII and no exact timestamp, and the endpoint is unauthenticated
(AllowAny). Also regresses the _broker_json free-text hardening. SYNTHETIC ONLY.
"""
from __future__ import annotations

from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from broker_intelligence import external_withdrawal_classifier
from broker_public.disclosure import Reason, State
from broker_public.models import PublicBroker
from broker_public.tests_withdrawal_intelligence import CERT, _broker, _category, _published_cohort

URL = "/api/public/brokers/%s/withdrawal-intelligence/"
_CERT_PATCH = mock.patch.object(external_withdrawal_classifier, "CERTIFIED_BROKERS", CERT)


class WithdrawalIntelligenceApiDark(TestCase):
    def test_404_when_flag_off_even_for_published_certified_broker(self):
        _broker()
        _category()
        _published_cohort()
        c = APIClient()
        self.assertEqual(c.get(URL % "pepperstone").status_code, 404)   # payload never served while DARK

    @override_settings(BROKER_PUBLIC_WITHDRAWAL_INTELLIGENCE_ENABLED="1")
    def test_unknown_slug_is_404(self):
        self.assertEqual(APIClient().get(URL % "does-not-exist").status_code, 404)

    @override_settings(BROKER_PUBLIC_WITHDRAWAL_INTELLIGENCE_ENABLED="1")
    def test_unpublished_broker_is_404_indistinguishable_from_dark(self):
        _broker(published=False)
        self.assertEqual(APIClient().get(URL % "pepperstone").status_code, 404)


@override_settings(BROKER_PUBLIC_WITHDRAWAL_INTELLIGENCE_ENABLED="1")
class WithdrawalIntelligenceApiArmed(TestCase):
    def test_armed_but_uncertified_is_unavailable_not_member_data(self):
        _broker()
        _category()
        _published_cohort()   # real cohort exists, but CERTIFIED_BROKERS is empty by default
        r = APIClient().get(URL % "pepperstone")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["evidence_status"], {"state": State.UNAVAILABLE, "reason": Reason.NOT_CERTIFIED})
        self.assertNotIn("sample_sufficiency", body)         # no counts on the withheld path
        self.assertNotIn("withdrawal_speed", body)

    def test_certified_published_path_is_aggregate_only(self):
        _broker()
        _category()
        accts = _published_cohort()
        with _CERT_PATCH:
            r = APIClient().get(URL % "pepperstone")   # unauthenticated — AllowAny
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["evidence_status"]["state"], State.PUBLISHED)
        self.assertEqual(body["sample_sufficiency"]["observations"], 5)
        # every public timestamp is date-granular (no sub-day precision / no ISO 'T')
        self.assertNotIn("T", body["observation_window"]["end"] or "")
        flat = str(body)
        # the ACTUAL member identifiers from the contributing accounts must not appear anywhere in the payload
        for a in accts:
            self.assertNotIn(a.account_number, flat)
            self.assertNotIn(a.user.email, flat)
        # structural: no identifying KEYS are present
        for key in ("account_number", "user_id", "broker_reference_id", "trading_account"):
            self.assertNotIn(key, flat)

    def test_endpoint_is_unauthenticated(self):
        _broker()
        _category()
        # No credentials set on the client; AllowAny must answer (not 401/403).
        self.assertEqual(APIClient().get(URL % "pepperstone").status_code, 200)


@override_settings(BROKER_PUBLIC_DIRECTORY_ENABLED="1")
class BrokerJsonTextHardening(TestCase):
    def test_malicious_broker_name_is_escaped_in_directory(self):
        PublicBroker.objects.create(slug="x", display_name="<script>alert(1)</script>", source_slug="X",
                                    summary="see http://evil.test/p", website="javascript:alert(1)", is_published=True)
        body = APIClient().get("/api/public/brokers/").json()
        entry = next(b for b in body["brokers"] if b["slug"] == "x")
        self.assertNotIn("<script>", entry["display_name"])
        self.assertIn("&lt;script&gt;", entry["display_name"])
        self.assertIn("[REDACTED]", entry["summary"])        # URL in free text redacted
        self.assertEqual(entry["website"], "")               # javascript: URL rejected by the allow-list
