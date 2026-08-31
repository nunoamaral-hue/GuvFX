"""ADR-0034 Onboarding — journey projection + degrade-closed delivery + strategy-assignment eligibility.

Proves the customer-safe read models: the journey phase machine walks NO_WORKSPACE → … → WORKSPACE_READY
deterministically; delivery-readiness degrades CLOSED (never fabricates a RemoteApp URL); the login is masked
and no secret leaks; and strategy-assignment eligibility keeps the three tiers strictly separated
(assignment < armed < order-authorised) with a reachable checklist.
"""
from unittest.mock import patch

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
        # currently_attached_login has NO production writer (always "" in prod). Keep it empty here so the
        # broker-account display tests prove the value comes from the matched bound identity, not this dead field.
        currently_attached_login="")
    if tn is not None:
        ws.execution_node = tn
        ws.save(update_fields=["execution_node"])
    return ws, acct


class JourneyPhaseTests(TestCase):
    def setUp(self):
        # These tests assert the CUSTOMER phase machine on ordinary accounts. Test-DB accounts can receive pk 1,
        # which production treats as Customer Zero (→ operator projection). Pin the CZ set empty here so the
        # customer-journey assertions test what they mean regardless of the auto-assigned pk. The dedicated
        # OperatorAccountProjectionTests exercises the CZ override explicitly.
        p = patch("hosted_workspace.tenant_isolation.customer_zero_account_ids", return_value=frozenset())
        p.start(); self.addCleanup(p.stop)

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

    def test_onboarding_completes_at_connected_matched_confirmed_not_execution_ready(self):
        # AJ#3 product correction: onboarding is COMPLETE when the workspace is OPERATIONAL — CONNECTED + account
        # matched + confirmed — WITHOUT requiring canonical EXECUTION_READY (AutoTrading/arming). The retired
        # ACCOUNT_BOUND "Finishing up" wait (which depended on host-observed trade_allowed and could hang
        # indefinitely) is gone; the customer reaches WORKSPACE_READY and may choose a strategy.
        ws, acct = _ws(_user(), node=True, state=S.CONNECTED, connected=True, matched=True, confirmed=True)
        p = RM.onboarding_journey_projection(ws, acct)
        self.assertEqual(p["phase"], RM.PHASE_WORKSPACE_READY)       # NOT ACCOUNT_BOUND
        self.assertEqual(p["next_action"], RM.NEXT_ASSIGN_STRATEGY)
        self.assertTrue(p["strategy_eligible"])                     # onboarding-complete signal (not arming)
        self.assertNotEqual(p["phase"], RM.PHASE_ACCOUNT_BOUND)     # the indefinite "Finishing up" state is retired

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

    def test_identity_declared_true_once_login_recorded(self):
        # The write-once bind records the expected login on trading_account.account_number, so a non-empty
        # account number IS "identity declared" — the UI's single source of truth for form-vs-waiting.
        ws, acct = _ws(_user(), node=True, state=S.WAITING_FOR_LOGIN, login="700900")
        p = RM.onboarding_journey_projection(ws, acct)
        self.assertTrue(p["identity_declared"])

    def test_identity_declared_false_before_bind(self):
        # Deferred bind: a provisioned workspace whose identity is not yet declared (empty account number).
        # Determined purely from server state → deterministic across reloads/devices, no client session flag.
        ws, acct = _ws(_user(), node=True, state=S.WAITING_FOR_LOGIN, login="")
        p = RM.onboarding_journey_projection(ws, acct)
        self.assertFalse(p["identity_declared"])

    def test_identity_declared_false_when_no_workspace(self):
        p = RM.onboarding_journey_projection(None, None)
        self.assertFalse(p["identity_declared"])


