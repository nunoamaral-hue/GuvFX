"""
Phase 7A tests — Signal Intelligence Producer (Wayond).

Assert: immutable envelope, producer correctness, delivery creates a WIMS
ConsumptionContract with the full audited lifecycle, the existing pipeline
accepts the object, and ADR-009 holds (no producer-side models, no WIMS trade
objects).
"""

import dataclasses
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from intelligence.delivery import ingest_wayond_signal
from intelligence.envelope import SignalIntelligenceEnvelope
from intelligence.producer import SignalIntelligenceProducer
from wims import services
from wims.models import AuditEvent, ConsumptionContract, Content, Publish, Review

User = get_user_model()

SIGNAL = {
    "signal_id": "WAYOND-TEST-001",
    "market": "XAUUSD",
    "direction": "BUY",
    "entry": "3350.0",
    "stop_loss": "3335.0",
    "take_profit": "3370.0",
    "timestamp": "2026-06-14T08:00:00Z",
    "confidence": "72",
    "summary": "test setup",
}


class EnvelopeTests(TestCase):
    def test_producer_builds_envelope(self):
        env = SignalIntelligenceProducer().produce(SIGNAL)
        self.assertIsInstance(env, SignalIntelligenceEnvelope)
        self.assertEqual(env.intelligence_type, "SIGNAL")
        self.assertEqual(env.version, "1.0")
        self.assertEqual(env.source, "WAYOND")
        self.assertEqual(env.structured_payload.market, "XAUUSD")
        self.assertEqual(env.structured_payload.signal_id, "WAYOND-TEST-001")
        self.assertIn("structured_payload", env.to_dict())

    def test_envelope_is_immutable(self):
        env = SignalIntelligenceProducer().produce(SIGNAL)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            env.version = "2.0"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            env.structured_payload.entry = "9999"

    def test_producer_rejects_incomplete_signal(self):
        bad = dict(SIGNAL)
        del bad["entry"]
        with self.assertRaises(ValueError):
            SignalIntelligenceProducer().produce(bad)


class DeliveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="p7a", email="p7a@example.invalid", password="x"
        )

    def test_ingest_creates_contract_and_audits_lifecycle(self):
        envelope, contract = ingest_wayond_signal(SIGNAL, actor=self.user)

        # Consumption: a WAYOND ConsumptionContract was created from the envelope.
        self.assertEqual(ConsumptionContract.objects.count(), 1)
        self.assertEqual(contract.source_type, ConsumptionContract.SourceType.WAYOND)
        self.assertEqual(contract.symbol, "XAUUSD")
        self.assertEqual(contract.entry_price, Decimal("3350.0"))
        self.assertIn(envelope.intelligence_id, contract.source_reference)

        # All four lifecycle events recorded via the existing audit capability.
        events = set(AuditEvent.objects.values_list("event", flat=True))
        for e in (
            AuditEvent.Event.SIGNAL_RECEIVED,
            AuditEvent.Event.ENVELOPE_CREATED,
            AuditEvent.Event.ENVELOPE_DELIVERED,
            AuditEvent.Event.ENVELOPE_CONSUMED,
            AuditEvent.Event.CONTRACT_CREATED,  # WIMS' own consumption event
        ):
            self.assertIn(e, events)

    def test_existing_pipeline_accepts_consumed_object(self):
        _, contract = ingest_wayond_signal(SIGNAL, actor=self.user)
        ctx = services.create_context_from_contract(
            contract=contract, context_text="neutral education", actor=self.user
        )
        content = services.create_content(
            context=ctx, title="t", content_text="c", actor=self.user
        )
        services.submit_for_review(content=content, actor=self.user)
        services.review_content(
            content=content, decision=Review.Decision.APPROVE, reviewer=self.user
        )
        services.publish_content(
            content=content, channel=Publish.Channel.TELEGRAM, publisher=self.user
        )
        content.refresh_from_db()
        self.assertEqual(content.status, Content.Status.PUBLISHED)

    def test_adr009_no_producer_models(self):
        from django.apps import apps
        self.assertEqual(
            list(apps.get_app_config("intelligence").get_models()), [],
            "intelligence (producer) must persist no models — envelope is transient",
        )

    def test_demo_command_runs_and_passes(self):
        out = StringIO()
        call_command("produce_wayond_signal", stdout=out, stderr=StringIO())
        output = out.getvalue()
        self.assertIn("PASS", output)
        self.assertIn("Lifecycle OK", output)
        self.assertEqual(ConsumptionContract.objects.count(), 1)
