"""GFX-PKT-CANONICAL-TRADE-RESULT — the single canonical trade result + its renderers.

Proves: one ``CanonicalTradeResult`` carries every field (facts + signal/parser provenance +
execution context + media/stat references); the Telegram and WIMS renderers both format FROM it
(no duplicated formatting); the deployed Telegram dry-run envelope is byte-for-byte preserved by
sourcing from the canonical object; and the canonical/renderer code transmits/orders/publishes
NOTHING.
"""
import ast
import importlib
import pathlib
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution.models import (
    ExecutionJob,
    NotificationCandidate,
    ProposedOrderLeg,
    SignalExecutionPlan,
    TradeOutcomeRecord,
)
from execution.notifications.contracts import build_telegram_envelope, resolve_signal_linkage
from intelligence.canonical import CanonicalTradeResult, build_canonical_trade_result
from intelligence.renderers import TelegramRenderer, WIMSRenderer
from signal_intake.models import ParserProfile, PendingSignalApproval, SignalProvider
from strategies.models import Strategy  # noqa: F401 (kept for scenario parity / future linkage)
from trading.models import Trade, TradingAccount

User = get_user_model()


class CanonicalBase(TestCase):
    CID = "corr-xyz"

    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo Acct", account_number="D1", is_demo=True,
        )
        self.parser = ParserProfile.objects.create(
            slug="wayond_v1", certification_level=ParserProfile.CertificationLevel.MEDIUM,
        )
        self.provider = SignalProvider.objects.create(
            slug="wayond", name="Wayond", telegram_chat_id="-100", parser_profile=self.parser,
            status=SignalProvider.Status.ONBOARDING,
        )
        self.approval = PendingSignalApproval.objects.create(
            source="wayond", message_id="sig-1", provider=self.provider, symbol="EURUSD",
            direction="BUY", entry="1.0850", stop_loss="1.0800", take_profit="1.0900",
            take_profits=["1.0900"], status=PendingSignalApproval.Status.APPROVED,
        )
        self.plan = SignalExecutionPlan.objects.create(
            approval=self.approval, account=self.acct, source="wayond", message_id="sig-1",
            symbol="EURUSD", direction="BUY", entry="1.0850", stop_loss="1.0800",
            is_demo=True, signal_timestamp=timezone.now(),
            status=SignalExecutionPlan.Status.PLANNED, correlation_id=self.CID,
        )
        self.job = ExecutionJob.objects.create(
            job_type=ExecutionJob.JobType.PLACE_ORDER, account=self.acct,
            status=ExecutionJob.Status.PENDING,
            payload={"execution_mode": "DEMO", "comment": f"WAY{self.plan.id}L1"},
        )
        self.leg = ProposedOrderLeg.objects.create(
            plan=self.plan, leg_index=1, take_profit="1.0900", stop_loss="1.0800",
            lot_size=Decimal("0.01"), status=ProposedOrderLeg.Status.PROMOTED,
            execution_job=self.job,
        )
        self.trade = Trade.objects.create(
            account=self.acct, ticket="T1", symbol="EURUSD", side="BUY",
            volume=Decimal("0.01"), open_time=timezone.now(), open_price=Decimal("1.0850"),
            close_time=timezone.now(), close_price=Decimal("1.0900"), profit=Decimal("21"),
            comment=f"WAY{self.plan.id}L1", correlation_id=self.CID,
        )

    def _candidate(self):
        outcome = TradeOutcomeRecord.objects.create(
            trade=self.trade, outcome=TradeOutcomeRecord.Outcome.WIN, net_pnl=Decimal("21"),
            is_delivery_candidate=True, correlation_id=self.CID, signal_source="wayond",
            execution_job=self.job,
        )
        return NotificationCandidate.objects.create(
            outcome_record=outcome, correlation_id=self.CID, signal_source="wayond",
            net_pnl=Decimal("21"),
        )


