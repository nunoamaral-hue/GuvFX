"""GFX-PKT-TI-SOURCE-SCOPED-LOT-SIZING — ti_signals sizes 0.40/leg (1.20 total); wayond unchanged.

Every sizing gate is PER-SOURCE — the planning split, the promotion re-validation, the worker cap
and the free-margin guard. Wayond keeps the global 0.02/0.06 defaults. No order is ever placed:
planning + promotion are no-order (promotion creates SUPPRESSED shadow jobs), the worker cap test
calls the pure helper, and the free-margin guard's bridge call is mocked (never networks).
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

import mt5_trade_ingest_worker as worker
from execution import risk_controls
from execution import signal_planning as planning
from execution.signal_promotion import PromotionRejected, promote_plan_to_shadow_jobs
from execution.models import (
    ExecutionJob,
    ProposedOrderLeg,
    SignalExecutionPlan,
    SignalSourceConfig,
)
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount, Trade

User = get_user_model()
TI = "ti_signals"
WAY = "wayond"


class _Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True
        )
        # ti_signals owns the 0.40/leg (1.20 total) policy; wayond keeps the global defaults.
        SignalSourceConfig.objects.create(
            source=TI, auto_demo_execution_enabled=True, total_lot_target=Decimal("1.20"),
            max_lot_per_leg=Decimal("0.40"), max_total_lot=Decimal("1.20"),
        )
        SignalSourceConfig.objects.create(
            source=WAY, auto_demo_execution_enabled=True, total_lot_target=Decimal("0.03"),
        )  # max_lot_per_leg / max_total_lot default to 0.02 / 0.06

    def _approval(self, mid, *, source, symbol="XAUUSD"):
        return PendingSignalApproval.objects.create(
            source=source, message_id=mid, symbol=symbol, direction="BUY",
            entry="4000", stop_loss="3990", take_profit="4010",
            take_profits=["4010", "4020", "4030"], status=PendingSignalApproval.Status.APPROVED,
        )

    def _plan_with_legs(self, *, source, lots, symbol="XAUUSD", mid="p1"):
        appr = self._approval(mid, source=source, symbol=symbol)
        plan = SignalExecutionPlan.objects.create(
            approval=appr, account=self.demo, source=source, message_id=mid, symbol=symbol,
            direction="BUY", stop_loss="3990", is_demo=True, signal_timestamp=timezone.now(),
            status=SignalExecutionPlan.Status.PLANNED,
        )
        for i, lot in enumerate(lots, start=1):
            ProposedOrderLeg.objects.create(
                plan=plan, leg_index=i, take_profit="4010", stop_loss="3990",
                lot_size=Decimal(lot), status=ProposedOrderLeg.Status.PLANNED,
            )
        return plan


class SplitSizingTests(_Base):
    # 1 + 2 — TI three-leg → 0.40 / 0.40 / 0.40, total 1.20
    def test_ti_three_leg_splits_040_total_120(self):
        plan = planning.plan_demo_execution(self._approval("t1", source=TI), account=self.demo)
        lots = [l.lot_size for l in plan.legs.order_by("leg_index")]
        self.assertEqual(lots, [Decimal("0.40"), Decimal("0.40"), Decimal("0.40")])
        self.assertEqual(plan.total_lot, Decimal("1.20"))
        self.assertEqual(ExecutionJob.objects.count(), 0)  # 13 — no order placed

    # 3 — wayond sizing unchanged (0.03 target → 0.01/leg under its 0.02 cap)
    def test_wayond_sizing_unchanged(self):
        plan = planning.plan_demo_execution(
            self._approval("w1", source=WAY, symbol="EURUSD"), account=self.demo)
        lots = [l.lot_size for l in plan.legs.order_by("leg_index")]
        self.assertEqual(lots, [Decimal("0.01"), Decimal("0.01"), Decimal("0.01")])
        self.assertEqual(plan.total_lot, Decimal("0.03"))

    # 4 — TI's size cannot leak to another source: even with a high total_lot_target, wayond's
    #     per-leg cap (0.02) clamps it — it can never reach 0.40.
    def test_ti_size_cannot_leak_to_wayond(self):
        SignalSourceConfig.objects.filter(source=WAY).update(total_lot_target=Decimal("1.20"))
        plan = planning.plan_demo_execution(
            self._approval("w2", source=WAY, symbol="EURUSD"), account=self.demo)
        lots = [l.lot_size for l in plan.legs.order_by("leg_index")]
        self.assertTrue(all(l == Decimal("0.02") for l in lots))  # clamped to wayond's 0.02 cap
        self.assertEqual(plan.total_lot, Decimal("0.06"))          # and its 0.06 total cap


class PromotionCapTests(_Base):
    def _reject_code(self, plan):
        with self.assertRaises(PromotionRejected) as cm:
            promote_plan_to_shadow_jobs(plan, actor=self.user)
        return cm.exception.code

    # 5 — a leg above the TI per-leg ceiling (0.41 > 0.40) is rejected at promotion
    def test_promotion_rejects_leg_over_ti_cap(self):
        plan = self._plan_with_legs(source=TI, lots=("0.41",), mid="over-leg")
        self.assertEqual(self._reject_code(plan), "lot_out_of_range")
        self.assertEqual(ExecutionJob.objects.count(), 0)

    # 6 — a total above the TI per-signal ceiling is rejected INDEPENDENTLY of the per-leg cap:
    #     four legs each within 0.40 still sum to 1.60 > 1.20 → total_lot_exceeds_cap.
    def test_promotion_rejects_total_over_ti_cap(self):
        plan = self._plan_with_legs(source=TI, lots=("0.40", "0.40", "0.40", "0.40"), mid="over-total")
        self.assertEqual(self._reject_code(plan), "total_lot_exceeds_cap")

    # TI at exactly the cap promotes cleanly to 3 SUPPRESSED shadow jobs (no order)
    def test_ti_at_cap_promotes_to_three_shadow_jobs(self):
        plan = self._plan_with_legs(source=TI, lots=("0.40", "0.40", "0.40"), mid="ok")
        jobs = promote_plan_to_shadow_jobs(plan, actor=self.user)
        self.assertEqual(len(jobs), 3)  # 12 — three independent legs → three jobs
        self.assertTrue(all(j.job_type == ExecutionJob.JobType.PLACE_ORDER_SHADOW for j in jobs))
        # each job's payload carries the source + the per-source cap (source reaches worker/bridge)
        for j in jobs:
            self.assertEqual(j.payload["signal_source"], TI)
            self.assertEqual(j.payload["max_lot"], "0.40")


class WorkerCapTests(SimpleTestCase):
    # 7 — worker admits 0.40 when the payload cap allows it, rejects above the hard ceiling,
    #     and fail-closes to 0.02 when the payload carries no source cap.
    def test_worker_max_lot_source_scoped_and_failclosed(self):
        self.assertEqual(worker.worker_max_lot({"max_lot": "0.40"}), 0.40)   # TI cap admitted
        self.assertEqual(worker.worker_max_lot({}), 0.02)                    # fail-closed default
        self.assertEqual(worker.worker_max_lot({"max_lot": "bad"}), 0.02)    # invalid → default
        self.assertEqual(worker.worker_max_lot({"max_lot": "5.0"}),
                         worker.WORKER_HARD_MAX_LOT)                         # bounded by hard ceiling
        self.assertLessEqual(0.40, worker.worker_max_lot({"max_lot": "0.40"}))


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FreeMarginGuardTests(_Base):
    def _plan(self):
        return self._plan_with_legs(source=TI, lots=("0.40", "0.40", "0.40"), mid="mg")

    # 11 — the guard rejects a promotion whose PROJECTED margin level falls below the floor
    def test_margin_guard_rejects_low_projected_level(self):
        plan = self._plan()
        legs = list(plan.legs.all())
        # equity 1000; after a 0.40 check leg: free 50, margin 100 → per-lot 250; full 1.20 plan
        # → projected margin used 1150 → level ~87% < 300% floor → reject.
        resp = {"equity": 1000.0, "free_margin": 50.0, "margin": 100.0, "margin_level": 90.0}
        with mock.patch.object(risk_controls, "MARGIN_GUARD_ENABLED", True), \
             mock.patch.dict("os.environ", {"GUVFX_WINDOWS_AGENT_BASE_URL": "http://bridge"}), \
             mock.patch("urllib.request.urlopen", return_value=_FakeResp(resp)):
            self.assertEqual(
                risk_controls._margin_guard_reason(plan, legs, Decimal("1.20")),
                "margin_level_too_low",
            )

    # ample margin → no block
    def test_margin_guard_allows_ample_margin(self):
        plan = self._plan()
        legs = list(plan.legs.all())
        resp = {"equity": 50000.0, "free_margin": 49700.0, "margin": 100.0}
        with mock.patch.object(risk_controls, "MARGIN_GUARD_ENABLED", True), \
             mock.patch.dict("os.environ", {"GUVFX_WINDOWS_AGENT_BASE_URL": "http://bridge"}), \
             mock.patch("urllib.request.urlopen", return_value=_FakeResp(resp)):
            self.assertIsNone(risk_controls._margin_guard_reason(plan, legs, Decimal("1.20")))

    # 11b — FAIL-OPEN: a bridge/network error must never block a within-spec signal
    def test_margin_guard_fail_open_on_bridge_error(self):
        plan = self._plan()
        legs = list(plan.legs.all())
        with mock.patch.object(risk_controls, "MARGIN_GUARD_ENABLED", True), \
             mock.patch.dict("os.environ", {"GUVFX_WINDOWS_AGENT_BASE_URL": "http://bridge"}), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("bridge down")):
            self.assertIsNone(risk_controls._margin_guard_reason(plan, legs, Decimal("1.20")))

    # small orders (<= the global default total) skip the guard entirely (no network call)
    def test_margin_guard_skips_small_orders(self):
        plan = self._plan()
        legs = list(plan.legs.all())
        with mock.patch.object(risk_controls, "MARGIN_GUARD_ENABLED", True), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("must not network")):
            self.assertIsNone(risk_controls._margin_guard_reason(plan, legs, Decimal("0.06")))


class ExposureIndependenceTests(_Base):
    _tk = 0

    def _open_trade(self, *, symbol="XAUUSD", volume="0.40"):
        type(self)._tk += 1
        return Trade.objects.create(
            account=self.demo, symbol=symbol, side="BUY", volume=Decimal(volume),
            ticket=f"e{type(self)._tk}", open_time=timezone.now(), open_price=Decimal("4000"),
            profit=Decimal("0"),
        )

    # 10 — the exposure caps (now 2.40) are still independently enforced above the ceiling
    def test_symbol_exposure_still_rejects_above_cap(self):
        # 2.40 already open on XAUUSD; a new 0.40×3 = 1.20 plan would exceed 2.40 → reject.
        for _ in range(6):
            self._open_trade(volume="0.40")  # 2.40 open
        plan = self._plan_with_legs(source=TI, lots=("0.40", "0.40", "0.40"), mid="exp")
        with mock.patch.object(risk_controls, "MARGIN_GUARD_ENABLED", False):
            reason = risk_controls.evaluate_promotion_risk(plan, list(plan.legs.all()))
        self.assertIn(reason, ("symbol_exposure_exceeded", "account_exposure_exceeded"))

    # deployed exposure defaults are the documented 2.40 (bounded, not a blind 20×)
    def test_exposure_defaults_are_240(self):
        self.assertEqual(risk_controls.MAX_ACCOUNT_EXPOSURE_LOT, Decimal("2.40"))
        self.assertEqual(risk_controls.MAX_SYMBOL_EXPOSURE_LOT, Decimal("2.40"))
