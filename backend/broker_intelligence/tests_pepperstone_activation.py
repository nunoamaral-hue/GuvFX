"""GFX-PEPPERSTONE-WITHDRAWAL-DEMO-003 — Pepperstone activation (migration 0027) tests.

Migration 0027 flips the Pepperstone ``BrokerProfile`` to ``is_active=True`` so an authenticated Pepperstone
withdrawal email is admitted to the acquisition filter + retention gate and surfaces truthfully. This module proves
the ACTIVATION is correct and does NOT broaden recognition or weaken fail-closed handling.

The Pepperstone parser's own lifecycle correctness — "confirm your withdrawal request" opens at
CONFIRMATION_REQUIRED and is NEVER completed, GBP 15.00 extraction, URL/token redaction, idempotent correlation,
completion-before-request never fabricates — is already proven in ``tests_parsers_stream_b`` and
``tests_stream_b_scenario``; the REAL-mail retention + sender-authenticity gates are proven in ``tests_ingestion``.
Those are the (sender-agnostic) code paths this migration enables for Pepperstone; here we assert the activation
wiring plus a parser sanity-check tying it to the genuine GBP 15 scenario.
"""
from decimal import Decimal

from django.test import TestCase

from broker_intelligence.broker_senders import is_allowlisted_broker_sender, sender_allowlist
from broker_intelligence.external_withdrawal_classifier import CERTIFIED_BROKERS
from broker_intelligence.models import BrokerProfile, BrokerProfileAudit
from broker_intelligence.parsers_stream_b import PEPPERSTONE

# Genuine-SHAPED but SYNTHETIC Pepperstone confirmation-required email (mirrors the real GBP 15 request; no real
# template, account number, reference or address is reproduced).
PS_CONFIRMATION_REQUIRED = dict(
    from_address="no-reply@pepperstone.com",
    subject="Action required: confirm your withdrawal",
    body=("Hello,\n\nPlease confirm your withdrawal request.\n"
          "Amount: GBP 15.00\nWithdrawal method: Visa card\nReference: PSSYNTH-REF-001\n"
          "Confirm: https://pepperstone.com/confirm?token=SYNTH_PLACEHOLDER_DO_NOT_FOLLOW\n"))
PS_PROCESSING = dict(
    from_address="no-reply@pepperstone.com",
    subject="Your withdrawal is being processed",
    body="Your withdrawal is being processed.\nAmount: GBP 15.00\nReference: PSSYNTH-REF-001\n")
PS_COMPLETED = dict(
    from_address="no-reply@pepperstone.com",
    subject="Your withdrawal is complete",
    body=("Your withdrawal has been completed.\nFunds have been sent to your Visa card.\n"
          "Amount: GBP 15.00\n"))


class PepperstoneActivationMigrationTests(TestCase):
    """Migration 0027 has run against the test database, so Pepperstone is active here."""

    def test_profile_is_active_and_operator_verified(self):
        bp = BrokerProfile.objects.get(slug="pepperstone")
        self.assertTrue(bp.is_active)
        self.assertEqual(bp.provenance, "OPERATOR_VERIFIED")
        self.assertEqual(bp.auth_policy, "DMARC_OR_ALIGNED_DKIM")
        self.assertIn("pepperstone.com", bp.approved_domains())

    def test_activation_is_audited_append_only(self):
        bp = BrokerProfile.objects.get(slug="pepperstone")
        self.assertTrue(
            BrokerProfileAudit.objects.filter(broker_profile=bp, action="UPDATE",
                                              changed_by__icontains="0027").exists())

    def test_pepperstone_now_in_allowlist(self):
        allow = sender_allowlist()
        self.assertIn("pepperstone.com", allow)
        self.assertTrue(is_allowlisted_broker_sender("no-reply@pepperstone.com"))

    def test_failclosed_preserved_lookalike_and_nonbroker_rejected(self):
        # Activation must NOT open a hole: a non-broker sender and any look-alike of pepperstone.com stay rejected.
        self.assertFalse(is_allowlisted_broker_sender("no-reply@evil.example"))
        self.assertFalse(is_allowlisted_broker_sender("no-reply@pepperstone.com.evil.example"))
        self.assertFalse(is_allowlisted_broker_sender("no-reply@notpepperstone.com"))
        self.assertFalse(is_allowlisted_broker_sender("no-reply@pepperstone.co"))
        self.assertFalse(is_allowlisted_broker_sender(""))
        self.assertFalse(is_allowlisted_broker_sender(None))

    def test_activation_does_not_certify_broker(self):
        # The member dashboard surfaces REAL EXTERNAL_WITHDRAWAL events on their own merit; the public-metrics
        # CERTIFIED_BROKERS path is separate and genuine-sample-gated. This migration must never certify.
        self.assertNotIn("Pepperstone", CERTIFIED_BROKERS)

    def test_dominion_and_vantage_stay_dark(self):
        self.assertFalse(BrokerProfile.objects.get(slug="dominion_markets").is_active)
        self.assertFalse(BrokerProfile.objects.get(slug="vantage").is_active)
        allow = sender_allowlist()
        self.assertNotIn("dominionmarkets.com", allow)
        self.assertNotIn("vantagemarkets.com", allow)


class PepperstoneGenuineScenarioParserTests(TestCase):
    """The genuine 2026-11-05 email is a 'confirm your withdrawal request' message; prove the activated adapter
    renders a TRUTHFUL lifecycle state for it (request/confirmation, never completion)."""

    def test_confirmation_required_is_external_withdrawal_gbp15_never_completed(self):
        p = PEPPERSTONE.parse(**PS_CONFIRMATION_REQUIRED)
        self.assertIsNotNone(p)
        self.assertEqual(p.event_type, "WITHDRAWAL_CONFIRMATION_REQUIRED")  # NOT requested-complete, NOT completed
        self.assertEqual(p.transaction_category, "EXTERNAL_WITHDRAWAL")
        self.assertEqual(p.amount, Decimal("15.00"))
        self.assertEqual(p.currency, "GBP")
        self.assertEqual(p.broker, "Pepperstone")

    def test_processing_and_completed_render_their_own_states(self):
        self.assertEqual(PEPPERSTONE.parse(**PS_PROCESSING).event_type, "WITHDRAWAL_PROCESSING")
        self.assertEqual(PEPPERSTONE.parse(**PS_COMPLETED).event_type, "WITHDRAWAL_COMPLETED")

    def test_confirmation_url_and_token_never_surface(self):
        p = PEPPERSTONE.parse(**PS_CONFIRMATION_REQUIRED)
        blob = repr(p).lower()
        self.assertNotIn("http", blob)
        self.assertNotIn("token", blob)
        self.assertNotIn("synth_placeholder", blob)

    def test_unknown_template_quarantines_no_event(self):
        # A Pepperstone message that is not a recognised withdrawal template must yield NO event (fail-closed),
        # never a guessed/misleading record.
        self.assertIsNone(PEPPERSTONE.parse(
            from_address="no-reply@pepperstone.com",
            subject="Your monthly account statement is ready",
            body="Your statement for last month is now available to download in the client portal.\n"))
