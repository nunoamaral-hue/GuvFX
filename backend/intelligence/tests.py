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


# --- Wayond Telegram content + winner -> results card -> WIMS packet --------
from intelligence import telegram_source as ts  # noqa: E402
from intelligence.delivery import (  # noqa: E402
    ingest_wayond_telegram_signal,
    ingest_winning_trade,
    is_winning_trade,
)
from intelligence.results_card import render_results_card, row_from_trade  # noqa: E402

WAYOND_MSG = (
    "NZDJPY | Potential upward movement\n\nNZDJPY | BUY 91.300\n\n"
    "❌ Stop Loss 90.900 (40 pips)\n\n✅ TP1 91.450\n✅ TP2 91.700\n✅ TP3 92.100"
)

WIN_TRADE = {
    "ticket": "100245789", "symbol": "EURUSD", "side": "BUY",
    "open_time": "2026-06-13T09:15:00Z", "close_time": "2026-06-13T11:45:00Z",
    "open_price": "1.10200", "close_price": "1.10850",
    "profit": "325.00", "commission": "-4.00", "swap": "-1.20",
}
LOSS_TRADE = dict(WIN_TRADE, profit="-120.00", close_price="1.10080")


class WayondTelegramParserTests(TestCase):
    def test_parses_new_signal(self):
        p = ts.parse_message(WAYOND_MSG, "m1")
        self.assertEqual(p.kind, ts.Kind.SIGNAL)
        self.assertEqual(p.market, "NZDJPY")
        self.assertEqual(p.direction, "BUY")
        self.assertEqual(p.entry, "91.300")
        self.assertEqual(p.stop_loss, "90.900")
        self.assertEqual(p.take_profits, ("91.450", "91.700", "92.100"))
        self.assertTrue(p.is_tradeable_shape())

    def test_tp_hit_is_update_not_signal(self):
        p = ts.parse_message("TP1 hit! That gives us +300 pips.\nMove SL to 59200", "m2")
        self.assertEqual(p.kind, ts.Kind.UPDATE)
        self.assertEqual(p.update_type, "TP_HIT")
        self.assertFalse(p.is_tradeable_shape())

    def test_chitchat_is_quarantined(self):
        p = ts.parse_message("Good luck everyone, trade safe.", "m3")
        self.assertEqual(p.kind, ts.Kind.UNKNOWN)
        self.assertTrue(p.reason)

    def test_classify_dedups_and_quarantines(self):
        msgs = [
            {"message_id": "a", "text": WAYOND_MSG},
            {"message_id": "a", "text": WAYOND_MSG},   # duplicate id
            {"message_id": "b", "text": "random noise"},
        ]
        plan = ts.classify_messages(msgs, seen_ids=set())
        self.assertEqual(len(plan.signals), 1)
        self.assertEqual(plan.duplicates, ["a"])
        self.assertEqual(len(plan.quarantined), 1)

    def test_seen_ids_skip_already_ingested(self):
        plan = ts.classify_messages([{"message_id": "a", "text": WAYOND_MSG}], seen_ids={"a"})
        self.assertEqual(len(plan.signals), 0)
        self.assertEqual(plan.duplicates, ["a"])


class WayondContentIngestionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="wtg", email="wtg@example.invalid", password="x"
        )

    def test_parsed_signal_becomes_wayond_content_contract(self):
        p = ts.parse_message(WAYOND_MSG, "m1")
        env, contract = ingest_wayond_telegram_signal(p, actor=self.user)
        self.assertEqual(contract.source_type, ConsumptionContract.SourceType.WAYOND)
        self.assertEqual(contract.symbol, "NZDJPY")
        # content-only: no trade-result/outcome data on a fresh signal contract
        self.assertEqual(contract.result_type, "")

    def test_command_is_content_only_and_idempotent(self):
        out = StringIO()
        call_command("ingest_wayond_telegram", stdout=out, stderr=StringIO())
        first = ConsumptionContract.objects.count()
        self.assertGreaterEqual(first, 1)
        self.assertIn("0 trades placed", out.getvalue())
        # re-run: deduped, no new contracts
        call_command("ingest_wayond_telegram", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(ConsumptionContract.objects.count(), first)


class WinningTradePacketTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="win", email="win@example.invalid", password="x"
        )

    def test_winner_creates_packet_with_results_card_media(self):
        env, contract = ingest_winning_trade(WIN_TRADE, actor=self.user)
        self.assertEqual(contract.result_type, "WIN")
        card = contract.media.get("results_card", {})
        self.assertEqual(card.get("format"), "svg")
        self.assertIn("<svg", card.get("svg", ""))
        self.assertIn("Profit:", card["svg"])

    def test_loser_is_rejected_and_never_persisted(self):
        self.assertFalse(is_winning_trade(LOSS_TRADE))
        with self.assertRaises(ValueError):
            ingest_winning_trade(LOSS_TRADE, actor=self.user)
        self.assertEqual(ConsumptionContract.objects.count(), 0)

    def test_results_card_renders_from_trade(self):
        svg = render_results_card([row_from_trade(WIN_TRADE)], title="EURUSD BUY — WIN")
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("EURUSD", svg)
        self.assertIn("Profit:", svg)

    def test_publish_command_holds_at_review_gate(self):
        out = StringIO()
        call_command("publish_winning_trade", stdout=out, stderr=StringIO())
        output = out.getvalue()
        self.assertIn("PASS", output)
        self.assertIn("human-review gate", output)
        # nothing published without approval
        self.assertEqual(Publish.objects.count(), 0)