class CustomerSafeTests(TestCase):
    def setUp(self):
        p = patch("hosted_workspace.tenant_isolation.customer_zero_account_ids", return_value=frozenset())
        p.start(); self.addCleanup(p.stop)

    def test_login_is_masked_and_no_full_login_leaks(self):
        ws, acct = _ws(_user(), node=True, state=S.CONNECTED, connected=True, matched=True, login="7009999")
        p = RM.onboarding_journey_projection(ws, acct, staff=False)
        self.assertEqual(p["active_login_masked"], "***999")   # derived from the matched account_number
        self.assertEqual(p["active_server"], "IS6-Demo")        # matched broker server name
        self.assertNotIn("7009999", str(p))          # the full login never appears
        self.assertNotIn("_staff", p)                 # non-staff never receives operator context

    def test_broker_account_empty_until_matched(self):
        # An account can be bound (account_number set) + connected but NOT yet matched -> the broker-account
        # display must stay empty ("Not yet"), never a wrong/unverified account. Guards the matched-gate.
        ws, acct = _ws(_user(), node=True, state=S.CONNECTED, connected=True, matched=False, login="7001234")
        p = RM.onboarding_journey_projection(ws, acct, staff=False)
        self.assertEqual(p["active_login_masked"], "")
        self.assertEqual(p["active_server"], "")

    def test_broker_account_shows_server_and_masked_login_when_matched(self):
        ws, acct = _ws(_user(), node=True, state=S.EXECUTION_READY, connected=True, matched=True,
                       confirmed=True, login="62139344")
        p = RM.onboarding_journey_projection(ws, acct, staff=False)
        self.assertEqual(p["active_login_masked"], "***344")
        self.assertEqual(p["active_server"], "IS6-Demo")
        self.assertNotIn("62139344", str(p))          # full number never leaves the backend

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


class OperatorAccountProjectionTests(TestCase):
    """A reserved Customer-Zero / operator account must NOT be shown the customer onboarding journey as
    permanently incomplete. Its workspace legitimately sits at WAITING_FOR_LOGIN forever (legacy operator,
    never bound by the per-tenant observer); the projection returns OPERATOR-READY instead — a projection-only
    correction that mutates no durable state. Non-CZ customers are unaffected."""

    def _cz(self, acct_id):
        return patch("hosted_workspace.tenant_isolation.customer_zero_account_ids",
                     return_value=frozenset({acct_id}))

    def test_customer_zero_stuck_workspace_projects_operator_ready(self):
        ws, acct = _ws(_user("cz"), node=True, state=S.WAITING_FOR_LOGIN)   # the exact "stuck" prod shape
        with self._cz(acct.id):
            p = RM.onboarding_journey_projection(ws, acct)
        self.assertTrue(p["operator_account"])
        self.assertEqual(p["phase"], RM.PHASE_WORKSPACE_READY)              # NOT AWAITING_BROKER_LOGIN
        self.assertEqual(p["next_action"], RM.NEXT_ASSIGN_STRATEGY)
        self.assertTrue(p["strategy_eligible"])

    def test_operator_projection_mutates_no_durable_state(self):
        ws, acct = _ws(_user("cz2"), node=True, state=S.WAITING_FOR_LOGIN)
        with self._cz(acct.id):
            RM.onboarding_journey_projection(ws, acct)
        ws.refresh_from_db(); acct.refresh_from_db()
        self.assertEqual(ws.canonical_state, S.WAITING_FOR_LOGIN)           # untouched
        self.assertIsNone(acct.workspace_confirmed_at)                      # never fabricated

    def test_non_cz_customer_same_state_is_unaffected(self):
        ws, acct = _ws(_user("cust"), node=True, state=S.WAITING_FOR_LOGIN)
        with patch("hosted_workspace.tenant_isolation.customer_zero_account_ids",
                   return_value=frozenset({acct.id + 10_000})):            # some OTHER account is CZ
            p = RM.onboarding_journey_projection(ws, acct)
        self.assertFalse(p["operator_account"])
        self.assertEqual(p["phase"], RM.PHASE_AWAITING_BROKER_LOGIN)        # ordinary customer journey preserved
        self.assertEqual(p["next_action"], RM.NEXT_OPEN_MT5_AND_LOGIN)

    def test_fresh_beta_customer_ready_path_unchanged(self):
        # A genuinely operational NON-CZ customer still reaches WORKSPACE_READY via the real gates (not the
        # operator override): operator_account stays False.
        ws, acct = _ws(_user("beta"), node=True, state=S.CONNECTED, connected=True, matched=True, confirmed=True)
        with patch("hosted_workspace.tenant_isolation.customer_zero_account_ids",
                   return_value=frozenset({acct.id + 10_000})):
            p = RM.onboarding_journey_projection(ws, acct)
        self.assertFalse(p["operator_account"])
        self.assertEqual(p["phase"], RM.PHASE_WORKSPACE_READY)
