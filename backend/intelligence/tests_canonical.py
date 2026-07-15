"""GFX-PKT-CANONICAL-TRADE-RESULT — the single canonical trade result + its renderers.

Proves: one ``CanonicalTradeResult`` carries every field (facts + signal/parser provenance +
execution context + media/stat references); the Telegram and WIMS renderers both format FROM it
(no duplicated formatting); the deployed Telegram dry-run envelope is byte-for-byte preserved by
sourcing from the canonical object; and the canonical/renderer code transmits/orders/publishes
NOTHING.
"""
import ast
import base64
import importlib
import pathlib
import types
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
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
        # Redesigned 3-section report (single-leg fallback: no leg_evidence supplied here).
        r = build_canonical_trade_result(
            self.trade, correlation_id=self.CID, signal_source="wayond",
            linkage=resolve_signal_linkage(self.CID),
        )
        content = TelegramRenderer().render(r)
        self.assertEqual(content.renderer, "telegram")
        self.assertEqual(content.title, "WAYOND TRADE RESULT — WIN")
        # Section 1 — executive summary
        self.assertIn("Instrument: EURUSD", content.text)
        self.assertIn("Direction: BUY", content.text)
        self.assertIn("Net profit so far: +$21.00", content.text)
        self.assertIn("+50.0 pips", content.text)
        # Section 2 — trade evidence (TP labels, not "Leg"; per-TP pips)
        self.assertIn("📊 TRADE EVIDENCE", content.text)
        self.assertIn("✅ TP1", content.text)
        self.assertNotIn("Leg 1", content.text)
        self.assertIn("1.0850 → 1.0900", content.text)
        # Section 3 — TRADE ANALYSIS (renamed from Execution Analysis)
        self.assertIn("🔎 TRADE ANALYSIS", content.text)
        self.assertNotIn("EXECUTION ANALYSIS", content.text)
        self.assertIn("Reference entry: 1.0850", content.text)
        self.assertIn("Actual fill: 1.0850", content.text)
        self.assertIn("Stop loss: 1.0800", content.text)
        self.assertIn("Take profits: TP1 1.0900", content.text)
        # requirement 6: execution mode + correlation id are HIDDEN from stakeholder output...
        self.assertNotIn("Correlation id", content.text)
        self.assertNotIn(self.CID, content.text)
        self.assertNotIn("Execution mode", content.text)
        self.assertIn("No manual intervention occurred.", content.text)
        # ...but requirement 10: they REMAIN on the canonical for internal audit
        self.assertEqual(r.correlation_id, self.CID)
        self.assertEqual(r.execution_mode, "DEMO")
        # no marketing language
        for banned in ("Excellent", "perfectly", "delivered"):
            self.assertNotIn(banned, content.text)
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
        # The envelope CONTRACT fields are unchanged; the rendered_message is the redesign.
        # One leg, closed -> a FINAL card via the leg resolver.
        self.assertEqual(env.title, "WAYOND TRADE RESULT — FINAL WIN")
        self.assertIn("EURUSD", env.rendered_message)
        self.assertIn("TRADE ANALYSIS", env.rendered_message)
        # correlation id stays on the envelope FIELD (internal audit) but is hidden from the message
        self.assertEqual(env.correlation_id, self.CID)
        self.assertNotIn(self.CID, env.rendered_message)
        for field in ("title", "summary", "strategy", "symbol", "direction", "reference_entry",
                      "actual_fill", "stop_loss", "take_profit", "profit", "pips",
                      "execution_timestamp", "correlation_id", "rendered_message"):
            self.assertIn(field, env.as_dict())


