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

from types import SimpleNamespace

from intelligence.delivery import ingest_trade_result, ingest_wayond_signal
from intelligence.envelope import (
    SignalIntelligenceEnvelope,
    TradeResultIntelligenceEnvelope,
)
from intelligence.producer import SignalIntelligenceProducer
from intelligence.trade_result_producer import TradeResultProducer
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


# --- Phase 7B — Trade Result Intelligence ---------------------------------
from datetime import datetime, timezone  # noqa: E402

CLOSED_TRADE = {
    "ticket": "100245789",
    "symbol": "EURUSD",
    "side": "BUY",
    "open_time": "2026-06-13T09:15:00Z",
    "close_time": "2026-06-13T11:45:00Z",
    "open_price": "1.10200",
    "close_price": "1.10850",
    "profit": "325.00",
    "commission": "-4.00",
    "swap": "-1.20",
    "signal_id": "WAYOND-2026-0613-007",
}


def _trade_like_object():
    """A duck-typed stand-in matching trading.models.Trade field shape."""
    return SimpleNamespace(
        ticket="100245789",
        symbol="EURUSD",
        side="BUY",
        open_time=datetime(2026, 6, 13, 9, 15, tzinfo=timezone.utc),
        close_time=datetime(2026, 6, 13, 11, 45, tzinfo=timezone.utc),
        open_price=Decimal("1.10200"),
        close_price=Decimal("1.10850"),
        profit=Decimal("325.00"),
        commission=Decimal("-4.00"),
        swap=Decimal("-1.20"),
        magic_number=220614,
        comment="wayond",
    )


class TradeResultEnvelopeTests(TestCase):
    def test_producer_builds_envelope_from_mapping(self):
        env = TradeResultProducer().produce(CLOSED_TRADE)
        self.assertIsInstance(env, TradeResultIntelligenceEnvelope)
        self.assertEqual(env.intelligence_type, "TRADE_RESULT")
        self.assertEqual(env.version, "1.0")
        self.assertEqual(env.source, "GUVFX_TRADE_HISTORY")
        self.assertEqual(env.structured_payload.market, "EURUSD")
        self.assertEqual(env.structured_payload.outcome, "WIN")  # net 319.80 > 0
        self.assertEqual(env.structured_payload.pips, "65.0")    # 0.0065 / 0.0001

    def test_producer_handles_trade_like_object(self):
        env = TradeResultProducer().produce(_trade_like_object())
        self.assertEqual(env.structured_payload.trade_id, "100245789")
        self.assertEqual(env.structured_payload.outcome, "WIN")
        self.assertTrue(env.structured_payload.duration)  # datetime delta computed

    def test_envelope_is_immutable(self):
        env = TradeResultProducer().produce(CLOSED_TRADE)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            env.version = "2.0"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            env.structured_payload.pnl = "0"

    def test_producer_rejects_open_trade(self):
        open_trade = dict(CLOSED_TRADE)
        open_trade["close_time"] = None
        open_trade["close_price"] = None
        with self.assertRaises(ValueError):
            TradeResultProducer().produce(open_trade)


class TradeResultDeliveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="p7b", email="p7b@example.invalid", password="x"
        )

    def test_ingest_creates_trade_result_contract_and_audits(self):
        envelope, contract = ingest_trade_result(CLOSED_TRADE, actor=self.user)
        self.assertEqual(ConsumptionContract.objects.count(), 1)
        self.assertEqual(contract.source_type, ConsumptionContract.SourceType.TRADE_RESULT)
        self.assertEqual(contract.result_type, "WIN")
        self.assertEqual(contract.symbol, "EURUSD")
        self.assertIn(envelope.intelligence_id, contract.source_reference)

        events = set(AuditEvent.objects.values_list("event", flat=True))
        for e in (
            AuditEvent.Event.TRADE_DETECTED,
            AuditEvent.Event.ENVELOPE_CREATED,
            AuditEvent.Event.ENVELOPE_DELIVERED,
            AuditEvent.Event.ENVELOPE_CONSUMED,
            AuditEvent.Event.CONTRACT_CREATED,
        ):
            self.assertIn(e, events)

    def test_existing_pipeline_accepts_consumed_trade_result(self):
        _, contract = ingest_trade_result(CLOSED_TRADE, actor=self.user)
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

    def test_demo_command_runs_and_passes(self):
        out = StringIO()
        call_command("produce_trade_result", stdout=out, stderr=StringIO())
        output = out.getvalue()
        self.assertIn("PASS", output)
        self.assertIn("Lifecycle OK", output)
        self.assertEqual(ConsumptionContract.objects.count(), 1)
