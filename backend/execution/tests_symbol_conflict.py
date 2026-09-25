"""B1 — DARK multi-strategy same-symbol conflict policy + 5-account matrix (Phase 11). TEST-ONLY.

Only PROVEN-fresh HEDGING accounts allow a different assignment to concurrently own the same symbol;
NETTING / EXCHANGE / UNKNOWN / stale fail closed. Same-assignment multi-leg / repeated signals never
trip (owner is excluded). Flag OFF -> inert. Cross-account is always isolated.
"""
import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution.models import SignalExecutionPlan
from execution.risk_controls import evaluate_symbol_conflict
from hosted_workspace import margin_mode as mm
from hosted_workspace.models import HostedMt5Workspace
from signal_intake.models import PendingSignalApproval
from strategies.models import Strategy, StrategyAssignment
from trading.models import Trade, TradingAccount

User = get_user_model()
FLAG = "STRATEGY_SYMBOL_CONFLICT_POLICY_ENABLED"
ON = {FLAG: True}


class ConflictBase(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="u", email="u@x.invalid", password="x")
        self._c = 0

    def _acct(self, margin=None, fresh=True):
        self._c += 1
        acct = TradingAccount.objects.create(
            user=self.u, name=f"A{self._c}", account_number=f"AC{self._c}", is_demo=True, broker_name="Demo")
        if margin is not None:
            last = timezone.now() - datetime.timedelta(hours=1 if fresh else 48)
            HostedMt5Workspace.objects.create(
                trading_account=acct, proj_margin_mode=margin, last_decision_at=last)
        return acct

    def _asn(self, acct):
        self._c += 1
        strat = Strategy.objects.create(owner=self.u, name=f"S{self._c}")
        return StrategyAssignment.objects.create(
            strategy=strat, account=acct, is_active=True, stage=StrategyAssignment.STAGE_LIVE,
            signal_source="ti_signals", execution_mode=StrategyAssignment.ExecutionMode.AUTO_DEMO)

    def _plan(self, acct, owner, symbol, status=SignalExecutionPlan.Status.PLANNED):
        self._c += 1
        appr = PendingSignalApproval.objects.create(
            source="ti_signals", message_id=f"m{self._c}", symbol=symbol, direction="BUY",
            stop_loss="1.0", take_profits=["1.1"], status=PendingSignalApproval.Status.APPROVED)
        return SignalExecutionPlan.objects.create(
            approval=appr, account=acct, source="ti_signals", message_id=f"m{self._c}", symbol=symbol,
            direction="BUY", stop_loss="1.0", is_demo=True, signal_timestamp=timezone.now(),
            status=status, strategy_assignment=owner)

    def _trade(self, acct, owner, symbol, closed=False):
        self._c += 1
        return Trade.objects.create(
            account=acct, ticket=f"tk{self._c}", symbol=symbol, side="BUY", volume=Decimal("0.01"),
            open_time=timezone.now(), open_price=Decimal("1.0"),
            close_time=(timezone.now() if closed else None), strategy_assignment=owner)