class ProgressiveLegEvidenceTests(CanonicalBase):
    """GFX-PKT-CANONICAL-LEG-EVIDENCE — a card per profitable leg close, showing closed + pending."""

    def _legev(self, closed):
        tps = ["2367.50", "2371.50", "2375.50"]
        profs = ["42.88", "43.11", "42.65"]
        legs = []
        for i in range(3):
            if i < closed:
                legs.append(dict(index=i + 1, tp_label=f"TP{i + 1}", direction="BUY", volume="0.02",
                                 entry="2362.45", target=tps[i], exit=tps[i], profit=profs[i],
                                 status="CLOSED", close_time="2026-07-07T13:00:00+00:00"))
            else:
                legs.append(dict(index=i + 1, tp_label=f"TP{i + 1}", direction="BUY", volume="0.02",
                                 entry="2362.45", target=tps[i], exit="", profit="", status="PENDING",
                                 close_time=""))
        return dict(legs=legs, take_profits=tps, strategy_display_name="Wayond Auto Demo",
                    progress=dict(closed=closed, total=3, label=f"TP{closed}", final=(closed >= 3)))

    def _render(self, closed):
        r = build_canonical_trade_result(
            self.trade, correlation_id=self.CID, signal_source="wayond",
            linkage=resolve_signal_linkage(self.CID), leg_evidence=self._legev(closed),
        )
        return r, TelegramRenderer().render(r).text

    def test_tp1_card_shows_one_closed_two_pending(self):
        r, t = self._render(1)
        self.assertEqual(len(r.legs), 3)
        self.assertIn("TRADE RESULT — TP1 WIN", t)
        self.assertIn("1 of 3 legs closed", t)
        self.assertIn("✅ TP1", t)
        self.assertIn("2362.45 → 2367.50", t)
        self.assertIn("⏳ TP2", t)
        self.assertIn("pending", t)
        self.assertIn("Total closed: +$42.88 (1 of 3 legs)", t)

    def test_tp2_card_running_total(self):
        _, t = self._render(2)
        self.assertIn("TRADE RESULT — TP2 WIN", t)
        self.assertIn("2 of 3 legs closed", t)
        self.assertIn("Total closed: +$85.99 (2 of 3 legs)", t)

    def test_final_card_shows_all_closed_and_total(self):
        r, t = self._render(3)
        self.assertIn("TRADE RESULT — FINAL WIN", t)
        self.assertIn("all 3 of 3 legs closed", t)
        self.assertIn("✅ TP3", t)
        self.assertNotIn("pending", t)
        self.assertIn("Total net profit: +$128.64", t)
        self.assertIn("Total closed: +$128.64 (3 of 3 legs)", t)
        self.assertIn("All 3 take-profit legs were executed", t)

    def test_canonical_leg_fields_populated(self):
        r = build_canonical_trade_result(self.trade, correlation_id=self.CID, leg_evidence=self._legev(2))
        self.assertEqual([lg.status for lg in r.legs], ["CLOSED", "CLOSED", "PENDING"])
        self.assertEqual(r.take_profits, ("2367.50", "2371.50", "2375.50"))
        self.assertEqual(r.strategy_display_name, "Wayond Auto Demo")
        self.assertEqual(r.progress["closed"], 2)


