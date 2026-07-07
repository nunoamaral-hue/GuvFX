"""TELEGRAM-TRANSPORT-FOUNDATION — transport, dispatcher, lifecycle, idempotency, boundary.

Proves: feature-flag gating (default OFF → no-op); PENDING → PROCESSING → SENT/FAILED atomic
lifecycle; dry-run rendering (nothing transmitted); idempotency (SENT/SUPPRESSED ignored, never
delivered twice); correlation preservation; retry of FAILED; suppressed ignored; and the AST
boundary — no HTTP/Telegram/network import, no execution business logic, no Trade/outcome mutation.
"""
import ast
import os
import pathlib
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from execution.models import (
    NotificationCandidate,
    NotificationDelivery,
    TradeOutcomeRecord,
)
from execution.notifications import contracts, dispatcher, transport
from execution.notifications.transport import (
    DeliveryResult,
    NotificationTransport,
    TelegramDryRunTransport,
)
from trading.models import Trade, TradingAccount

User = get_user_model()
Status = NotificationCandidate.Status
ENABLE = {"NOTIFICATION_DISPATCH_ENABLED": "true"}


class _RaisingTransport(NotificationTransport):
    name = "test-raise"

    def deliver(self, candidate):
        raise RuntimeError("simulated transport error")


class _FailTransport(NotificationTransport):
    name = "test-fail"

    def deliver(self, candidate):
        return DeliveryResult(ok=False, status="FAILED", transmitted=False,
                              rendered_message="", detail="simulated failure")


class TransportBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.account = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True,
        )
        self._n = 0

    def _candidate(self, *, net_pnl="10", cid="corr-1", source="wayond", status=Status.PENDING):
        self._n += 1
        trade = Trade.objects.create(
            account=self.account, ticket=f"t{self._n}", symbol="EURUSD", side="BUY",
            volume=Decimal("0.01"), open_time=timezone.now(), open_price=Decimal("1.0850"),
            close_time=timezone.now(), close_price=Decimal("1.0900"), profit=Decimal(net_pnl),
        )
        rec = TradeOutcomeRecord.objects.create(
            trade=trade, outcome=TradeOutcomeRecord.Outcome.WIN, net_pnl=Decimal(net_pnl),
            is_delivery_candidate=True, correlation_id=cid, signal_source=source, routed=True,
        )
        return NotificationCandidate.objects.create(
            outcome_record=rec, correlation_id=cid, signal_source=source,
            net_pnl=Decimal(net_pnl), status=status,
        )


class FeatureFlagTests(TransportBase):
    def test_disabled_by_default_is_noop(self):
        cand = self._candidate()
        counts = dispatcher.dispatch_pending()  # flag OFF by default
        self.assertFalse(counts["enabled"])
        self.assertEqual(counts["claimed"], 0)
        cand.refresh_from_db()
        self.assertEqual(cand.status, Status.PENDING)
        self.assertEqual(NotificationDelivery.objects.count(), 0)


