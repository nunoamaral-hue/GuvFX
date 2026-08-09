"""ADR-0034 Onboarding — journey projection + degrade-closed delivery + strategy-assignment eligibility.

Proves the customer-safe read models: the journey phase machine walks NO_WORKSPACE → … → WORKSPACE_READY
deterministically; delivery-readiness degrades CLOSED (never fabricates a RemoteApp URL); the login is masked
and no secret leaks; and strategy-assignment eligibility keeps the three tiers strictly separated
(assignment < armed < order-authorised) with a reachable checklist.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from billing.models import UserSubscriptionState
from execution.models import TerminalNode
from execution.readiness import PERSISTENT_WORKSPACE
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import eligibility as EL
from hosted_workspace import onboarding_read_model as RM
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S, WorkspaceReason

U = get_user_model()


def _user(name="u1", *, entitled=True):
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.update_or_create(
        user=u, defaults=dict(current_plan=("beta" if entitled else "starter_trial"),
                              plan_status="active", viewer_mode=False))
    return u


def _ws(user, *, node=False, state=S.PROVISIONING, connected=None, matched=None,
        confirmed=False, login="700900", execution_enabled=False):
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    acct = TradingAccount.objects.create(user=user, name="B", broker_name="B", account_number=login,
                                         is_demo=True, broker_server=srv,
                                         readiness_provider=PERSISTENT_WORKSPACE, is_active=False)
    if confirmed:
        acct.workspace_confirmed_at = timezone.now()
        acct.save(update_fields=["workspace_confirmed_at"])
    tn = TerminalNode.objects.create(hostname=f"n-{user.pk}", status=TerminalNode.Status.ACTIVE) if node else None
    ws = HostedMt5Workspace.objects.create(
        trading_account=acct, canonical_state=state, proj_connected=connected,
        proj_account_match=matched, execution_enabled=execution_enabled,
        currently_attached_login=(login if connected else ""))
    if tn is not None:
        ws.execution_node = tn
        ws.save(update_fields=["execution_node"])
    return ws, acct


class JourneyPhaseTests(TestCase):
    def test_no_workspace(self):
        p = RM.onboarding_journey_projection(None, None)
        self.assertEqual(p["phase"], RM.PHASE_NO_WORKSPACE)
        self.assertEqual(p["next_action"], RM.NEXT_REQUEST_WORKSPACE)

    def test_requested_but_no_node(self):
        ws, acct = _ws(_user(), node=False, state=S.PROVISIONING)
        p = RM.onboarding_journey_projection(ws, acct)
        self.assertEqual(p["phase"], RM.PHASE_WORKSPACE_PREPARING)
        self.assertEqual(p["next_action"], RM.NEXT_WAIT)

    def test_awaiting_login(self):
        ws, acct = _ws(_user(), node=True, state=S.WAITING_FOR_LOGIN)
        p = RM.onboarding_journey_projection(ws, acct)
        self.assertEqual(p["phase"], RM.PHASE_AWAITING_BROKER_LOGIN)
        self.assertEqual(p["next_action"], RM.NEXT_OPEN_MT5_AND_LOGIN)

    def test_connected_not_matched(self):
        ws, acct = _ws(_user(), node=True, state=S.CONNECTED, connected=True, matched=False)
        p = RM.onboarding_journey_projection(ws, acct)
        self.assertEqual(p["phase"], RM.PHASE_BROKER_CONNECTED)

    def test_confirmation_required(self):
        ws, acct = _ws(_user(), node=True, state=S.CONNECTED, connected=True, matched=True, confirmed=False)
        p = RM.onboarding_journey_projection(ws, acct)
        self.assertEqual(p["phase"], RM.PHASE_ACCOUNT_CONFIRMATION_REQUIRED)
        self.assertEqual(p["next_action"], RM.NEXT_CONFIRM_ACCOUNT)

    def test_account_bound_connected_confirmed(self):
        ws, acct = _ws(_user(), node=True, state=S.CONNECTED, connected=True, matched=True, confirmed=True)
        p = RM.onboarding_journey_projection(ws, acct)
        self.assertEqual(p["phase"], RM.PHASE_ACCOUNT_BOUND)

    def test_workspace_ready(self):
        ws, acct = _ws(_user(), node=True, state=S.EXECUTION_READY, connected=True, matched=True, confirmed=True)
        p = RM.onboarding_journey_projection(ws, acct)
        self.assertEqual(p["phase"], RM.PHASE_WORKSPACE_READY)
        self.assertEqual(p["next_action"], RM.NEXT_ASSIGN_STRATEGY)
        self.assertTrue(p["strategy_eligible"])

    def test_degraded_state_unavailable(self):
        ws, acct = _ws(_user(), node=True, state=S.DISCONNECTED, connected=False)
        p = RM.onboarding_journey_projection(ws, acct)
        self.assertEqual(p["phase"], RM.PHASE_WORKSPACE_UNAVAILABLE)
        self.assertEqual(p["next_action"], RM.NEXT_CONTACT_SUPPORT)

    def test_active_account_mismatch_is_switchable_not_support(self):
        # The certified writer maps connected-but-mismatched to SUSPENDED/ACCOUNT_MISMATCH. The journey must
        # guide the customer to SWITCH their active account (recoverable), NOT collapse to "contact support".
        ws, acct = _ws(_user(), node=True, state=S.SUSPENDED, connected=True, matched=False)
        ws.canonical_reason = WorkspaceReason.ACCOUNT_MISMATCH
        ws.save(update_fields=["canonical_reason"])
        p = RM.onboarding_journey_projection(ws, acct)
        self.assertEqual(p["phase"], RM.PHASE_BROKER_CONNECTED)
        self.assertEqual(p["next_action"], RM.NEXT_OPEN_MT5_AND_LOGIN)

    def test_suspended_without_mismatch_is_still_unavailable(self):
        # A SUSPENDED for any OTHER reason is still the generic degraded bucket (contact support).
        ws, acct = _ws(_user(), node=True, state=S.SUSPENDED, connected=False)
        p = RM.onboarding_journey_projection(ws, acct)
        self.assertEqual(p["phase"], RM.PHASE_WORKSPACE_UNAVAILABLE)


class CustomerSafeTests(TestCase):
    def test_login_is_masked_and_no_full_login_leaks(self):
        ws, acct = _ws(_user(), node=True, state=S.CONNECTED, connected=True, matched=True, login="7009999")
        p = RM.onboarding_journey_projection(ws, acct, staff=False)
        self.assertEqual(p["active_login_masked"], "***999")
        self.assertNotIn("7009999", str(p))          # the full login never appears
        self.assertNotIn("_staff", p)                 # non-staff never receives operator context

    def test_staff_gets_context_but_no_secret(self):
        ws, acct = _ws(_user(), node=True, state=S.CONNECTED, connected=True, matched=True)
        p = RM.onboarding_journey_projection(ws, acct, staff=True)
        self.assertIn("_staff", p)
        self.assertEqual(p["_staff"]["canonical_state"], str(S.CONNECTED))
        self.assertNotIn("password", str(p).lower())


class DeliveryReadinessTests(TestCase):
    def test_none_workspace_not_available(self):
        self.assertEqual(RM.delivery_readiness(None), RM.DELIVERY_NOT_AVAILABLE)

    def test_flag_off_not_available(self):
        ws, _ = _ws(_user(), node=True, state=S.CONNECTED, connected=True)
        self.assertEqual(RM.delivery_readiness(ws), RM.DELIVERY_NOT_AVAILABLE)   # remoteapp flag OFF (default)

    @override_settings(HOSTED_MT5_REMOTEAPP_ENABLED="1")
    def test_flag_on_undelivered_is_external_gate(self):
        # Reconciled with merged #316: a flagged workspace whose delivery_state is NONE (not delivered) is
        # EXTERNAL_GATE — the RemoteApp host (RDS) is the un-begun Sponsor/host gate. NEVER fabricates READY.
        ws, _ = _ws(_user(), node=True, state=S.EXECUTION_READY, connected=True, matched=True, confirmed=True)
        self.assertEqual(str(ws.delivery_state), "NONE")
        self.assertEqual(RM.delivery_readiness(ws), RM.DELIVERY_EXTERNAL_GATE)

    @override_settings(HOSTED_MT5_REMOTEAPP_ENABLED="1")
    def test_connected_delivery_is_ready(self):
        # Only a genuinely CONNECTED RemoteApp is READY (the real delivery_state, owned by the delivery writer).
        ws, _ = _ws(_user(), node=True, state=S.EXECUTION_READY, connected=True, matched=True, confirmed=True)
        ws.delivery_state = HostedMt5Workspace.DeliveryState.CONNECTED
        ws.save(update_fields=["delivery_state"])
        self.assertEqual(RM.delivery_readiness(ws), RM.DELIVERY_READY)

    @override_settings(HOSTED_MT5_REMOTEAPP_ENABLED="1")
    def test_authorized_or_disconnected_delivery_is_preparing(self):
        ws, _ = _ws(_user(), node=True, state=S.EXECUTION_READY, connected=True, matched=True, confirmed=True)
        for state in (HostedMt5Workspace.DeliveryState.AUTHORIZED, HostedMt5Workspace.DeliveryState.DISCONNECTED):
            ws.delivery_state = state
            ws.save(update_fields=["delivery_state"])
            self.assertEqual(RM.delivery_readiness(ws), RM.DELIVERY_PREPARING, state)


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1")
class EligibilityTierTests(TestCase):
    def test_not_eligible_lists_first_unmet(self):
        ws, acct = _ws(_user(), node=True, state=S.CONNECTED, connected=True, matched=True, confirmed=False)
        e = EL.strategy_assignment_eligibility(acct)
        self.assertEqual(e["state"], EL.STATE_NOT_ELIGIBLE)
        self.assertFalse(e["assignment_eligible"])
        self.assertEqual(e["next_action"], RM.NEXT_CONFIRM_ACCOUNT)   # confirm is the first unmet check
        self.assertFalse(e["armed"])

    def test_assignment_eligible_but_not_armed(self):
        # confirmed + connected + matched, but execution NOT armed (execution_enabled False): eligible to
        # ASSIGN, strictly below ARMED. This is the core tier-separation guard.
        ws, acct = _ws(_user(), node=True, state=S.CONNECTED, connected=True, matched=True, confirmed=True,
                       execution_enabled=False)
        e = EL.strategy_assignment_eligibility(acct)
        self.assertEqual(e["state"], EL.STATE_ASSIGNMENT_ELIGIBLE)
        self.assertTrue(e["assignment_eligible"])
        self.assertFalse(e["armed"])
        self.assertEqual(e["next_action"], RM.NEXT_ASSIGN_STRATEGY)
        self.assertEqual(e["order_authorisation"], EL.ORDER_AUTHORISATION_EXTERNAL)  # tier 3 never asserted

    def test_armed_requires_execution_enabled_and_ready(self):
        ws, acct = _ws(_user(), node=True, state=S.EXECUTION_READY, connected=True, matched=True,
                       confirmed=True, execution_enabled=True)
        e = EL.strategy_assignment_eligibility(acct)
        self.assertEqual(e["state"], EL.STATE_ARMED)
        self.assertTrue(e["armed"])
        # armed is strictly above assignment — assignment_eligible remains True too
        self.assertTrue(e["assignment_eligible"])

    def test_subsystem_off_never_eligible(self):
        ws, acct = _ws(_user(), node=True, state=S.EXECUTION_READY, connected=True, matched=True, confirmed=True,
                       execution_enabled=True)
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED="0"):
            e = EL.strategy_assignment_eligibility(acct)
        self.assertEqual(e["state"], EL.STATE_NOT_ELIGIBLE)
        self.assertFalse(e["assignment_eligible"])
        self.assertFalse(e["armed"])