class ResolveLegEvidenceTests(CanonicalBase):
    """The execution-side resolver gathers a plan's per-leg evidence (closed + open + pending)."""

    def setUp(self):
        super().setUp()
        # leg 1 is already closed (trade T1). Add leg 2 (closed) + leg 3 (no trade -> pending).
        job2 = ExecutionJob.objects.create(
            job_type=ExecutionJob.JobType.PLACE_ORDER, account=self.acct,
            status=ExecutionJob.Status.PENDING,
            payload={"execution_mode": "DEMO", "comment": f"WAY{self.plan.id}L2"},
        )
        ProposedOrderLeg.objects.create(
            plan=self.plan, leg_index=2, take_profit="1.0950", stop_loss="1.0800",
            lot_size=Decimal("0.01"), status=ProposedOrderLeg.Status.PROMOTED, execution_job=job2,
        )
        self.trade2 = Trade.objects.create(
            account=self.acct, ticket="T2", symbol="EURUSD", side="BUY", volume=Decimal("0.01"),
            open_time=timezone.now(), open_price=Decimal("1.0850"), close_time=timezone.now(),
            close_price=Decimal("1.0950"), profit=Decimal("42"), comment=f"WAY{self.plan.id}L2",
            correlation_id=self.CID,
        )
        ProposedOrderLeg.objects.create(
            plan=self.plan, leg_index=3, take_profit="1.1000", stop_loss="1.0800",
            lot_size=Decimal("0.01"), status=ProposedOrderLeg.Status.PLANNED,  # no job/trade -> pending
        )

    def test_resolver_reports_closed_and_pending(self):
        from execution.notifications.contracts import resolve_leg_evidence
        ev = resolve_leg_evidence(self.CID, self.trade2)  # triggered by leg 2's close
        self.assertEqual([lg["status"] for lg in ev["legs"]], ["CLOSED", "CLOSED", "PENDING"])
        self.assertEqual(ev["take_profits"], ["1.0900", "1.0950", "1.1000"])
        self.assertEqual(ev["progress"], {"closed": 2, "total": 3, "label": "TP2", "final": False})
        self.assertEqual(ev["legs"][1]["profit"], "42.00")
        self.assertEqual(ev["legs"][2]["target"], "1.1000")
        self.assertEqual(ev["legs"][2]["exit"], "")

    def test_resolver_blank_when_no_plan(self):
        from execution.notifications.contracts import resolve_leg_evidence
        self.assertEqual(resolve_leg_evidence("no-such-corr", None), {})
        self.assertEqual(resolve_leg_evidence("", None), {})


class LegEvidenceCollisionAndProgressiveTests(CanonicalBase):
    """A stale price-less deal row must not shadow the authoritative position row, and a card
    shows only the legs closed at/before its OWN trade's close (honest progressive state, so a
    retroactively-rendered recovered result never over-reports)."""

    def _add_closed_leg(self, idx, tp, *, closed_at, close_price, profit, ticket):
        job = ExecutionJob.objects.create(
            job_type=ExecutionJob.JobType.PLACE_ORDER, account=self.acct,
            status=ExecutionJob.Status.PENDING,
            payload={"execution_mode": "DEMO", "comment": f"WAY{self.plan.id}L{idx}"},
        )
        ProposedOrderLeg.objects.create(
            plan=self.plan, leg_index=idx, take_profit=tp, stop_loss="1.0800",
            lot_size=Decimal("0.01"), status=ProposedOrderLeg.Status.PROMOTED, execution_job=job,
        )
        return Trade.objects.create(
            account=self.acct, ticket=ticket, symbol="EURUSD", side="BUY", volume=Decimal("0.01"),
            open_time=self.trade.open_time, open_price=Decimal("1.0850"),
            close_time=closed_at, close_price=Decimal(close_price), profit=Decimal(profit),
            comment=f"WAY{self.plan.id}L{idx}", correlation_id=self.CID,
        )

    def test_authoritative_row_wins_over_stale_priceless_duplicate(self):
        from execution.notifications.contracts import resolve_leg_evidence
        # Stale deal-keyed duplicate for leg 1: same comment, close_time set but NO close_price,
        # profit 0 (exactly what the old deal-per-row worker produced).
        Trade.objects.create(
            account=self.acct, ticket="STALE1", symbol="EURUSD", side="BUY", volume=Decimal("0.01"),
            open_time=self.trade.open_time, open_price=Decimal("0"),
            close_time=self.trade.close_time, close_price=None, profit=Decimal("0"),
            comment=f"WAY{self.plan.id}L1", correlation_id="",
        )
        leg1 = resolve_leg_evidence(self.CID, self.trade)["legs"][0]
        self.assertEqual(leg1["status"], "CLOSED")
        # Picks the authoritative position row (T1, exit 1.09000, +21) — NOT the price-less 0-profit row.
        self.assertEqual(leg1["exit"], "1.09000")
        self.assertEqual(leg1["profit"], "21.00")

    def test_card_shows_only_legs_closed_by_its_own_close_time(self):
        from execution.notifications.contracts import resolve_leg_evidence
        later = self.trade.close_time + timedelta(minutes=5)
        t2 = self._add_closed_leg(2, "1.0950", closed_at=later, close_price="1.0950",
                                  profit="42", ticket="P2")
        # A card triggered by leg 1 (closed earlier) must NOT yet count leg 2 as closed.
        ev1 = resolve_leg_evidence(self.CID, self.trade)
        self.assertEqual([lg["status"] for lg in ev1["legs"]], ["CLOSED", "OPEN"])
        self.assertEqual(ev1["progress"]["closed"], 1)
        self.assertFalse(ev1["progress"]["final"])
        # The later card (triggered by leg 2) DOES show both closed (final).
        ev2 = resolve_leg_evidence(self.CID, t2)
        self.assertEqual([lg["status"] for lg in ev2["legs"]], ["CLOSED", "CLOSED"])
        self.assertEqual(ev2["progress"]["closed"], 2)
        self.assertTrue(ev2["progress"]["final"])