class SymbolConflictPolicyTests(ConflictBase):
    def test_flag_off_is_inert_even_on_netting_conflict(self):
        acct = self._acct(mm.RETAIL_NETTING)
        a, b = self._asn(acct), self._asn(acct)
        self._trade(acct, a, "XAUUSD")                 # A owns XAUUSD
        plan = self._plan(acct, b, "XAUUSD")           # B promotes XAUUSD
        self.assertIsNone(evaluate_symbol_conflict(plan, b))  # flag OFF -> None

    def test_hedging_allows_concurrent_same_symbol(self):
        acct = self._acct(mm.RETAIL_HEDGING)
        a, b = self._asn(acct), self._asn(acct)
        self._trade(acct, a, "XAUUSD")
        plan = self._plan(acct, b, "XAUUSD")
        with override_settings(**ON):
            self.assertIsNone(evaluate_symbol_conflict(plan, b))  # hedging -> independent -> allow

    def test_netting_blocks_foreign_open_trade(self):
        acct = self._acct(mm.RETAIL_NETTING)
        a, b = self._asn(acct), self._asn(acct)
        self._trade(acct, a, "XAUUSD")
        plan = self._plan(acct, b, "XAUUSD")
        with override_settings(**ON):
            self.assertEqual(evaluate_symbol_conflict(plan, b), "multi_strategy_netting_symbol_conflict")

    def test_netting_blocks_foreign_planned_plan(self):
        acct = self._acct(mm.RETAIL_NETTING)
        a, b = self._asn(acct), self._asn(acct)
        self._plan(acct, a, "XAUUSD", status=SignalExecutionPlan.Status.PROMOTED)  # A's order-intent
        plan = self._plan(acct, b, "XAUUSD")
        with override_settings(**ON):
            self.assertEqual(evaluate_symbol_conflict(plan, b), "multi_strategy_netting_symbol_conflict")

    def test_netting_different_symbol_allowed(self):
        acct = self._acct(mm.RETAIL_NETTING)
        a, b = self._asn(acct), self._asn(acct)
        self._trade(acct, a, "XAUUSD")
        plan = self._plan(acct, b, "EURUSD")           # different symbol -> no conflict
        with override_settings(**ON):
            self.assertIsNone(evaluate_symbol_conflict(plan, b))

    def test_unknown_mode_blocks_on_foreign(self):
        acct = self._acct(margin=None)                 # no workspace -> UNKNOWN
        a, b = self._asn(acct), self._asn(acct)
        self._trade(acct, a, "XAUUSD")
        plan = self._plan(acct, b, "XAUUSD")
        with override_settings(**ON):
            self.assertEqual(evaluate_symbol_conflict(plan, b),
                             "margin_mode_unknown_multi_strategy_conflict")

    def test_stale_hedging_treated_unknown(self):
        acct = self._acct(mm.RETAIL_HEDGING, fresh=False)  # stale -> UNKNOWN -> fail closed
        a, b = self._asn(acct), self._asn(acct)
        self._trade(acct, a, "XAUUSD")
        plan = self._plan(acct, b, "XAUUSD")
        with override_settings(**ON):
            self.assertEqual(evaluate_symbol_conflict(plan, b),
                             "margin_mode_unknown_multi_strategy_conflict")

    def test_owner_none_fails_closed_on_non_hedging(self):
        acct = self._acct(mm.RETAIL_NETTING)
        plan = self._plan(acct, None, "XAUUSD")        # unresolvable owner
        with override_settings(**ON):
            self.assertEqual(evaluate_symbol_conflict(plan, None),
                             "multi_strategy_netting_symbol_conflict")

    def test_exchange_mode_reason(self):
        acct = self._acct(mm.EXCHANGE)
        a, b = self._asn(acct), self._asn(acct)
        self._trade(acct, a, "XAUUSD")
        plan = self._plan(acct, b, "XAUUSD")
        with override_settings(**ON):
            self.assertEqual(evaluate_symbol_conflict(plan, b),
                             "exchange_mode_multi_strategy_uncertified")

    def test_same_assignment_multi_leg_not_a_conflict(self):
        acct = self._acct(mm.RETAIL_NETTING)
        a = self._asn(acct)
        self._trade(acct, a, "XAUUSD")                 # A's own open position (an earlier leg)
        plan = self._plan(acct, a, "XAUUSD")           # A promotes again on XAUUSD
        with override_settings(**ON):
            self.assertIsNone(evaluate_symbol_conflict(plan, a))  # own exposure excluded

    def test_same_assignment_repeated_plan_not_a_conflict(self):
        acct = self._acct(mm.RETAIL_NETTING)
        a = self._asn(acct)
        self._plan(acct, a, "XAUUSD", status=SignalExecutionPlan.Status.PROMOTED)
        plan = self._plan(acct, a, "XAUUSD")
        with override_settings(**ON):
            self.assertIsNone(evaluate_symbol_conflict(plan, a))

    def test_null_owner_manual_position_is_foreign_on_netting(self):
        acct = self._acct(mm.RETAIL_NETTING)
        b = self._asn(acct)
        self._trade(acct, None, "XAUUSD")              # manual / legacy magic=0 (unattributed)
        plan = self._plan(acct, b, "XAUUSD")
        with override_settings(**ON):
            self.assertEqual(evaluate_symbol_conflict(plan, b),
                             "multi_strategy_netting_symbol_conflict")

    def test_null_fk_promoting_plan_not_self_conflict(self):
        # Review HIGH regression: a promoting plan whose OWN FK is NULL (owner resolved via fallback) must
        # not count itself as a foreign owner. Single strategy, no foreign exposure -> allowed.
        acct = self._acct(mm.RETAIL_NETTING)
        a = self._asn(acct)
        plan = self._plan(acct, None, "XAUUSD")  # FK NULL; owner resolved externally to `a`
        with override_settings(**ON):
            self.assertIsNone(evaluate_symbol_conflict(plan, a))  # excludes itself by pk

    def test_closed_foreign_trade_not_a_conflict(self):
        acct = self._acct(mm.RETAIL_NETTING)
        a, b = self._asn(acct), self._asn(acct)
        self._trade(acct, a, "XAUUSD", closed=True)    # closed -> no live exposure
        plan = self._plan(acct, b, "XAUUSD")
        with override_settings(**ON):
            self.assertIsNone(evaluate_symbol_conflict(plan, b))

    def test_cross_account_never_conflicts(self):
        acct1 = self._acct(mm.RETAIL_NETTING)
        acct2 = self._acct(mm.RETAIL_NETTING)
        a = self._asn(acct1)
        b = self._asn(acct2)
        self._trade(acct1, a, "XAUUSD")                # A owns XAUUSD on acct1
        plan = self._plan(acct2, b, "XAUUSD")          # B promotes XAUUSD on acct2 (different account)
        with override_settings(**ON):
            self.assertIsNone(evaluate_symbol_conflict(plan, b))


