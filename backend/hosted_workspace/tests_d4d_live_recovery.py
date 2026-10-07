"""Stream D4d — LIVE recovery semantics (DARK). Recovery restores INFRASTRUCTURE only; it NEVER creates a
LiveExecutionAuthorization and NEVER sets execution_enabled/execution_authorized_at. The shared
``_armed_and_matched`` candidate gate (used by BOTH liveness_recovery and session_reconciler) widens from
demo-only to (demo OR clean-LIVE-with-a-valid-§3-authorization) only when the D4d flag is on; DEMO + flag-off
are byte-identical.

Invariants pinned:
  * DEMO armed+matched ⇒ candidate regardless of the flag (byte-identical).
  * LIVE armed+matched ⇒ candidate ONLY with the flag on AND a currently-valid §3 authorization. A LIVE
    candidate must still be ARMED (which for a LIVE account required the §3 ceremony). A demo/live MISMATCH, an
    unapproved, a revoked, or a materially-invalidated LIVE account is fail-closed (defense-in-depth: the gate
    re-derives ``is_live_execution_authorized`` exactly as readiness condition-11 / the arm gate / the bridge do).
  * Re-arm authority is untouched: being a recovery candidate restores the terminal/session, never execution.
  * capability_recovery is deliberately NOT widened to LIVE (it writes AllowLiveTrading=1 — Amber/Sponsor-gated).
"""
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from execution import readiness as R
from hosted_workspace.liveness_recovery import _armed_and_matched
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from trading.models import BrokerServer

ON = {"HOSTED_LIVE_MONITORING_ENABLED": "1"}


def _ws():
    return SimpleNamespace(execution_enabled=True, execution_authorized_at=timezone.now(), proj_account_match=True)


def _acct(is_demo, broker_server=None):
    return SimpleNamespace(is_demo=is_demo, workspace_confirmed_at=timezone.now(), broker_server=broker_server)


class ArmedAndMatchedGateTests(SimpleTestCase):
    """DB-free: DEMO equivalence + the structural fail-closed negatives. A LIVE account can only be *admitted*
    with a real §3 authorization, which is a DB fact — so here every LIVE case that reaches the authz check
    fail-closes (no DB ⇒ ``_is_live_authorized`` returns False); the admitted-LIVE positive is proven in
    ``LiveAuthorizedRecoveryTests`` below."""

    def test_demo_candidate_regardless_of_flag(self):
        ws, acct = _ws(), _acct(is_demo=True)
        self.assertTrue(_armed_and_matched(ws, acct))                 # flag off
        with override_settings(**ON):
            self.assertTrue(_armed_and_matched(ws, acct))             # flag on — demo unchanged (byte-identical)

    def test_live_not_candidate_when_flag_off(self):
        ws, acct = _ws(), _acct(is_demo=False)                        # live (no server ⇒ is_live_environment True)
        self.assertFalse(_armed_and_matched(ws, acct))               # flag off ⇒ demo-only ⇒ not a candidate

    def test_live_not_candidate_without_authorization(self):
        # Flag ON + clean-LIVE + armed + matched + confirmed, but NO valid §3 authorization ⇒ fail-closed.
        ws, acct = _ws(), _acct(is_demo=False)
        with override_settings(**ON):
            self.assertFalse(_armed_and_matched(ws, acct))

    @override_settings(**ON)
    def test_live_must_be_armed(self):
        acct = _acct(is_demo=False)
        ws = _ws(); ws.execution_enabled = False                     # not armed (never §3-authorized/armed)
        self.assertFalse(_armed_and_matched(ws, acct))
        ws = _ws(); ws.execution_authorized_at = None
        self.assertFalse(_armed_and_matched(ws, acct))

    @override_settings(**ON)
    def test_live_must_be_matched_and_confirmed(self):
        acct = _acct(is_demo=False)
        ws = _ws(); ws.proj_account_match = False
        self.assertFalse(_armed_and_matched(ws, acct))
        acct2 = _acct(is_demo=False); acct2.workspace_confirmed_at = None
        self.assertFalse(_armed_and_matched(_ws(), acct2))

    @override_settings(**ON)
    def test_demo_live_mismatch_fail_closed(self):
        # is_demo=False but a DEMO broker server = integrity mismatch ⇒ is_live_environment False ⇒ not a live candidate.
        acct = _acct(is_demo=False, broker_server=BrokerServer(server_name="x", environment=BrokerServer.DEMO))
        self.assertFalse(_armed_and_matched(_ws(), acct))

    def test_recovery_predicate_is_pure_read(self):
        # The candidate predicate must not mutate the account/workspace (recovery restores infra elsewhere).
        ws, acct = _ws(), _acct(is_demo=True)
        before = (ws.execution_enabled, ws.execution_authorized_at)
        with override_settings(**ON):
            _armed_and_matched(ws, acct)
        self.assertEqual((ws.execution_enabled, ws.execution_authorized_at), before)


