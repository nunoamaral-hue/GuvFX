"""Stream D4c — the explicit human LIVE-execution authorization ceremony + revoke + material-change
invalidation + the arm-gate integration.

Invariants pinned:
  * The ceremony is the ONLY writer of a LiveExecutionAuthorization; server-enforced order (flag → LIVE →
    confirmed → connected+matched → strategy → exact acknowledgement → write).
  * A LIVE account CANNOT ARM without a valid authorization (arm gate mirrors readiness condition 11);
    DEMO arm is byte-identical flag-off vs flag-on.
  * Material change (strategy deactivated/reassigned, sizing bumped) INVALIDATES the authorization.
  * Revoke (disable live) revokes + disarms; DARK (flag off) makes the ceremony unavailable.
No real order anywhere.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution.hosted_provisioning import _arm_preconditions
from execution.live_authz import is_live_execution_authorized
from execution.models import LiveExecutionAuthorization
from execution import readiness as R
from hosted_workspace import provisioning as P
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from strategies.models import Strategy, StrategyAssignment, AssignmentLegSizing
from trading.models import TradingAccount

User = get_user_model()

# Admission on (open-access) + subsystem on. HOSTED_LIVE_EXECUTION_ENABLED is applied per-test.
BASE = dict(HOSTED_PERSISTENT_MT5_ENABLED=True, HOSTED_WORKSPACE_ONBOARDING_ENABLED=True,
            CLOSED_BETA_OPEN_ACCESS_ENABLED=True)
D4_ON = dict(BASE, HOSTED_LIVE_EXECUTION_ENABLED=True)
# For the arm gate we additionally need the subsystem execution flag (condition 2).
ARM_ON = dict(D4_ON, HOSTED_MT5_EXECUTION_ENABLED=True)
ARM_OFF = dict(BASE, HOSTED_MT5_EXECUTION_ENABLED=True)   # D4 live-exec flag OFF


def _mk(user, *, is_demo, number, confirmed=True, with_ws=True, with_strategy=True):
    acct = TradingAccount.objects.create(
        user=user, name=f"A{number}", account_number=number, broker_name="Broker",
        is_demo=is_demo, is_active=True, readiness_provider=R.PERSISTENT_WORKSPACE,
        workspace_confirmed_at=(timezone.now() if confirmed else None))
    if with_ws:
        HostedMt5Workspace.objects.create(
            trading_account=acct, proj_connected=True, proj_account_match=True,
            last_decision_at=timezone.now(), canonical_state=S.EXECUTION_READY)
    if with_strategy:
        strat = Strategy.objects.create(owner=user, name=f"S{number}")
        asn = StrategyAssignment.objects.create(
            strategy=strat, account=acct, execution_mode=StrategyAssignment.ExecutionMode.AUTO_DEMO,
            signal_source=f"src{number}", is_active=True, stage=StrategyAssignment.STAGE_LIVE)
        AssignmentLegSizing.objects.create(assignment=asn, lot_per_leg=Decimal("0.01"), version=1)
    return acct


@override_settings(**D4_ON)
class CeremonyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="cer", email="cer@x.invalid", password="x")
        self.acct = _mk(self.user, is_demo=False, number="L1")
        self.ws = self.acct.hosted_workspace

    def _authorize(self, **kw):
        kw.setdefault("acknowledgement_text", P.LIVE_ACK_TEXT)
        return P.authorize_live_execution(self.user, self.ws, request=None, **kw)

    def test_happy_path_authorizes_and_validates(self):
        res = self._authorize()
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.reason, P.LIVE_AUTHZ_OK)
        self.assertEqual(LiveExecutionAuthorization.objects.filter(trading_account=self.acct, is_active=True).count(), 1)
        self.assertTrue(is_live_execution_authorized(self.acct))

    def test_idempotent(self):
        self.assertTrue(self._authorize().ok)
        again = self._authorize()
        self.assertTrue(again.ok)
        self.assertEqual(again.reason, P.LIVE_AUTHZ_ALREADY)
        self.assertEqual(LiveExecutionAuthorization.objects.filter(trading_account=self.acct).count(), 1)

    def test_wrong_acknowledgement_rejected(self):
        res = self._authorize(acknowledgement_text="I agree")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.LIVE_AUTHZ_ACK_REQUIRED)
        self.assertFalse(is_live_execution_authorized(self.acct))

    def test_not_confirmed_rejected(self):
        self.acct.workspace_confirmed_at = None
        self.acct.save(update_fields=["workspace_confirmed_at"])
        self.assertEqual(self._authorize().reason, P.LIVE_AUTHZ_NOT_CONFIRMED)

    def test_not_connected_matched_rejected(self):
        self.ws.proj_account_match = False
        self.ws.save(update_fields=["proj_account_match"])
        self.assertEqual(self._authorize().reason, P.LIVE_AUTHZ_NOT_READY)

    def test_no_strategy_rejected(self):
        StrategyAssignment.objects.filter(account=self.acct).update(is_active=False)
        self.assertEqual(self._authorize().reason, P.LIVE_AUTHZ_NO_STRATEGY)

    @override_settings(HOSTED_LIVE_EXECUTION_ENABLED=False)   # D4 live-exec flag explicitly OFF
    def test_flag_off_not_enabled(self):
        self.assertEqual(self._authorize().reason, P.LIVE_AUTHZ_NOT_ENABLED)

    def test_demo_account_not_live(self):
        demo = _mk(self.user, is_demo=True, number="D1")
        res = P.authorize_live_execution(self.user, demo.hosted_workspace, acknowledgement_text=P.LIVE_ACK_TEXT)
        self.assertEqual(res.reason, P.LIVE_AUTHZ_NOT_LIVE)

    # ---- revoke ----
    def test_revoke_revokes_and_disarms(self):
        self.assertTrue(self._authorize().ok)
        self.ws.execution_enabled = True
        self.ws.save(update_fields=["execution_enabled"])
        res = P.revoke_live_execution(self.user, self.ws)
        self.assertTrue(res.ok)
        self.assertEqual(res.reason, P.LIVE_REVOKE_OK)
        self.assertFalse(is_live_execution_authorized(self.acct))
        self.ws.refresh_from_db()
        self.assertFalse(self.ws.execution_enabled)   # disabling live trading disarms too

    # ---- material-change invalidation ----
    def test_strategy_deactivation_invalidates(self):
        self.assertTrue(self._authorize().ok)
        self.assertTrue(is_live_execution_authorized(self.acct))
        StrategyAssignment.objects.filter(account=self.acct).update(is_active=False)
        self.assertFalse(is_live_execution_authorized(self.acct))   # material change ⇒ stale

    def test_sizing_change_invalidates(self):
        self.assertTrue(self._authorize().ok)
        self.assertTrue(is_live_execution_authorized(self.acct))
        sz = AssignmentLegSizing.objects.get(assignment__account=self.acct)
        sz.lot_per_leg = Decimal("0.02"); sz.version = 2
        sz.save(update_fields=["lot_per_leg", "version"])
        self.assertFalse(is_live_execution_authorized(self.acct))


class ArmGateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="arm", email="arm@x.invalid", password="x")

    def _authorize(self, acct):
        with override_settings(**D4_ON):
            return P.authorize_live_execution(self.user, acct.hosted_workspace,
                                              acknowledgement_text=P.LIVE_ACK_TEXT)

    def test_live_cannot_arm_without_authorization(self):
        acct = _mk(self.user, is_demo=False, number="L2")
        with override_settings(**ARM_ON):
            self.assertEqual(_arm_preconditions(acct).reason_code, R.RW_REAL_ACCOUNT_NOT_ENABLED)

    def test_live_passes_demo_wall_with_authorization(self):
        acct = _mk(self.user, is_demo=False, number="L3")
        self.assertTrue(self._authorize(acct).ok)
        with override_settings(**ARM_ON):
            reason = _arm_preconditions(acct).reason_code
        # Passed the demo/§3 wall → fails at a LATER arm precondition (route/node), NOT the live wall.
        self.assertNotEqual(reason, R.RW_REAL_ACCOUNT_NOT_ENABLED)

    def test_live_blocked_when_flag_off_even_with_authorization(self):
        acct = _mk(self.user, is_demo=False, number="L4")
        self.assertTrue(self._authorize(acct).ok)
        with override_settings(**ARM_OFF):   # D4 live-exec flag OFF
            self.assertEqual(_arm_preconditions(acct).reason_code, R.RW_REAL_ACCOUNT_NOT_ENABLED)

    def test_demo_arm_reason_identical_flag_off_and_on(self):
        acct = _mk(self.user, is_demo=True, number="D2")
        with override_settings(**ARM_OFF):
            off = _arm_preconditions(acct).reason_code
        with override_settings(**ARM_ON):
            on = _arm_preconditions(acct).reason_code
        self.assertEqual(off, on)   # DEMO equivalence: the §3 branch never runs for a demo account