class StakeholderCardTests(TestCase):
    """GFX-PKT-STAKEHOLDER-OUTPUT-VISUAL-UPGRADE — the redesigned result card + short caption."""

    def _canonical(self, closed=3):
        acct = types.SimpleNamespace(is_demo=True, name="TradersWay Demo")
        trade = {"ticket": "S", "symbol": "XAUUSD", "side": "BUY", "volume": 0.02,
                 "open_time": timezone.now() - timedelta(hours=2),
                 "close_time": timezone.now() - timedelta(minutes=18),
                 "open_price": 2362.45, "close_price": 2375.50, "profit": 42.65,
                 "commission": 0.0, "swap": 0.0, "account": acct}
        tps = ["2367.50", "2371.50", "2375.50"]
        profs = ["42.88", "43.11", "42.65"]
        legs = [dict(index=i + 1, tp_label=f"TP{i + 1}", direction="BUY", volume="0.02",
                     entry="2362.45", target=tps[i], exit=(tps[i] if i < closed else ""),
                     profit=(profs[i] if i < closed else ""),
                     status=("CLOSED" if i < closed else "PENDING"),
                     close_time=(timezone.now().isoformat() if i < closed else "")) for i in range(3)]
        ev = dict(legs=legs, take_profits=tps, strategy_display_name="Wayond Auto Demo",
                  progress=dict(closed=closed, total=3, label=f"TP{closed}", final=(closed >= 3)))
        link = dict(reference_entry="2362.45", stop_loss="2358.00", take_profit="2367.50",
                    source="wayond", provider="wayond", execution_mode="DEMO", signal_id="m")
        return build_canonical_trade_result(trade, correlation_id="corr-abc-123",
                                            signal_source="wayond", linkage=link, leg_evidence=ev)

    def _svg(self, closed=3):
        from intelligence.results_card import render_result_card
        return render_result_card(self._canonical(closed))["svg"]

    def test_1_and_2_tp_labels_replace_leg(self):
        svg = self._svg(3)
        for tp in ("TP1", "TP2", "TP3"):
            self.assertIn(tp, svg)
        self.assertNotIn("Leg", svg)

    def test_3_each_tp_row_shows_pips(self):
        svg = self._svg(3)
        self.assertIn("pips", svg)
        self.assertIn("+50.5 pips", svg)   # XAUUSD TP1: (2367.50-2362.45)/0.1
        self.assertIn("+130.5 pips", svg)  # TP3

    def test_4_trade_analysis_replaces_execution_analysis(self):
        svg = self._svg(3)
        self.assertIn("TRADE ANALYSIS", svg)
        self.assertNotIn("EXECUTION ANALYSIS", svg)

    def test_5_and_6_execution_mode_and_correlation_id_hidden(self):
        svg = self._svg(3)
        self.assertNotIn("DEMO", svg)          # execution mode hidden
        self.assertNotIn("corr-abc-123", svg)  # correlation id hidden
        self.assertNotIn("Correlation", svg)
        self.assertNotIn("Execution mode", svg)

    def test_7_pending_tps_not_implied_as_wins(self):
        svg = self._svg(1)  # only TP1 closed
        self.assertIn("PENDING", svg)
        self.assertIn("PROFIT SO FAR", svg)   # not TOTAL PROFIT
        self.assertIn("1 of 3 take-profits closed", svg)
        self.assertIn("target 2371.50", svg)  # TP2 shown as a target, not a realised win

    def test_8_final_card_shows_all_closed(self):
        svg = self._svg(3)
        self.assertNotIn("PENDING", svg)
        self.assertIn("TOTAL PROFIT", svg)
        self.assertIn("3 of 3 take-profits closed", svg)

    def test_card_png_renders(self):
        card = __import__("intelligence.results_card", fromlist=["render_result_card"]).render_result_card(
            self._canonical(3))
        self.assertTrue(base64.b64decode(card["png_base64"]).startswith(b"\x89PNG"))

    def test_9_short_caption_is_stakeholder_friendly(self):
        from intelligence.caption import build_short_caption
        cap = build_short_caption(self._canonical(3))
        self.assertIn("closed a winning XAUUSD BUY trade", cap)
        self.assertIn("Total Profit: +$128.64", cap)
        self.assertIn("Result: WIN", cap)
        self.assertIn("Full trade card attached", cap)
        self.assertLessEqual(len(cap.splitlines()), 8)
        self.assertNotIn("DEMO", cap)
        self.assertNotIn("corr", cap.lower())

    def test_10_internal_audit_fields_remain_on_canonical(self):
        r = self._canonical(3)
        self.assertEqual(r.correlation_id, "corr-abc-123")
        self.assertEqual(r.execution_mode, "DEMO")
        self.assertEqual([lg.pips for lg in r.legs], ["50.5", "90.5", "130.5"])