@mock.patch.dict(os.environ, ENABLE)
class LifecycleTests(TransportBase):
    def test_win_candidate_dry_run_sent(self):
        cand = self._candidate(cid="c-9")
        counts = dispatcher.dispatch_pending()
        cand.refresh_from_db()
        self.assertEqual(cand.status, Status.SENT)
        self.assertEqual(counts["sent"], 1)
        d = NotificationDelivery.objects.get()
        self.assertEqual(d.result, NotificationDelivery.Result.SENT)
        self.assertFalse(d.transmitted)                 # nothing transmitted
        self.assertEqual(d.correlation_id, "c-9")       # correlation preserved
        self.assertTrue(d.rendered_message)             # rendered
        self.assertEqual(d.attempt, 1)

    def test_suppressed_is_ignored(self):
        cand = self._candidate(status=Status.SUPPRESSED)
        dispatcher.dispatch_pending()
        cand.refresh_from_db()
        self.assertEqual(cand.status, Status.SUPPRESSED)
        self.assertEqual(NotificationDelivery.objects.count(), 0)

    def test_idempotent_sent_not_redispatched(self):
        self._candidate()
        dispatcher.dispatch_pending()
        self.assertEqual(NotificationDelivery.objects.count(), 1)
        dispatcher.dispatch_pending()  # re-run — SENT is ignored
        dispatcher.dispatch_pending()
        self.assertEqual(NotificationDelivery.objects.count(), 1)

    def test_transport_error_marks_failed(self):
        cand = self._candidate()
        dispatcher.dispatch_pending(transport=_RaisingTransport())
        cand.refresh_from_db()
        self.assertEqual(cand.status, Status.FAILED)
        d = NotificationDelivery.objects.get()
        self.assertEqual(d.result, NotificationDelivery.Result.FAILED)
        self.assertEqual(d.detail, "transport raised")

    def test_failed_result_marks_failed(self):
        cand = self._candidate()
        dispatcher.dispatch_pending(transport=_FailTransport())
        cand.refresh_from_db()
        self.assertEqual(cand.status, Status.FAILED)

    def test_retry_failed_then_sent(self):
        cand = self._candidate()
        dispatcher.dispatch_pending(transport=_RaisingTransport())  # attempt 1 → FAILED
        cand.refresh_from_db()
        self.assertEqual(cand.status, Status.FAILED)
        dispatcher.dispatch_pending()  # retry with dry-run → SENT (attempt 2)
        cand.refresh_from_db()
        self.assertEqual(cand.status, Status.SENT)
        attempts = list(cand.deliveries.order_by("attempt").values_list("attempt", "result"))
        self.assertEqual(attempts, [
            (1, NotificationDelivery.Result.FAILED),
            (2, NotificationDelivery.Result.SENT),
        ])

    def test_orphaned_processing_is_reaped_then_retried(self):
        # A candidate stuck in PROCESSING (crash/DB-error orphan) beyond the timeout is
        # reclaimed to FAILED by the reaper, then delivered on the same run.
        cand = self._candidate()
        old = timezone.now() - timezone.timedelta(seconds=10_000)
        NotificationCandidate.objects.filter(id=cand.id).update(
            status=Status.PROCESSING, updated_at=old,
        )
        dispatcher.dispatch_pending()
        cand.refresh_from_db()
        self.assertEqual(cand.status, Status.SENT)  # reaped → FAILED → retried → SENT
        self.assertTrue(NotificationDelivery.objects.filter(
            candidate=cand, result=NotificationDelivery.Result.SENT).exists())

    def test_recent_processing_not_reaped(self):
        # A freshly-claimed PROCESSING row (within the timeout) is NOT reclaimed.
        cand = self._candidate()
        NotificationCandidate.objects.filter(id=cand.id).update(
            status=Status.PROCESSING, updated_at=timezone.now(),
        )
        counts = dispatcher.dispatch_pending()
        self.assertEqual(counts["reaped"], 0)
        cand.refresh_from_db()
        self.assertEqual(cand.status, Status.PROCESSING)  # left in flight

    def test_does_not_modify_trade_or_outcome(self):
        cand = self._candidate(net_pnl="15")
        trade_id, rec_id = cand.outcome_record.trade_id, cand.outcome_record_id
        before_trades = Trade.objects.count()
        before_recs = TradeOutcomeRecord.objects.count()
        dispatcher.dispatch_pending()
        self.assertEqual(Trade.objects.count(), before_trades)
        self.assertEqual(TradeOutcomeRecord.objects.count(), before_recs)
        rec = TradeOutcomeRecord.objects.get(id=rec_id)
        self.assertEqual(rec.net_pnl, Decimal("15"))     # unchanged
        self.assertEqual(rec.outcome, TradeOutcomeRecord.Outcome.WIN)
        self.assertEqual(Trade.objects.get(id=trade_id).close_price, Decimal("1.0900"))


