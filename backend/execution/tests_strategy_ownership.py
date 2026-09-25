"""Phase A — durable strategy ownership + MT5 magic numbers: adversarial tests (§16).

Covers: deterministic collision-free magic allocation; the (account, source) owner
resolution; the §9 Trade attribution matrix (strong/legacy/conflict/unattributed,
account-verified); guarded owner-scoping (§10) fail-open/enforce; hedging-safe
BUY+SELL distinction; and — critically — that with the DARK flags OFF the promotion
path is byte-identical (no plan/job ownership, no payload magic).
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from execution import ownership, ownership_flags, ownership_stamp
from execution.models import (
    ExecutionControl,
    ExecutionJob,
    SignalExecutionPlan,
    SignalSourceConfig,
)
from execution.signal_planning import plan_demo_execution
from execution.signal_promotion import (
    PromotionRejected, _resolve_plan_assignment, promote_plan_to_demo_jobs)
from signal_intake.models import PendingSignalApproval
from strategies.magic_allocation import (
    ASSIGNMENT_MAGIC_BASE,
    MagicAllocationError,
    allocate_magic,
    magic_for,
)
from strategies.models import Strategy, StrategyAssignment
from trading.models import Trade, TradingAccount

User = get_user_model()
Mode = ExecutionControl.SignalExecutionMode
JT = ExecutionJob.JobType
DUAL = "STRATEGY_OWNERSHIP_DUAL_WRITE_ENABLED"
MAGIC = "STRATEGY_MAGIC_SEND_ENABLED"
ENFORCE = "STRATEGY_OWNERSHIP_ENFORCE_ENABLED"


class OwnershipBase(TestCase):
    def setUp(self):
        self.op = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.acct_a = TradingAccount.objects.create(
            user=self.op, name="A", account_number="A1", is_demo=True, broker_name="DemoBroker")
        self.acct_b = TradingAccount.objects.create(
            user=self.op, name="B", account_number="B1", is_demo=True, broker_name="DemoBroker")
        SignalSourceConfig.objects.create(
            source="ti_signals", auto_demo_execution_enabled=True, total_lot_target=Decimal("0.03"))
        self.strat = Strategy.objects.create(owner=self.op, name="T1")
        self.strat2 = Strategy.objects.create(owner=self.op, name="T2")

    def _asn(self, strategy, account, *, source="ti_signals", mode=None):
        return StrategyAssignment.objects.create(
            strategy=strategy, account=account, is_active=True,
            stage=StrategyAssignment.STAGE_LIVE, signal_source=source,
            execution_mode=mode or StrategyAssignment.ExecutionMode.AUTO_DEMO)

    def _mode_demo(self):
        c = ExecutionControl.get_solo()
        c.signal_execution_mode = Mode.DEMO
        c.auto_execution_enabled = True
        c.kill_switch_engaged = False
        c.save()

    def _plan(self, account, assignment, *, mid, symbol="EURUSD", direction="BUY"):
        approval = PendingSignalApproval.objects.create(
            source="ti_signals", message_id=mid, symbol=symbol, direction=direction,
            entry="1.0850", stop_loss="1.0800", take_profit="1.0900",
            take_profits=["1.0900"], status=PendingSignalApproval.Status.APPROVED)
        return plan_demo_execution(
            approval, account=account, actor=self.op,
            signal_timestamp=timezone.now(), assignment=assignment)


# --- Magic allocation ---------------------------------------------------------
class MagicAllocationTests(OwnershipBase):
    def test_two_assignments_same_account_get_distinct_magics(self):
        a = self._asn(self.strat, self.acct_a)
        b = self._asn(self.strat2, self.acct_a)
        self.assertNotEqual(magic_for(a.id), magic_for(b.id))
        self.assertEqual(magic_for(a.id), ASSIGNMENT_MAGIC_BASE + a.id)

    def test_same_strategy_two_accounts_distinct_magics(self):
        a = self._asn(self.strat, self.acct_a)
        b = self._asn(self.strat, self.acct_b)
        self.assertNotEqual(magic_for(a.id), magic_for(b.id))

    def test_deleted_assignment_magic_never_reused(self):
        a = self._asn(self.strat, self.acct_a)
        allocate_magic(a)
        old = a.magic_number
        a.delete()
        c = self._asn(self.strat, self.acct_a)  # new row → new PK
        allocate_magic(c)
        self.assertNotEqual(c.magic_number, old)

    def test_registry_unique_blocks_duplicate_magic(self):
        a = self._asn(self.strat, self.acct_a)
        allocate_magic(a)
        b = self._asn(self.strat2, self.acct_a)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                b.magic_number = a.magic_number  # collide the registry
                b.save(update_fields=["magic_number"])

    def test_out_of_band_magic_rejected_by_check_constraint(self):
        a = self._asn(self.strat, self.acct_a)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                a.magic_number = 6  # below the 1e9 band (legacy strategy-id style)
                a.save(update_fields=["magic_number"])

    def test_allocate_is_idempotent(self):
        a = self._asn(self.strat, self.acct_a)
        first = allocate_magic(a)
        second = allocate_magic(a)
        self.assertEqual(first, second)

    def test_magic_for_rejects_out_of_int32(self):
        with self.assertRaises(MagicAllocationError):
            magic_for(2_000_000_000)  # 1e9 + 2e9 > int32 max


# --- Owner resolution ---------------------------------------------------------
class OwnerResolutionTests(OwnershipBase):
    def test_resolve_prefers_dual_written_assignment(self):
        a = self._asn(self.strat, self.acct_a)
        with override_settings(**{DUAL: True}):
            plan = self._plan(self.acct_a, a, mid="r1")
        self.assertEqual(plan.strategy_assignment_id, a.id)
        self.assertEqual(_resolve_plan_assignment(plan).id, a.id)

    def test_resolve_falls_back_to_account_source_when_unique(self):
        a = self._asn(self.strat, self.acct_a)
        plan = SignalExecutionPlan.objects.create(
            approval=PendingSignalApproval.objects.create(
                source="ti_signals", message_id="r2", symbol="EURUSD", direction="BUY",
                stop_loss="1.08", take_profits=["1.09"],
                status=PendingSignalApproval.Status.APPROVED),
            account=self.acct_a, source="ti_signals", message_id="r2", symbol="EURUSD",
            direction="BUY", stop_loss="1.08", is_demo=True, signal_timestamp=timezone.now(),
            status=SignalExecutionPlan.Status.PLANNED)
        self.assertEqual(_resolve_plan_assignment(plan).id, a.id)

    def test_resolve_none_when_wrong_account(self):
        a = self._asn(self.strat, self.acct_b)  # bound to acct_b
        plan = SignalExecutionPlan.objects.create(
            approval=PendingSignalApproval.objects.create(
                source="ti_signals", message_id="r3", symbol="EURUSD", direction="BUY",
                stop_loss="1.08", take_profits=["1.09"],
                status=PendingSignalApproval.Status.APPROVED),
            account=self.acct_a, source="ti_signals", message_id="r3", symbol="EURUSD",
            direction="BUY", stop_loss="1.08", is_demo=True, signal_timestamp=timezone.now(),
            status=SignalExecutionPlan.Status.PLANNED)
        # a is on acct_b, plan is acct_a → no match for acct_a
        self.assertIsNone(_resolve_plan_assignment(plan))


# --- §9 Trade attribution matrix ---------------------------------------------
class TradeAttributionTests(OwnershipBase):
    def _trade(self, account, *, magic=0, comment=""):
        return Trade.objects.create(
            account=account, ticket="tk", symbol="EURUSD", side="BUY",
            volume=Decimal("0.01"), open_time=timezone.now(), open_price=Decimal("1.085"),
            magic_number=magic, comment=comment)

    def _plan_with_owner(self, account, assignment, mid):
        with override_settings(**{DUAL: True}):
            return self._plan(account, assignment, mid=mid)

    def test_magic_and_comment_agree_is_strong(self):
        a = self._asn(self.strat, self.acct_a)
        allocate_magic(a)
        plan = self._plan_with_owner(self.acct_a, a, "s1")
        t = self._trade(self.acct_a, magic=a.magic_number, comment=f"WAY{plan.id}L1")
        owner, code = ownership_stamp.resolve_owner_for_trade(t)
        self.assertEqual(code, ownership_stamp.STRONG)
        self.assertEqual(owner.id, a.id)

    def test_magic_and_comment_conflict_is_quarantined(self):
        a = self._asn(self.strat, self.acct_a)
        b = self._asn(self.strat2, self.acct_a)
        allocate_magic(a)
        allocate_magic(b)
        plan_b = self._plan_with_owner(self.acct_a, b, "s2")  # comment → b
        t = self._trade(self.acct_a, magic=a.magic_number, comment=f"WAY{plan_b.id}L1")  # magic → a
        owner, code = ownership_stamp.resolve_owner_for_trade(t)
        self.assertEqual(code, ownership_stamp.CONFLICT)
        self.assertIsNone(owner)

    def test_magic_zero_uses_legacy_comment(self):
        a = self._asn(self.strat, self.acct_a)
        allocate_magic(a)
        plan = self._plan_with_owner(self.acct_a, a, "s3")
        t = self._trade(self.acct_a, magic=0, comment=f"WAY{plan.id}L1")
        owner, code = ownership_stamp.resolve_owner_for_trade(t)
        self.assertEqual(code, ownership_stamp.LEGACY)
        self.assertEqual(owner.id, a.id)

    def test_unknown_magic_and_no_comment_is_unattributed(self):
        self._asn(self.strat, self.acct_a)
        t = self._trade(self.acct_a, magic=0, comment="")
        owner, code = ownership_stamp.resolve_owner_for_trade(t)
        self.assertEqual(code, ownership_stamp.UNATTRIBUTED)
        self.assertIsNone(owner)

    def test_magic_of_other_account_not_matched(self):
        a = self._asn(self.strat, self.acct_a)
        allocate_magic(a)
        t = self._trade(self.acct_b, magic=a.magic_number, comment="")  # magic belongs to acct_a
        owner, code = ownership_stamp.resolve_owner_for_trade(t)
        self.assertEqual(code, ownership_stamp.UNATTRIBUTED)  # account-verified → not matched

    def test_stamp_is_noop_when_dual_write_off(self):
        a = self._asn(self.strat, self.acct_a)
        allocate_magic(a)
        plan = self._plan_with_owner(self.acct_a, a, "s4")
        t = self._trade(self.acct_a, magic=a.magic_number, comment=f"WAY{plan.id}L1")
        self.assertEqual(ownership_stamp.stamp_trade_ownership(t), ownership_stamp.NOOP)
        t.refresh_from_db()
        self.assertIsNone(t.strategy_assignment_id)

    def test_stamp_persists_under_dual_write(self):
        a = self._asn(self.strat, self.acct_a)
        allocate_magic(a)
        plan = self._plan_with_owner(self.acct_a, a, "s5")
        t = self._trade(self.acct_a, magic=a.magic_number, comment=f"WAY{plan.id}L1")
        with override_settings(**{DUAL: True}):
            code = ownership_stamp.stamp_trade_ownership(t)
        self.assertEqual(code, ownership_stamp.STRONG)
        t.refresh_from_db()
        self.assertEqual(t.strategy_assignment_id, a.id)

    def test_ownership_survives_assignment_deactivation(self):
        # Durable historical lineage: once a Trade is attributed, DEACTIVATING the assignment
        # (is_active=False — not a delete) must never null or change the Trade's ownership FK.
        a = self._asn(self.strat, self.acct_a)
        allocate_magic(a)
        plan = self._plan_with_owner(self.acct_a, a, "deact1")
        t = self._trade(self.acct_a, magic=a.magic_number, comment=f"WAY{plan.id}L1")
        with override_settings(**{DUAL: True}):
            self.assertEqual(ownership_stamp.stamp_trade_ownership(t), ownership_stamp.STRONG)
        t.refresh_from_db()
        self.assertEqual(t.strategy_assignment_id, a.id)
        a.is_active = False
        a.save(update_fields=["is_active"])
        t.refresh_from_db()
        self.assertEqual(t.strategy_assignment_id, a.id)          # FK is independent of is_active
        self.assertFalse(t.strategy_assignment.is_active)         # and still resolves to the (now-inactive) owner


# --- §10 guarded owner-scoping -----------------------------------------------
class OwnerScopingTests(OwnershipBase):
    def _owned_trade(self, account, assignment):
        return Trade.objects.create(
            account=account, ticket="tk", symbol="EURUSD", side="BUY", volume=Decimal("0.01"),
            open_time=timezone.now(), open_price=Decimal("1.085"), strategy_assignment=assignment)

    def test_enforce_off_never_blocks(self):
        a = self._asn(self.strat, self.acct_a)
        b = self._asn(self.strat2, self.acct_a)
        t = self._owned_trade(self.acct_a, a)
        self.assertIsNone(ownership.owner_block_reason(t, b))  # DARK — fail-open

    def test_enforce_on_blocks_cross_strategy(self):
        a = self._asn(self.strat, self.acct_a)
        b = self._asn(self.strat2, self.acct_a)
        t = self._owned_trade(self.acct_a, a)
        with override_settings(**{ENFORCE: True}):
            self.assertEqual(ownership.owner_block_reason(t, b), ownership.CROSS_STRATEGY)
            self.assertIsNone(ownership.owner_block_reason(t, a))  # same owner allowed

    def test_enforce_on_legacy_null_owner_fails_open(self):
        a = self._asn(self.strat, self.acct_a)
        legacy = Trade.objects.create(
            account=self.acct_a, ticket="lg", symbol="EURUSD", side="BUY", volume=Decimal("0.01"),
            open_time=timezone.now(), open_price=Decimal("1.085"))  # no owner (legacy)
        with override_settings(**{ENFORCE: True}):
            self.assertIsNone(ownership.owner_block_reason(legacy, a))  # manageable during migration

    def test_buy_and_sell_same_symbol_distinct_owners(self):
        a = self._asn(self.strat, self.acct_a)
        b = self._asn(self.strat2, self.acct_a)
        buy = self._owned_trade(self.acct_a, a)
        sell = Trade.objects.create(
            account=self.acct_a, ticket="tk2", symbol="EURUSD", side="SELL", volume=Decimal("0.01"),
            open_time=timezone.now(), open_price=Decimal("1.085"), strategy_assignment=b)
        with override_settings(**{ENFORCE: True}):
            # each strategy may act on its own, never the other's (hedging-safe)
            self.assertIsNone(ownership.owner_block_reason(buy, a))
            self.assertIsNone(ownership.owner_block_reason(sell, b))
            self.assertEqual(ownership.owner_block_reason(buy, b), ownership.CROSS_STRATEGY)
            self.assertEqual(ownership.owner_block_reason(sell, a), ownership.CROSS_STRATEGY)


# --- Promotion path: flags OFF byte-identical; ON writes ownership ------------
class PromotionDualWriteTests(OwnershipBase):
    def _netting_ws(self, account):
        from hosted_workspace.models import HostedMt5Workspace
        from hosted_workspace.margin_mode import RETAIL_NETTING
        return HostedMt5Workspace.objects.create(
            trading_account=account, proj_margin_mode=RETAIL_NETTING, last_decision_at=timezone.now())

    def test_symbol_conflict_guard_blocks_promotion_when_armed(self):
        # B1: on a netting account, a DIFFERENT assignment already owns the symbol -> the armed guard
        # refuses promotion through the real _validate path (proves the hook wiring + owner resolution).
        a = self._asn(self.strat, self.acct_a)
        b = self._asn(self.strat2, self.acct_a)
        self._netting_ws(self.acct_a)
        Trade.objects.create(account=self.acct_a, ticket="fx", symbol="EURUSD", side="BUY",
                             volume=Decimal("0.01"), open_time=timezone.now(),
                             open_price=Decimal("1.0"), strategy_assignment=b)  # B owns EURUSD
        self._mode_demo()
        with override_settings(**{DUAL: True}):
            plan = self._plan(self.acct_a, a, mid="cf1")  # FK = A (EURUSD)
        self.assertEqual(plan.strategy_assignment_id, a.id)
        with override_settings(**{DUAL: True, "STRATEGY_SYMBOL_CONFLICT_POLICY_ENABLED": True}):
            with self.assertRaises(PromotionRejected) as cm:
                promote_plan_to_demo_jobs(plan, actor=self.op)
        self.assertEqual(cm.exception.code, "multi_strategy_netting_symbol_conflict")

    def test_null_fk_single_strategy_not_self_blocked_when_armed(self):
        # B1 regression (review HIGH): a PLANNED plan whose OWN strategy_assignment FK is NULL but whose
        # owner resolves via the (account, source) fallback must NOT count itself as a foreign owner. With a
        # single active assignment and no foreign exposure, the armed guard must ALLOW promotion.
        a = self._asn(self.strat, self.acct_a)  # the only active (acct_a, ti_signals) assignment
        self._netting_ws(self.acct_a)
        self._mode_demo()
        plan = self._plan(self.acct_a, a, mid="nf1")  # dual-write OFF -> plan FK NULL
        self.assertIsNone(plan.strategy_assignment_id)
        with override_settings(**{"STRATEGY_SYMBOL_CONFLICT_POLICY_ENABLED": True}):
            jobs = promote_plan_to_demo_jobs(plan, actor=self.op)  # must NOT self-conflict
        self.assertTrue(jobs)

    def test_flags_off_promotion_is_byte_identical(self):
        a = self._asn(self.strat, self.acct_a)
        allocate_magic(a)
        self._mode_demo()
        plan = self._plan(self.acct_a, a, mid="p1")  # dual-write OFF at planning
        self.assertIsNone(plan.strategy_assignment_id)
        jobs = promote_plan_to_demo_jobs(plan, actor=self.op)
        self.assertTrue(jobs)
        for j in jobs:
            self.assertIsNone(j.assignment_id)              # no ownership written
            self.assertNotIn("magic", j.payload)            # no magic in payload
            self.assertEqual(j.payload.get("comment"), f"WAY{plan.id}L{j.payload['leg_index']}")

    def test_dual_write_on_writes_job_assignment_no_magic(self):
        a = self._asn(self.strat, self.acct_a)
        allocate_magic(a)
        self._mode_demo()
        with override_settings(**{DUAL: True}):  # dual-write ON, magic-send OFF
            plan = self._plan(self.acct_a, a, mid="p2")
            self.assertEqual(plan.strategy_assignment_id, a.id)
            jobs = promote_plan_to_demo_jobs(plan, actor=self.op)
        for j in jobs:
            self.assertEqual(j.assignment_id, a.id)
            self.assertEqual(j.strategy_id, self.strat.id)
            self.assertNotIn("magic", j.payload)             # magic-send still OFF

    def test_magic_send_on_adds_payload_magic(self):
        a = self._asn(self.strat, self.acct_a)
        allocate_magic(a)
        self._mode_demo()
        with override_settings(**{DUAL: True, MAGIC: True}):
            plan = self._plan(self.acct_a, a, mid="p3")
            jobs = promote_plan_to_demo_jobs(plan, actor=self.op)
        for j in jobs:
            self.assertEqual(j.payload.get("magic"), a.magic_number)
            self.assertGreaterEqual(j.payload["magic"], ASSIGNMENT_MAGIC_BASE)

    def test_magic_send_off_rollback_preserves_execution(self):
        # Simulate rollback of magic-send while dual-write stays on: ownership still
        # written, but no magic sent (byte-identical order payload to pre-magic).
        a = self._asn(self.strat, self.acct_a)
        allocate_magic(a)
        self._mode_demo()
        with override_settings(**{DUAL: True}):
            plan = self._plan(self.acct_a, a, mid="p4")
            jobs = promote_plan_to_demo_jobs(plan, actor=self.op)
        for j in jobs:
            self.assertEqual(j.assignment_id, a.id)
            self.assertNotIn("magic", j.payload)

    def test_magic_never_sent_on_shadow_path(self):
        from execution.signal_promotion import promote_plan_to_shadow_jobs
        a = self._asn(self.strat, self.acct_a, mode=StrategyAssignment.ExecutionMode.AUTO_SHADOW)
        allocate_magic(a)
        c = ExecutionControl.get_solo()
        c.signal_execution_mode = Mode.SHADOW
        c.auto_execution_enabled = True
        c.kill_switch_engaged = False
        c.save()
        with override_settings(**{DUAL: True, MAGIC: True}):
            plan = self._plan(self.acct_a, a, mid="p5")
            jobs = promote_plan_to_shadow_jobs(plan, actor=self.op)
        for j in jobs:
            self.assertNotIn("magic", j.payload)  # shadow jobs never carry magic


class FlagDefaultTests(TestCase):
    def test_all_flags_default_off(self):
        self.assertFalse(ownership_flags.dual_write_enabled())
        self.assertFalse(ownership_flags.magic_send_enabled())
        self.assertFalse(ownership_flags.ownership_read_enabled())
        self.assertFalse(ownership_flags.ownership_enforce_enabled())

    def test_sweep_window_hours_precedence(self):
        import os
        from unittest.mock import patch
        # default when neither setting nor env is set
        self.assertEqual(ownership_flags.ownership_sweep_window_hours(), 72.0)
        # env honoured
        with patch.dict(os.environ, {"STRATEGY_OWNERSHIP_SWEEP_WINDOW_HOURS": "24"}):
            self.assertEqual(ownership_flags.ownership_sweep_window_hours(), 24.0)
        # explicit Django setting wins over env
        with patch.dict(os.environ, {"STRATEGY_OWNERSHIP_SWEEP_WINDOW_HOURS": "24"}):
            with override_settings(STRATEGY_OWNERSHIP_SWEEP_WINDOW_HOURS=12):
                self.assertEqual(ownership_flags.ownership_sweep_window_hours(), 12.0)
        # junk / non-positive / non-finite / over-range → default (fail-safe: never
        # unbounded, never an OverflowError on timedelta).
        for bad in ("abc", 0, -5, "inf", "-inf", "nan", "1e13", 1e13, float("inf")):
            with self.subTest(bad=bad):
                with override_settings(STRATEGY_OWNERSHIP_SWEEP_WINDOW_HOURS=bad):
                    self.assertEqual(ownership_flags.ownership_sweep_window_hours(), 72.0)
        # a large-but-sane value within the cap is honoured
        with override_settings(STRATEGY_OWNERSHIP_SWEEP_WINDOW_HOURS=8000):
            self.assertEqual(ownership_flags.ownership_sweep_window_hours(), 8000.0)


class SweepWindowTests(OwnershipBase):
    """Forward-safety: ``sweep_trade_ownership`` only attributes trades INGESTED within the
    rolling window (``STRATEGY_OWNERSHIP_SWEEP_WINDOW_HOURS``), measured on
    ``Trade.created_at`` — so enabling/re-enabling DUAL_WRITE never implicitly walks the
    back-catalogue. Older trades are the explicit ``backfill_execution_ownership`` job."""

    def _trade(self, account, *, magic=0, comment="", open_time=None):
        return Trade.objects.create(
            account=account, ticket="tk", symbol="EURUSD", side="BUY",
            volume=Decimal("0.01"), open_time=open_time or timezone.now(),
            open_price=Decimal("1.085"), magic_number=magic, comment=comment)

    def _plan_with_owner(self, account, assignment, mid):
        with override_settings(**{DUAL: True}):
            return self._plan(account, assignment, mid=mid)

    def _eligible_trade(self, mid, *, open_time=None):
        """A live-shape eligible trade: magic=0 + WAY comment → LEGACY-resolvable to the
        single active (account, ti_signals) assignment."""
        a = self._asn(self.strat, self.acct_a)
        plan = self._plan_with_owner(self.acct_a, a, mid)
        t = self._trade(self.acct_a, magic=0, comment=f"WAY{plan.id}L1", open_time=open_time)
        return a, t

    def _set_created(self, trade, when):
        # created_at is auto_now_add; bypass it via a queryset .update().
        Trade.objects.filter(pk=trade.pk).update(created_at=when)
        trade.refresh_from_db()

    def test_sweep_skips_trade_ingested_before_window(self):
        with override_settings(**{DUAL: True}):
            a, t = self._eligible_trade("w1")
            self._set_created(t, timezone.now() - timedelta(days=10))
            ownership_stamp.sweep_trade_ownership()
        t.refresh_from_db()
        self.assertIsNone(t.strategy_assignment_id)  # back-catalogue not walked

    def test_sweep_stamps_recent_trade(self):
        with override_settings(**{DUAL: True}):
            a, t = self._eligible_trade("w2")  # created_at defaults to now
            counts = ownership_stamp.sweep_trade_ownership()
        t.refresh_from_db()
        self.assertEqual(t.strategy_assignment_id, a.id)
        self.assertEqual(counts.get(ownership_stamp.LEGACY), 1)

    def test_sweep_stamps_late_ingested_old_open_time(self):
        # open_time old (market time) but created_at recent (ingestion) → stamped: proves
        # the bound is on created_at, not open_time (async ingestion).
        with override_settings(**{DUAL: True}):
            a, t = self._eligible_trade("w3", open_time=timezone.now() - timedelta(days=30))
            ownership_stamp.sweep_trade_ownership()
        t.refresh_from_db()
        self.assertEqual(t.strategy_assignment_id, a.id)

    def test_window_setting_widens_scope(self):
        # 8000h (~11mo, within the sanity cap) covers a 10-day-old ingestion.
        with override_settings(**{DUAL: True, "STRATEGY_OWNERSHIP_SWEEP_WINDOW_HOURS": 8000}):
            a, t = self._eligible_trade("w4")
            self._set_created(t, timezone.now() - timedelta(days=10))
            ownership_stamp.sweep_trade_ownership()
        t.refresh_from_db()
        self.assertEqual(t.strategy_assignment_id, a.id)

    def test_absurd_window_falls_back_and_does_not_crash_sweep(self):
        # An inf/over-range window must degrade to the 72h default, NOT overflow timedelta on
        # every tick. A 10-day-old ingestion is then OUT of the (default) window → not stamped.
        # (Helper-level fall-back for inf/1e13/100000 is covered in FlagDefaultTests.)
        with override_settings(**{DUAL: True, "STRATEGY_OWNERSHIP_SWEEP_WINDOW_HOURS": "inf"}):
            a, t = self._eligible_trade("wbad")
            self._set_created(t, timezone.now() - timedelta(days=10))
            counts = ownership_stamp.sweep_trade_ownership()  # must not raise
        t.refresh_from_db()
        self.assertIsNone(t.strategy_assignment_id)
        self.assertIsInstance(counts, dict)

    def test_sweep_noop_when_dual_write_off(self):
        a, t = self._eligible_trade("w5")  # created now; flag OFF (default)
        result = ownership_stamp.sweep_trade_ownership()
        t.refresh_from_db()
        self.assertEqual(result, {"skipped": "dark"})
        self.assertIsNone(t.strategy_assignment_id)