# Admission on (open-access) + subsystem on; the §3 ceremony needs these to WRITE an authorization.
D4_ON = dict(HOSTED_PERSISTENT_MT5_ENABLED=True, HOSTED_WORKSPACE_ONBOARDING_ENABLED=True,
             CLOSED_BETA_OPEN_ACCESS_ENABLED=True, HOSTED_LIVE_EXECUTION_ENABLED=True)


@override_settings(**D4_ON)
class LiveAuthorizedRecoveryTests(TestCase):
    """DB-backed: proves a legitimately-authorized LIVE account IS still recoverable (the hardening must not
    break real LIVE recovery), and that losing a valid authorization (material change / revoke) fail-closes it."""

    def setUp(self):
        from hosted_workspace import provisioning as P
        from strategies.models import Strategy, StrategyAssignment, AssignmentLegSizing
        from trading.models import TradingAccount
        User = get_user_model()
        self.P = P
        self.user = User.objects.create_user(username="d4dlr", email="d4dlr@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="LR", account_number="770001", broker_name="Broker",
            is_demo=False, is_active=True, readiness_provider=R.PERSISTENT_WORKSPACE,
            workspace_confirmed_at=timezone.now())
        self.ws = HostedMt5Workspace.objects.create(
            trading_account=self.acct, proj_connected=True, proj_account_match=True,
            last_decision_at=timezone.now(), canonical_state=S.EXECUTION_READY,
            execution_enabled=True, execution_authorized_at=timezone.now())   # ARMED
        strat = Strategy.objects.create(owner=self.user, name="S-LR")
        self.asn = StrategyAssignment.objects.create(
            strategy=strat, account=self.acct, execution_mode=StrategyAssignment.ExecutionMode.AUTO_DEMO,
            signal_source="src-lr", is_active=True, stage=StrategyAssignment.STAGE_LIVE)
        AssignmentLegSizing.objects.create(assignment=self.asn, lot_per_leg=Decimal("0.01"), version=1)
        # The one legitimate §3 authorization for this account/identity/strategy/sizing.
        res = P.authorize_live_execution(self.user, self.ws, acknowledgement_text=P.LIVE_ACK_TEXT)
        self.assertTrue(res.ok, res.reason)

    def _acct_fresh(self):
        self.acct.refresh_from_db()
        return self.acct

    def test_authorized_live_is_candidate_only_with_flag_on(self):
        # Flag OFF ⇒ demo-only ⇒ not a candidate even though fully authorized + armed.
        self.assertFalse(_armed_and_matched(self.ws, self._acct_fresh()))
        with override_settings(**ON):
            self.assertTrue(_armed_and_matched(self.ws, self._acct_fresh()))

    @override_settings(**ON)
    def test_materially_invalidated_live_not_candidate(self):
        from strategies.models import StrategyAssignment
        self.assertTrue(_armed_and_matched(self.ws, self._acct_fresh()))
        # A material change (strategy deactivated) invalidates the authorization ⇒ recovery fail-closed, even
        # though the durable arm bits are still set.
        StrategyAssignment.objects.filter(account=self.acct).update(is_active=False)
        self.assertFalse(_armed_and_matched(self.ws, self._acct_fresh()))

    @override_settings(**ON)
    def test_revoked_live_not_candidate(self):
        self.assertTrue(_armed_and_matched(self.ws, self._acct_fresh()))
        self.P.revoke_live_execution(self.user, self.ws)             # disable-live: revokes + disarms
        self.ws.refresh_from_db()
        # Revoke both invalidates the authz AND disarms (execution_enabled False) — fail-closed on both counts.
        self.assertFalse(_armed_and_matched(self.ws, self._acct_fresh()))


class CapabilityRecoveryNotWidenedTests(SimpleTestCase):
    def test_capability_recovery_stays_demo_only(self):
        # capability_recovery writes AllowLiveTrading=1 to the terminal — NOT pure infrastructure — so D4d
        # deliberately does NOT widen it to LIVE (Amber; Sponsor-gated separately). It must reference none of
        # the LIVE-recovery gate helpers.
        import inspect
        import hosted_workspace.capability_recovery as cap
        src = inspect.getsource(cap)
        self.assertNotIn("is_live_environment", src)
        self.assertNotIn("hosted_live_monitoring_enabled", src)
        self.assertNotIn("_live_recovery_enabled", src)