class RenderingTests(TransportBase):
    def test_dry_run_render_envelope_fields(self):
        cand = self._candidate(cid="c-render", source="wayond", net_pnl="21")
        envelope = TelegramDryRunTransport().render(cand)
        self.assertEqual(envelope.symbol, "EURUSD")
        self.assertEqual(envelope.direction, "BUY")
        self.assertEqual(Decimal(envelope.actual_fill), Decimal("1.0850"))  # the real fill
        self.assertEqual(Decimal(envelope.profit), Decimal("21"))
        self.assertEqual(envelope.correlation_id, "c-render")   # kept on the envelope field (internal)
        self.assertEqual(envelope.strategy, "wayond")
        self.assertIn("EURUSD", envelope.rendered_message)
        # requirement 6: the correlation id is HIDDEN from the stakeholder message text.
        self.assertNotIn("c-render", envelope.rendered_message)
        # The contract has every required field.
        for field in ("title", "summary", "strategy", "symbol", "direction", "reference_entry",
                      "actual_fill", "stop_loss", "take_profit", "profit", "pips",
                      "execution_timestamp", "correlation_id", "rendered_message"):
            self.assertIn(field, envelope.as_dict())

    def test_deliver_never_transmits(self):
        cand = self._candidate()
        result = TelegramDryRunTransport().deliver(cand)
        self.assertTrue(result.ok)
        self.assertFalse(result.transmitted)


@mock.patch.dict(os.environ, ENABLE)
class CommandTests(TransportBase):
    def test_command_dispatches(self):
        cand = self._candidate()
        call_command("dispatch_notifications")
        cand.refresh_from_db()
        self.assertEqual(cand.status, Status.SENT)


class BoundaryTests(TransportBase):
    PACKAGE_DIR = pathlib.Path(dispatcher.__file__).parent

    def _imports(self, path):
        tree = ast.parse(pathlib.Path(path).read_text())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    mods.add(a.name)
        return mods

    #: The SINGLE network-capable file (GFX-PKT-REAL-TELEGRAM-TRANSPORT). Every other file in the
    #: package stays network-free; the real transport is disabled by default (dispatch flag OFF +
    #: dry-run default). Isolating the send surface to one audited file keeps the guard meaningful.
    _NETWORK_ALLOWED = {"real_transport.py"}

    def test_no_network_or_telegram_imports_anywhere_except_real_transport(self):
        forbidden = ("requests", "urllib", "http", "socket", "telegram", "telethon", "httpx")
        for pyfile in self.PACKAGE_DIR.glob("*.py"):
            if pyfile.name in self._NETWORK_ALLOWED:
                continue
            mods = self._imports(pyfile)
            tops = {m.split(".")[0] for m in mods}
            for f in forbidden:
                self.assertNotIn(f, tops, f"{pyfile.name} must not import {f!r}")

    def test_only_real_transport_is_network_capable(self):
        # Positive: the real transport DOES use urllib (it is the send surface); nothing else does.
        real = self._imports(self.PACKAGE_DIR / "real_transport.py")
        self.assertIn("urllib.request", real)
        for pyfile in self.PACKAGE_DIR.glob("*.py"):
            if pyfile.name in self._NETWORK_ALLOWED:
                continue
            tops = {m.split(".")[0] for m in self._imports(pyfile)}
            self.assertNotIn("urllib", tops, f"{pyfile.name} unexpectedly imports urllib")

    def test_real_transport_makes_no_order_or_wims_reference(self):
        src = pathlib.Path((self.PACKAGE_DIR / "real_transport.py")).read_text()
        names = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        for token in ("order_send", "order_check", "agent_order", "ExecutionJob",
                      "create_contract", "deliver_trade_result", "ConsumptionContract", "wims"):
            self.assertNotIn(token, names, f"real_transport must not reference {token!r}")

    def test_transport_and_dispatcher_import_no_execution_logic(self):
        # The transport/dispatcher must not pull in execution business logic (orders/planning).
        forbidden = ("signal_planning", "signal_promotion", "auto_router", "close_monitor",
                     "outcome_router", "signal_proposals")
        for name in ("transport.py", "dispatcher.py"):
            mods = self._imports(self.PACKAGE_DIR / name)
            for f in forbidden:
                self.assertFalse(any(f in m for m in mods), f"{name} must not import {f!r}")

    def test_dispatcher_source_has_no_order_reference(self):
        src = pathlib.Path(dispatcher.__file__).read_text()
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        for token in ("order_send", "order_check", "agent_order", "ExecutionJob",
                      "promote_plan_to_shadow_jobs", "sendMessage"):
            self.assertNotIn(token, names)