class FiveAccountMatrixTests(ConflictBase):
    """Phase 11 — 1 user x 5 accounts x >=5 strategies, at the policy level."""

    def test_G_five_strategies_xauusd_on_hedging_all_allowed(self):
        acct = self._acct(mm.RETAIL_HEDGING)
        owners = [self._asn(acct) for _ in range(5)]
        # simulate first four already owning XAUUSD; the fifth still allowed (hedging independent)
        for o in owners[:4]:
            self._trade(acct, o, "XAUUSD")
        plan = self._plan(acct, owners[4], "XAUUSD")
        with override_settings(**ON):
            self.assertIsNone(evaluate_symbol_conflict(plan, owners[4]))

    def test_H_five_strategies_xauusd_on_netting_only_first_allowed(self):
        acct = self._acct(mm.RETAIL_NETTING)
        owners = [self._asn(acct) for _ in range(5)]
        # first strategy: no foreign owner yet -> allowed
        first_plan = self._plan(acct, owners[0], "XAUUSD")
        with override_settings(**ON):
            self.assertIsNone(evaluate_symbol_conflict(first_plan, owners[0]))
        # it opens a position; every other strategy is now blocked on XAUUSD
        self._trade(acct, owners[0], "XAUUSD")
        for o in owners[1:]:
            plan = self._plan(acct, o, "XAUUSD")
            with override_settings(**ON):
                self.assertEqual(evaluate_symbol_conflict(plan, o),
                                 "multi_strategy_netting_symbol_conflict")

    def test_I_xauusd_plus_eurusd_on_netting_coexist(self):
        acct = self._acct(mm.RETAIL_NETTING)
        a, b = self._asn(acct), self._asn(acct)
        self._trade(acct, a, "XAUUSD")
        plan = self._plan(acct, b, "EURUSD")
        with override_settings(**ON):
            self.assertIsNone(evaluate_symbol_conflict(plan, b))

    def test_mixed_modes_independent_per_account(self):
        hedge = self._acct(mm.RETAIL_HEDGING)
        net = self._acct(mm.RETAIL_NETTING)
        unk = self._acct(margin=None)
        for acct, expect_block in ((hedge, False), (net, True), (unk, True)):
            a, b = self._asn(acct), self._asn(acct)
            self._trade(acct, a, "XAUUSD")
            plan = self._plan(acct, b, "XAUUSD")
            with override_settings(**ON):
                reason = evaluate_symbol_conflict(plan, b)
            self.assertEqual(bool(reason), expect_block, acct.name)