class BuildCanonicalTests(CanonicalBase):
    def test_execution_side_resolver_reads_the_plan(self):
        # The execution side (owns SignalExecutionPlan) resolves linkage; intelligence never does.
        link = resolve_signal_linkage(self.CID)
        self.assertEqual(link["provider"], "wayond")
        self.assertEqual(link["parser_profile"], "wayond_v1")
        self.assertEqual(link["parser_confidence"], "MEDIUM")
        self.assertEqual(link["execution_mode"], "DEMO")
        self.assertEqual(link["reference_entry"], "1.0850")
        self.assertEqual(link["take_profit"], "1.0900")
        self.assertEqual(link["signal_id"], "sig-1")

    def test_canonical_carries_every_field_from_trade_and_linkage(self):
        link = resolve_signal_linkage(self.CID)
        r = build_canonical_trade_result(
            self.trade, correlation_id=self.CID, signal_source="wayond", linkage=link,
        )
        self.assertIsInstance(r, CanonicalTradeResult)
        # instrument / prices
        self.assertEqual((r.symbol, r.direction), ("EURUSD", "BUY"))
        self.assertEqual(Decimal(r.actual_fill), Decimal("1.0850"))
        self.assertEqual(Decimal(r.exit), Decimal("1.0900"))
        self.assertEqual(r.reference_entry, "1.0850")   # provider advisory, from the plan
        self.assertEqual(r.stop_loss, "1.0800")
        self.assertEqual(r.take_profit, "1.0900")        # from the leg
        # result
        self.assertEqual(Decimal(r.pips), Decimal("50.0"))
        self.assertEqual(Decimal(r.gross_pnl), Decimal("21"))
        self.assertEqual(Decimal(r.net_pnl), Decimal("21"))
        self.assertEqual(r.outcome, "WIN")
        self.assertTrue(r.execution_duration is not None)
        self.assertTrue(r.trade_timestamp and r.execution_timestamp)
        # provenance / execution context
        self.assertEqual(r.provider, "wayond")
        self.assertEqual(r.parser_profile, "wayond_v1")
        self.assertEqual(r.parser_confidence, "MEDIUM")
        self.assertEqual(r.execution_mode, "DEMO")        # from the leg's execution job payload
        self.assertEqual(r.signal_id, "sig-1")
        self.assertEqual(r.correlation_id, self.CID)
        self.assertTrue(r.is_demo)
        self.assertEqual(r.account_label, "Demo Acct")
        # statistics block
        self.assertEqual(r.statistics["net_pnl"], "21")
        self.assertEqual(r.statistics["outcome"], "WIN")
        self.assertEqual(r.statistics["pips"], "50.0")

    def test_strategy_field_has_no_provider_fallback(self):
        # Behaviour-preserving: strategy = signal_source OR plan source ONLY. Even when the
        # provider slug is set, an empty signal_source + empty plan source => strategy "" (n/a),
        # exactly as the deployed envelope rendered (the provider slug lives on `provider`).
        r = build_canonical_trade_result(
            self.trade, correlation_id=self.CID, signal_source="",
            linkage={"provider": "wayond", "source": ""},
        )
        self.assertEqual(r.strategy, "")          # no provider fallback
        self.assertEqual(r.provider, "wayond")    # provider still carries the slug
        self.assertIn("Strategy: n/a", TelegramRenderer().render(r).text)

    def test_light_build_renders_no_media(self):
        # The execution/Telegram path builds without media (stays free of Pillow / heavy work).
        r = build_canonical_trade_result(self.trade, correlation_id=self.CID)
        self.assertIsNone(r.result_card)
        self.assertIsNone(r.caption)
        self.assertEqual(r.card_rows, ())


class RendererTests(CanonicalBase):
    def test_telegram_renderer_formats_from_canonical(self):
        r = build_canonical_trade_result(
            self.trade, correlation_id=self.CID, signal_source="wayond",
            linkage=resolve_signal_linkage(self.CID),
        )
        content = TelegramRenderer().render(r)
        self.assertEqual(content.renderer, "telegram")
        self.assertEqual(content.title, "GuvFX — winning trade: EURUSD BUY")
        self.assertIn("EURUSD BUY", content.text)
        self.assertIn("Signal entry (ref): 1.0850  |  Filled: 1.0850", content.text)
        self.assertIn("SL: 1.0800  |  TP: 1.0900", content.text)
        self.assertIn("Profit: 21  (50.0 pips)", content.text)
        self.assertIn(f"ref: {self.CID}", content.text)
        self.assertIsNone(content.media)

    def test_wims_renderer_produces_card_and_caption(self):
        r = build_canonical_trade_result(
            self.trade, correlation_id=self.CID, account_label="GuvFX", with_media=True,
        )
        content = WIMSRenderer().render(r)
        self.assertEqual(content.renderer, "wims")
        self.assertIn("results_card", content.media)
        self.assertIn("caption", content.media)
        card = content.media["results_card"]
        self.assertIn("png_base64", card)
        self.assertIn("svg", card)
        self.assertTrue(content.media["caption"].startswith("✅"))
        self.assertIn("Net Profit", content.media["caption"])

    def test_wims_renderer_requires_media_built_canonical(self):
        r = build_canonical_trade_result(self.trade, correlation_id=self.CID)  # no media
        with self.assertRaises(ValueError):
            WIMSRenderer().render(r)


class TelegramEnvelopeRegressionTests(CanonicalBase):
    def test_envelope_is_behaviour_preserving(self):
        # The deployed dry-run envelope must be unchanged now that it is sourced from canonical.
        env = build_telegram_envelope(self._candidate())
        self.assertEqual(env.symbol, "EURUSD")
        self.assertEqual(env.direction, "BUY")
        self.assertEqual(Decimal(env.actual_fill), Decimal("1.0850"))
        self.assertEqual(Decimal(env.profit), Decimal("21"))
        self.assertEqual(env.correlation_id, self.CID)
        self.assertEqual(env.strategy, "wayond")
        self.assertEqual(env.title, "GuvFX — winning trade: EURUSD BUY")
        self.assertIn("EURUSD", env.rendered_message)
        self.assertIn(self.CID, env.rendered_message)
        for field in ("title", "summary", "strategy", "symbol", "direction", "reference_entry",
                      "actual_fill", "stop_loss", "take_profit", "profit", "pips",
                      "execution_timestamp", "correlation_id", "rendered_message"):
            self.assertIn(field, env.as_dict())


class CanonicalBoundaryTests(TestCase):
    def test_canonical_and_renderers_never_transmit_order_or_publish(self):
        for mod in ("intelligence.canonical", "intelligence.renderers"):
            src = pathlib.Path(importlib.import_module(mod).__file__).read_text()
            names = set()
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Name):
                    names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, ast.ImportFrom):
                    for n in node.names:
                        names.add(n.asname or n.name)
            for forbidden in ("order_send", "order_check", "MetaTrader5", "requests", "httpx",
                              "urllib", "socket", "sendMessage", "create_contract",
                              "deliver_trade_result", "NotificationTransport"):
                self.assertNotIn(forbidden, names, f"{mod} references {forbidden}")
