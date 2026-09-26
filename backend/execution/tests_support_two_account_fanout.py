"""Wayond support@ two-account concurrent routing — Phase-6 adversarial proof (ADR-0020 fan-out).

Existing fan-out coverage (``tests_tb1_fanout_router`` / ``tests_multiuser_isolation``) proves the
generic N-account path with TWO DIFFERENT users. This file proves the SPECIFIC production shape the
support@ POC turns on: ONE user (support@) owning TWO demo accounts, ONE strategy, TWO assignments
bound to the SAME Telegram source (``ti_signals``) — Account A (magic 1_000_000_010, no per-leg
sizing row → source-global) and Account B (a distinct deterministic magic, an EXPLICIT conservative
0.01 per-leg override, NOT inherited from A).

It certifies, with NO new routing code (the same ``_resolve_targets`` queryset), that:
  * both same-user accounts fan out from one signal;
  * each destination carries ITS OWN deterministic magic (``1e9 + assignment.id``) — distinct, in the
    reserved band, and Account A's magic is IMMUTABLE when Account B is added;
  * per-account order jobs bind to the OWNING assignment (prod-accurate: dual-write ON, magic-send OFF)
    and, when magic-send is armed, the payload magic is per-account and NEVER crossed;
  * per-assignment sizing DIVERGES in a single fan-out (A source-global, B explicit 0.01);
  * an account NOT bound to the source is never injected into the fan-out;
  * the SAME function scales to 5 and 20 destinations with no code change
    (NO_NEW_ARCHITECTURE_FOR_ACCOUNTS_3_TO_5, with headroom to 20).

Execution stays DEMO-only; every assertion is on demo accounts and dry/real-demo jobs.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from execution import auto_router
from execution.auto_router import _resolve_targets, effective_mode, MODE_AUTO_DEMO, MODE_MANUAL
from execution.models import ExecutionControl, ExecutionJob, SignalSourceConfig, TerminalNode
from execution.signal_planning import _customer_leg_size_override
from signal_intake.models import AcquiredMessage, ParserProfile, PendingSignalApproval, SignalProvider
from strategies.magic_allocation import ASSIGNMENT_MAGIC_BASE, allocate_magic
from strategies.models import AssignmentLegSizing, Strategy, StrategyAssignment
from trading.models import TradingAccount

User = get_user_model()
AM = StrategyAssignment.ExecutionMode
DEMO = AM.AUTO_DEMO
JT = ExecutionJob.JobType
O = AcquiredMessage.Outcome
SOURCE = "ti_signals"
FANOUT = override_settings(MULTI_ACCOUNT_ROUTING_ENABLED=True)
# Prod-accurate ownership posture: dual-write ON (job.assignment is written), magic-send OFF
# (the backend does NOT put magic in the payload — the listener is the sender). A separate test
# arms magic-send to prove the payload magic is per-account.
DUALWRITE = override_settings(STRATEGY_OWNERSHIP_DUAL_WRITE_ENABLED=True)


class _SupportBase(TestCase):
    """ONE user (support@ shape), ONE strategy, fully-armed AUTO_DEMO ti_signals config."""

    def setUp(self):
        self.system = User.objects.create_user(
            username="guvfx-auto-system", email="sys@x.invalid", password="x",
            is_staff=True, is_superuser=True)
        self.support = User.objects.create_user(
            username="support", email="support@guvfx.com", password="x")
        # ONE strategy owned by support (the Wayond WIM shape) → N accounts, N assignments.
        self.strategy = Strategy.objects.create(owner=self.support, name="Wayond WIM Strategy")
        self.parser = ParserProfile.objects.create(
            slug="ti_v1", certification_level=ParserProfile.CertificationLevel.MEDIUM)
        self.provider = SignalProvider.objects.create(
            slug=SOURCE, name="TI Signals", telegram_chat_id="-100123",
            parser_profile=self.parser, status=SignalProvider.Status.ARMED)
        # source-global per-leg (0.04) is deliberately DIFFERENT from Account B's 0.01 override so a
        # divergence is observable; cap 0.10 leaves B's 0.01 un-clamped.
        self.cfg = SignalSourceConfig.objects.create(
            source=SOURCE, auto_demo_execution_enabled=True,
            total_lot_target=Decimal("0.04"), max_lot_per_leg=Decimal("0.10"))
        ctrl = ExecutionControl.get_solo()
        ctrl.signal_execution_mode = ExecutionControl.SignalExecutionMode.DEMO
        ctrl.auto_execution_enabled = True
        ctrl.kill_switch_engaged = False
        ctrl.save()

    def _acct(self, number, *, is_demo=True, is_active=True):
        # ADR-0020: a fanned account needs its own dedicated ACTIVE node (else its PLACE_ORDER is
        # NULL-node and the shared legacy worker would claim it on another tenant's terminal).
        node = TerminalNode.objects.create(hostname=f"node-{number}", status=TerminalNode.Status.ACTIVE)
        return TradingAccount.objects.create(
            user=self.support, name=number, account_number=number, is_demo=is_demo,
            is_active=is_active, broker_name="DemoBroker", terminal_node=node)

    def _bind(self, acct, *, source=SOURCE, active=True, mode=DEMO, sizing=None, strategy=None):
        asn = StrategyAssignment.objects.create(
            strategy=strategy or self.strategy, account=acct, execution_mode=mode,
            signal_source=source, is_active=active, stage=StrategyAssignment.STAGE_LIVE)
        allocate_magic(asn)  # certified deterministic allocator: 1e9 + asn.id
        if sizing is not None:
            AssignmentLegSizing.objects.create(assignment=asn, lot_per_leg=Decimal(str(sizing)))
        return asn

    def _approval(self, message_id="m1"):
        return PendingSignalApproval.objects.create(
            source=SOURCE, message_id=message_id, provider=self.provider, symbol="EURUSD",
            direction="BUY", entry="1.0850", stop_loss="1.0800", take_profit="1.0900",
            take_profits=["1.0900"], status=PendingSignalApproval.Status.PENDING_APPROVAL,
            raw_payload={"chat_id": "-100123"})

    def _route(self, approval):
        acq, _ = AcquiredMessage.objects.get_or_create(
            provider=self.provider, message_id=approval.message_id,
            defaults=dict(chat_id="-100123", outcome=O.INTAKEN, approval=approval))
        auto_router.route_acquired_signal(
            provider=self.provider, acquired=acq, approval=approval, outcome=O.INTAKEN)


# ─────────────────────── same-user fan-out + deterministic magic ───────────────────────
class SameUserFanoutTests(_SupportBase):
    def setUp(self):
        super().setUp()
        self.acctA = self._acct("AA25")          # Account A (support@ T1 shape) — no sizing row
        self.acctB = self._acct("BB35")          # Account B — explicit 0.01
        self.asnA = self._bind(self.acctA, sizing=None)
        self.asnB = self._bind(self.acctB, sizing="0.01")

    @FANOUT
    def test_one_signal_fans_out_to_both_accounts_of_the_same_user(self):
        targets = _resolve_targets(DEMO, SOURCE)
        self.assertEqual({t.id for t in targets}, {self.asnA.id, self.asnB.id})
        # both destinations are owned by the SAME user (the support@ two-account shape)
        self.assertEqual({t.account.user_id for t in targets}, {self.support.id})

    @FANOUT
    def test_each_target_carries_its_own_deterministic_magic_in_reserved_band(self):
        for asn in (self.asnA, self.asnB):
            asn.refresh_from_db()
            self.assertEqual(asn.magic_number, ASSIGNMENT_MAGIC_BASE + asn.id)
            self.assertGreaterEqual(asn.magic_number, ASSIGNMENT_MAGIC_BASE)  # reserved 1e9 band
        self.assertNotEqual(self.asnA.magic_number, self.asnB.magic_number)   # unique per assignment

    @FANOUT
    def test_adding_account_b_never_mutates_account_a_magic(self):
        a_magic = StrategyAssignment.objects.get(id=self.asnA.id).magic_number
        extra = self._acct("CC99")
        self._bind(extra, sizing="0.01")         # add a THIRD account
        self.assertEqual(StrategyAssignment.objects.get(id=self.asnA.id).magic_number, a_magic)

    @FANOUT
    def test_effective_mode_armed_for_same_user_two_accounts(self):
        mode, reason = effective_mode(self._approval())
        self.assertEqual((mode, reason), (MODE_AUTO_DEMO, "armed"))

    def test_two_accounts_flag_off_is_manual(self):
        # Flag OFF (default) → a 2nd bound account makes the source ambiguous → MANUAL (fail-safe).
        mode, reason = effective_mode(self._approval())
        self.assertEqual(mode, MODE_MANUAL)
        self.assertEqual(reason, "no_unique_auto_assignment")


# ─────────────────────── per-account job binding + magic isolation ───────────────────────
@FANOUT
@DUALWRITE
class PerAccountBindingTests(_SupportBase):
    def setUp(self):
        super().setUp()
        self.acctA = self._acct("AA25")
        self.acctB = self._acct("BB35")
        self.asnA = self._bind(self.acctA, sizing=None)
        self.asnB = self._bind(self.acctB, sizing="0.01")

    def test_each_fanout_job_binds_to_its_own_accounts_assignment(self):
        # Prod-accurate: dual-write ON, magic-send OFF. Every PLACE_ORDER job for account A must be
        # owned by assignment A, and every job for B by assignment B — never crossed.
        appr = self._approval()
        self._route(appr)
        jobsA = ExecutionJob.objects.filter(account=self.acctA, job_type=JT.PLACE_ORDER)
        jobsB = ExecutionJob.objects.filter(account=self.acctB, job_type=JT.PLACE_ORDER)
        self.assertTrue(jobsA.exists() and jobsB.exists())
        self.assertTrue(all(j.assignment_id == self.asnA.id for j in jobsA))
        self.assertTrue(all(j.assignment_id == self.asnB.id for j in jobsB))
        # magic-send OFF ⇒ no payload magic on either destination (byte-identical to today)
        self.assertTrue(all("magic" not in (j.payload or {}) for j in list(jobsA) + list(jobsB)))

    @override_settings(STRATEGY_MAGIC_SEND_ENABLED=True)
    def test_payload_magic_is_per_account_and_never_crossed_when_armed(self):
        appr = self._approval()
        self._route(appr)
        jobsA = ExecutionJob.objects.filter(account=self.acctA, job_type=JT.PLACE_ORDER)
        jobsB = ExecutionJob.objects.filter(account=self.acctB, job_type=JT.PLACE_ORDER)
        self.assertTrue(jobsA.exists() and jobsB.exists())
        self.assertTrue(all(j.payload.get("magic") == self.asnA.magic_number for j in jobsA))
        self.assertTrue(all(j.payload.get("magic") == self.asnB.magic_number for j in jobsB))
        # cross-account leak check: A's magic never appears on B's jobs and vice-versa
        self.assertFalse(any(j.payload.get("magic") == self.asnB.magic_number for j in jobsA))
        self.assertFalse(any(j.payload.get("magic") == self.asnA.magic_number for j in jobsB))


# ─────────────────────── per-assignment sizing divergence ───────────────────────
class SizingDivergenceTests(_SupportBase):
    def setUp(self):
        super().setUp()
        self.acctA = self._acct("AA25")
        self.acctB = self._acct("BB35")
        self.asnA = self._bind(self.acctA, sizing=None)      # no row → source-global (0.04)
        self.asnB = self._bind(self.acctB, sizing="0.01")    # explicit conservative override

    def test_override_selection_is_per_assignment(self):
        # A has no row → source-global (None sentinel); B → its explicit 0.01 (un-clamped by the 0.10 cap).
        self.assertIsNone(_customer_leg_size_override(self.asnA, self.cfg))
        self.assertEqual(_customer_leg_size_override(self.asnB, self.cfg), Decimal("0.01"))

    @FANOUT
    @DUALWRITE
    def test_one_fanout_produces_divergent_leg_sizes(self):
        appr = self._approval()
        self._route(appr)
        from execution.models import SignalExecutionPlan
        planA = SignalExecutionPlan.objects.get(approval=appr, account=self.acctA)
        planB = SignalExecutionPlan.objects.get(approval=appr, account=self.acctB)
        legsA = [leg.lot_size for leg in planA.legs.all()]
        legsB = [leg.lot_size for leg in planB.legs.all()]
        self.assertTrue(legsA and legsB)
        # Account B is EXACTLY its explicit conservative 0.01 on every leg; Account A is NOT 0.01
        # (it used source-global sizing) — a single signal, two different per-account sizes.
        self.assertTrue(all(v == Decimal("0.01") for v in legsB))
        self.assertTrue(all(v != Decimal("0.01") for v in legsA))


# ─────────────────────── injection resistance ───────────────────────
@FANOUT
class InjectionResistanceTests(_SupportBase):
    def setUp(self):
        super().setUp()
        self.acctA = self._acct("AA25")
        self.asnA = self._bind(self.acctA, sizing=None)

    def test_account_not_bound_to_source_is_never_in_fanout(self):
        # A second account of the SAME user, bound to a DIFFERENT source, must never receive ti_signals.
        other = self._acct("OTHER")
        self._bind(other, source="wayond")
        self.assertEqual({t.id for t in _resolve_targets(DEMO, SOURCE)}, {self.asnA.id})

    def test_unbound_catch_all_never_receives_configured_source(self):
        # A ti_signals-CONFIGURED source (has a SignalSourceConfig row) must never fall through to an
        # UNBOUND legacy assignment — no cross-routing even if it were another user's catch-all.
        catchall = self._acct("CATCH")
        self._bind(catchall, source="")   # unbound catch-all
        self.assertEqual({t.id for t in _resolve_targets(DEMO, SOURCE)}, {self.asnA.id})


# ─────────────────────── N-account scalability (no new code for 3→5→20) ───────────────────────
@FANOUT
class ScalabilityTests(_SupportBase):
    def _bind_n(self, n):
        asns = [self._bind(self._acct(f"S{i:02d}"), sizing="0.01") for i in range(n)]
        return asns

    def test_scales_to_five_accounts_with_no_code_change(self):
        asns = self._bind_n(5)
        targets = _resolve_targets(DEMO, SOURCE)                       # SAME function as for 2
        self.assertEqual({t.id for t in targets}, {a.id for a in asns})
        magics = [a.magic_number for a in StrategyAssignment.objects.filter(id__in=[a.id for a in asns])]
        self.assertEqual(len(set(magics)), 5)                          # all distinct, deterministic

    def test_scales_to_twenty_accounts_with_no_code_change(self):
        asns = self._bind_n(20)                                        # 4× the 5-account target — headroom
        targets = _resolve_targets(DEMO, SOURCE)
        self.assertEqual(len(targets), 20)
        self.assertEqual({t.id for t in targets}, {a.id for a in asns})
        # every destination's magic is its own deterministic 1e9+id → 20 unique reserved-band magics
        self.assertEqual(len({t.magic_number for t in targets}), 20)
        self.assertTrue(all(t.magic_number == ASSIGNMENT_MAGIC_BASE + t.id for t in targets))
