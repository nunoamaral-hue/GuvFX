"""PR4 — the read-only dual-broker ingestion readiness command: reports preconditions + a verdict, and ENABLES
NOTHING (ingestion stays DARK, no records written, no broker contacted)."""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from broker_intelligence import parsers
from broker_intelligence.flags import broker_intelligence_ingest_enabled
from broker_intelligence.models import BrokerEvent, ConnectedMailbox

User = get_user_model()


def _connected_mailbox():
    u = User.objects.create_user(username="rdy", email="rdy@x.invalid", password="x")
    return ConnectedMailbox.objects.create(user=u, provider=ConnectedMailbox.Provider.GMAIL,
                                           provider_mailbox_id="g-rdy", primary_email="rdy@gmail.com",
                                           status=ConnectedMailbox.Status.CONNECTED)


@override_settings(GMAIL_OAUTH_CLIENT_ID="test-client-id", GMAIL_OAUTH_CLIENT_SECRET="test-secret")
class IngestReadinessTests(TestCase):
    def setUp(self):
        self.addCleanup(parsers.clear_registry)   # the command registers parsers; don't leak across tests

    def _run(self):
        out = StringIO()
        call_command("withdrawal_ingest_readiness", stdout=out)
        return out.getvalue()

    def test_capture_ready_when_preconditions_met(self):
        _connected_mailbox()
        text = self._run()
        self.assertIn("[PASS] connected_mailbox", text)
        self.assertIn("[PASS] parsers_registered", text)      # BOTH TradersWay + IS6
        self.assertIn("[PASS] broker_profiles_active", text)  # seeded is6 + tradersway
        self.assertIn("[PASS] sender_allowlist", text)
        self.assertIn("[PASS] oauth_client_credentials", text)
        self.assertIn("verdict: CAPTURE-READY", text)

    def test_reports_dark_and_capture_only(self):
        _connected_mailbox()
        text = self._run()
        self.assertIn("OFF (DARK)", text)                     # ingestion stays DARK
        self.assertIn("capture-only", text)                   # no positive classifier certified
        self.assertIn("ENABLES NOTHING", text)

    def test_not_ready_without_connected_mailbox(self):
        text = self._run()                                    # no mailbox
        self.assertIn("[FAIL] connected_mailbox", text)
        self.assertIn("verdict: NOT CAPTURE-READY", text)

    def test_enables_nothing(self):
        _connected_mailbox()
        self._run()
        self.assertFalse(broker_intelligence_ingest_enabled())   # flag untouched
        self.assertEqual(BrokerEvent.objects.count(), 0)         # nothing ingested/written

    @override_settings(GMAIL_OAUTH_CLIENT_ID="", GMAIL_OAUTH_CLIENT_SECRET="")
    def test_missing_oauth_is_not_ready(self):
        _connected_mailbox()
        text = self._run()
        self.assertIn("[FAIL] oauth_client_credentials", text)
        self.assertIn("verdict: NOT CAPTURE-READY", text)
