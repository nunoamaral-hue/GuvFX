"""P0-A — SAFE DEFAULT LOT: a genuinely fresh Wayond/signal-copy customer owns 0.01/leg from acquisition.

Root cause this pins: the signal-copy acquisition seams (``signal_copy_get`` / ``signal_copy_arm``) create
the AUTO_DEMO + ti_signals assignment but historically seeded NO ``AssignmentLegSizing`` row, so
``effective_lot_per_leg`` fell back to the ti_signals source cap (0.40) and Configure displayed 0.40. The
fix seeds ``AssignmentLegSizing.DEFAULT_LOT`` (0.01) on the CREATED path only — never resizing an existing
customer (support@ / no-row) and never overwriting an explicit Configure value.

The EXECUTION consumption of a 0.01 row (0.01/leg, <=0.03 for three legs, support@ no-row preserved) is
proven in ``execution/tests_customer_leg_sizing.py``; this file proves the ACQUISITION seed + one
end-to-end acquire->plan tie-through, plus preservation and fail-closed writes.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from billing.models import BetaTester
from execution.models import SignalSourceConfig
from strategies.models import (AssignmentLegSizing, Strategy, StrategyAssignment,
                               effective_lot_per_leg, seed_default_leg_sizing)
from strategies.views_sizing import AssignmentLegSizingView
from trading.models import TradingAccount

User = get_user_model()
GET_URL = "/api/strategies/strategies/signal-copy/get/"
ARM_URL = "/api/strategies/strategies/signal-copy/arm/"
MP = "mp-010"          # Wayond WIM -> signal_source ti_signals
TI = "ti_signals"
AM = StrategyAssignment.ExecutionMode
DEFAULT = Decimal("0.01")
BASE = dict(BETA_SELF_SERVE_ARM_ENABLED=True, BETA_RUNTIMES_ENABLED=True, BETA_MAX_TESTERS=1000)


def _admitted(username):
    u = User.objects.create_user(username=username, email=f"{username}@x.invalid", password="x")
    BetaTester.objects.create(email=u.email)
    return u


def _demo_acct(user, number, *, is_demo=True, is_active=True):
    return TradingAccount.objects.create(
        user=user, name=number, account_number=number, broker_name="DemoBroker",
        is_demo=is_demo, is_active=is_active, password_enc="enc")


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@override_settings(**BASE)
@mock.patch("strategies.views._arm_cohort_approved", new=lambda user: True)
class FreshAcquisitionSeedsDefault(TestCase):
    """Requirements 1-6: fresh acquisition creates the assignment AND a 0.01 sizing row; Configure sees 0.01."""

    def setUp(self):
        self.user = _admitted("p0a")
        self.acct = _demo_acct(self.user, "P0A")
        self.c = _client(self.user)
        # ti_signals owns the operator 0.40/leg policy (the source-cap fallback the bug exposed).
        SignalSourceConfig.objects.create(
            source=TI, auto_demo_execution_enabled=True, total_lot_target=Decimal("1.20"),
            max_lot_per_leg=Decimal("0.40"), max_total_lot=Decimal("1.20"))
        self.factory = APIRequestFactory()

    def _get(self, **body):
        return self.c.post(GET_URL, {"marketplace_strategy_id": MP, "account_id": self.acct.id, **body},
                           format="json")

    def _configure_get(self, asn):
        req = self.factory.get("/x")
        force_authenticate(req, user=self.user)
        return AssignmentLegSizingView.as_view()(req, pk=asn.id)

    def test_get_seeds_001_row_and_effective_is_001(self):
        r = self._get()
        self.assertEqual(r.status_code, 201, r.content)
        asn = StrategyAssignment.objects.get(id=r.json()["assignment_id"])
        row = AssignmentLegSizing.objects.get(assignment=asn)          # req 2: created automatically
        self.assertEqual(row.lot_per_leg, DEFAULT)                     # req 3
        self.assertEqual(row.version, 1)
        self.assertEqual(effective_lot_per_leg(asn), DEFAULT)          # req 4

    def test_configure_get_returns_001_not_the_040_source_cap(self):
        asn = StrategyAssignment.objects.get(id=self._get().json()["assignment_id"])
        resp = self._configure_get(asn)                               # req 5/6: API/UI value
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["lot_per_leg"], "0.01")
        self.assertTrue(resp.data["is_override"])                     # a real persisted row, not the fallback
        self.assertEqual(resp.data["source_cap"], "0.40")            # cap still visible, but not the default

    def test_arm_without_prior_get_also_seeds_001(self):
        # A customer who arms directly (no prior Get) still owns 0.01 from creation.
        with mock.patch("strategies.views._account_execution_ready", return_value=(True, "ready")):
            r = self.c.post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": self.acct.id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        asn = StrategyAssignment.objects.get(account=self.acct, signal_source=TI)
        self.assertEqual(AssignmentLegSizing.objects.get(assignment=asn).lot_per_leg, DEFAULT)

    def test_three_tp_signal_targets_at_most_003_total(self):
        # Requirement 7 end-to-end: acquire via Get, then plan a 3-TP signal -> 0.01/leg, <=0.03 total.
        from execution import signal_planning as planning
        from signal_intake.models import PendingSignalApproval
        asn = StrategyAssignment.objects.get(id=self._get().json()["assignment_id"])
        asn.is_active = True
        asn.save(update_fields=["is_active"])
        approval = PendingSignalApproval.objects.create(
            source=TI, message_id="p0a-3tp", symbol="XAUUSD", direction="BUY", entry="4000",
            stop_loss="3990", take_profit="4010", take_profits=["4010", "4020", "4030"],
            status=PendingSignalApproval.Status.APPROVED)
        plan = planning.plan_demo_execution(approval, account=self.acct, assignment=asn)
        lots = [leg.lot_size for leg in plan.legs.order_by("leg_index")]
        self.assertEqual(lots, [DEFAULT] * 3)
        self.assertEqual(plan.total_lot, Decimal("0.03"))
        self.assertLessEqual(plan.total_lot, Decimal("0.03"))


@override_settings(**BASE)
@mock.patch("strategies.views._arm_cohort_approved", new=lambda user: True)
class ExistingCustomersNeverResized(TestCase):
    """Requirements 9-10 + STOP guards: re-acquiring never seeds/overwrites an existing assignment."""

    def setUp(self):
        self.user = _admitted("p0b")
        self.acct = _demo_acct(self.user, "P0B")
        self.c = _client(self.user)
        SignalSourceConfig.objects.create(
            source=TI, auto_demo_execution_enabled=True, total_lot_target=Decimal("1.20"),
            max_lot_per_leg=Decimal("0.40"), max_total_lot=Decimal("1.20"))

    def _get(self):
        return self.c.post(GET_URL, {"marketplace_strategy_id": MP, "account_id": self.acct.id}, format="json")

    def test_reget_is_noop_and_never_overwrites_explicit_value(self):
        asn = StrategyAssignment.objects.get(id=self._get().json()["assignment_id"])
        # Seed is v1 @ 0.01; the customer deliberately raises to 0.05 (a real change -> v2).
        from strategies.models import set_assignment_lot_per_leg
        set_assignment_lot_per_leg(asn, Decimal("0.05"), user=self.user)
        again = self._get()                                           # re-acquire (idempotent, 200)
        self.assertEqual(again.status_code, 200)
        row = AssignmentLegSizing.objects.get(assignment=asn)
        self.assertEqual(row.lot_per_leg, Decimal("0.05"))           # NOT reset to 0.01 by re-acquisition
        self.assertEqual(row.version, 2)                             # customer's edit stands; no reseed bump

    def test_existing_no_row_assignment_is_not_seeded_by_reget(self):
        # Simulate the support@ / pre-seed shape: an assignment that resolves through this seam but has NO
        # sizing row. Re-acquiring must NOT seed it (created-only guard), so it keeps falling back to the
        # source-global cap (support@ preservation).
        asn = StrategyAssignment.objects.get(id=self._get().json()["assignment_id"])
        AssignmentLegSizing.objects.filter(assignment=asn).delete()   # legacy no-row shape
        self.assertEqual(effective_lot_per_leg(asn), Decimal("0.40"))  # source-global before
        again = self._get()                                          # re-acquire returns the SAME asn
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["assignment_id"], asn.id)
        self.assertFalse(AssignmentLegSizing.objects.filter(assignment=asn).exists())  # STILL no row
        self.assertEqual(effective_lot_per_leg(asn), Decimal("0.40"))  # source-global preserved


class SeedHelperContract(TestCase):
    """Direct contract of the shared seed used by all three acquisition seams (get / arm / marketplace)."""

    def setUp(self):
        self.user = User.objects.create_user(username="h", email="h@x.invalid", password="x")
        self.acct = _demo_acct(self.user, "H1")
        self.strat = Strategy.objects.create(owner=self.user, name="Wayond WIM")
        self.asn = StrategyAssignment.objects.create(
            strategy=self.strat, account=self.acct, signal_source=TI,
            execution_mode=AM.AUTO_DEMO, stage=StrategyAssignment.STAGE_LIVE)

    def test_seeds_default_001(self):
        row = seed_default_leg_sizing(self.asn)
        self.assertEqual(row.lot_per_leg, DEFAULT)
        self.assertEqual(AssignmentLegSizing.objects.filter(assignment=self.asn).count(), 1)

    def test_idempotent_never_overwrites_existing(self):
        AssignmentLegSizing.objects.create(assignment=self.asn, lot_per_leg=Decimal("0.05"))
        row = seed_default_leg_sizing(self.asn)                       # must NOT overwrite
        self.assertEqual(row.lot_per_leg, Decimal("0.05"))
        self.assertEqual(AssignmentLegSizing.objects.filter(assignment=self.asn).count(), 1)
