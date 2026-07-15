"""WS-G — soak-report evidence snapshot: source-aware, read-only, durably persistable."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from reliability.models import SoakSnapshot
from reliability.services.soak_report import build_soak_snapshot
from execution.models import (
    SignalSourceConfig, SignalExecutionPlan, TradeOutcomeRecord,
)
from signal_intake.models import PendingSignalApproval
from trading.models import Trade, TradingAccount

User = get_user_model()


class SoakReportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="sk", email="sk@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="SK1", is_demo=True)
        SignalSourceConfig.objects.create(source="ti_signals", max_lot_per_leg=Decimal("0.40"))
        # one winning ti_signals trade
        appr = PendingSignalApproval.objects.create(
            source="ti_signals", message_id="m1", symbol="XAUUSD", direction="BUY",
            stop_loss="4000", take_profits=["4010"], status=PendingSignalApproval.Status.APPROVED)
        SignalExecutionPlan.objects.create(
            approval=appr, account=self.acct, source="ti_signals", message_id="m1", symbol="XAUUSD",
            direction="BUY", is_demo=True, signal_timestamp=timezone.now(),
            status=SignalExecutionPlan.Status.CLOSED)
        trade = Trade.objects.create(
            account=self.acct, symbol="XAUUSD", side="BUY", volume=Decimal("0.40"),
            ticket="t1", open_time=timezone.now(), open_price=Decimal("4005"),
            close_time=timezone.now(), close_price=Decimal("4010"), comment="WAY1L1")
        TradeOutcomeRecord.objects.create(
            trade=trade, outcome="WIN", net_pnl=Decimal("20"), is_delivery_candidate=True,
            signal_source="ti_signals")

    def test_snapshot_is_source_aware(self):
        snap = build_soak_snapshot(window_hours=24, persist=False)
        self.assertIn("by_source", snap)
        ti = next(r for r in snap["by_source"] if r["source"] == "ti_signals")
        self.assertEqual(ti["signals_received"], 1)
        self.assertEqual(ti["plans_promoted"], 1)   # CLOSED counts as promoted-through
        self.assertEqual(ti["wins"], 1)
        self.assertEqual(Decimal(ti["realised_pnl"]), Decimal("20"))
        # each source has its own row — never one combined total
        for r in snap["by_source"]:
            for f in ("wins", "losses", "realised_pnl", "breakeven_modifications", "provider_commands"):
                self.assertIn(f, r)

    def test_persist_writes_durable_row(self):
        before = SoakSnapshot.objects.count()
        build_soak_snapshot(window_hours=1, persist=True)
        self.assertEqual(SoakSnapshot.objects.count(), before + 1)
        row = SoakSnapshot.objects.latest("generated_at")
        self.assertEqual(row.window_hours, 1)
        self.assertIn("by_source", row.data)

    def test_no_persist_is_read_only(self):
        before = SoakSnapshot.objects.count()
        build_soak_snapshot(window_hours=24, persist=False)
        self.assertEqual(SoakSnapshot.objects.count(), before)  # nothing written