class SendPhotoTransportTests(SimpleTestCase):
    """The image primitive (sendPhoto) posts multipart; deliver() sends the card (text fallback)."""

    def test_send_photo_posts_multipart_to_sendphoto(self):
        from execution.notifications import real_transport as rt
        captured = {}

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": true, "result": {"message_id": 7}}'

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["ct"] = req.headers.get("Content-type")
            captured["body"] = req.data
            return _Resp()

        t = rt.RealTelegramTransport(token="SECRET-TOK", chat_id="@wims07072026")
        with mock.patch.object(rt.urllib.request, "urlopen", fake_urlopen):
            resp = t._send_photo(b"\x89PNG\r\nFAKE", caption="Full trade card attached.")

        self.assertTrue(captured["url"].endswith("/sendPhoto"))
        self.assertIn("multipart/form-data", captured["ct"])
        self.assertIn(b"\x89PNG\r\nFAKE", captured["body"])
        self.assertIn(b'name="caption"', captured["body"])
        self.assertIn(b'name="photo"', captured["body"])
        self.assertEqual(resp["result"]["message_id"], 7)
        self.assertNotIn("SECRET-TOK", str(resp))  # token never surfaces in the result

    def test_11_win_notifications_use_the_card(self):
        # The candidate/dispatch path now sends the visual CARD (sendPhoto), with a text fallback.
        import inspect

        from execution.notifications.real_transport import RealTelegramTransport
        src = inspect.getsource(RealTelegramTransport.deliver)
        self.assertIn("_send_photo", src)              # card image is the primary output
        self.assertIn("build_stakeholder_card", src)
        self.assertIn("_send(text)", src)              # text fallback retained


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
