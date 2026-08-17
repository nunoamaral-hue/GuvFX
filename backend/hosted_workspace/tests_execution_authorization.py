"""ADR-0047 — explicit customer authorization to execute (supersedes ADR-0044 Decision 2).

Proves the load-bearing invariant from every angle: MT5 automation CAPABILITY (trade_allowed / EXECUTION_READY)
is NOT customer AUTHORIZATION. Reaching EXECUTION_READY can NEVER arm a hosted workspace; only the customer's
EXPLICIT ``authorize_workspace_execution`` (the "Enable automated trading" click) may. Covered:

  * arm chokepoint  — ``_arm_preconditions`` / ``arm_hosted_workspace_execution`` refuse while unauthorized
                       (binds BOTH the autonomous runner and the operator command).
  * auto_arm_runner — an EXECUTION_READY, unarmed, UNAUTHORIZED workspace is not even a candidate.
  * readiness       — belt-and-braces: a legacy execution_enabled=True but authorized_at=NULL row is denied.
  * authorize fn    — owner-scoped, requires confirmed + EXECUTION_READY + matched, idempotent, then arms.
  * endpoint        — 404 while dark; 409 until ready; 200 arms; owner-scoped.
  * Provider A / CZ — the TemporaryValidationProvider path is byte-unchanged (no authorization term applies).

None of these places an order — the live bridge gate remains the sole order authority.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from billing.models import BetaTester
from execution import hosted_provisioning as HP
from execution import readiness as R
from execution.models import TerminalNode
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import provisioning as P
from hosted_workspace.auto_arm_runner import run_hosted_auto_arm
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()
_n = 0

# Master + onboarding + execution flags on; admission via BetaTester. Supervised posture flag deliberately OFF
# so the posture gate is a no-op (True) — the posture is exercised by its own suite; here we isolate authz.
FLAGS = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_WORKSPACE_ONBOARDING_ENABLED="1",
             HOSTED_MT5_EXECUTION_ENABLED="1", BETA_MAX_TESTERS=1000)
AUTHORIZE = "/api/hosted-workspace/onboarding/authorize-execution/"


def _uniq():
    global _n
    _n += 1
    return f"93{_n:04d}"


def _account(*, provider=None, is_demo=True, admit=True):
    login = _uniq()
    user = U.objects.create_user(username=f"az{login}", email=f"{login}@x.invalid", password="x")
    if admit:
        BetaTester.objects.create(email=user.email, is_active=True)
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    node = TerminalNode.objects.create(hostname=f"n-{login}", status=TerminalNode.Status.ACTIVE)
    acct = TradingAccount.objects.create(
        user=user, name="a", broker_name="B", account_number=login, is_demo=is_demo, is_active=True,
        broker_server=srv, readiness_provider=(provider or R.PERSISTENT_WORKSPACE), terminal_node=node)
    return user, acct


def _ready_ws(acct, *, authorized, confirmed=True, **kw):
    """A fully connected + matched + EXECUTION_READY workspace, bound to the account's node. ``authorized``
    stamps the durable customer authorization; ``confirmed`` stamps the account's identity ACK."""
    base = dict(canonical_state=S.EXECUTION_READY, proj_connected=True, proj_trade_allowed=True,
                proj_account_match=True, proj_execution_ready=True, last_decision_at=timezone.now(),
                execution_node=acct.terminal_node)
    if authorized:
        base["execution_authorized_at"] = timezone.now()
    base.update(kw)
    ws = HostedMt5Workspace.objects.create(trading_account=acct, **base)
    if confirmed and acct.workspace_confirmed_at is None:
        acct.workspace_confirmed_at = timezone.now()
        acct.save(update_fields=["workspace_confirmed_at"])
    return ws


@override_settings(**FLAGS)
class ArmChokepointTests(TestCase):
    """The shared ``_arm_preconditions`` gate — the single chokepoint for the runner AND the operator."""

    def test_arm_refused_while_unauthorized(self):
        _user, acct = _account()
        _ready_ws(acct, authorized=False)
        res = HP.arm_hosted_workspace_execution(acct)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason_code, HP.ARM_NOT_AUTHORIZED)
        acct.hosted_workspace.refresh_from_db()
        self.assertFalse(acct.hosted_workspace.execution_enabled)   # nothing mutated on refusal

    def test_arm_succeeds_once_authorized(self):
        _user, acct = _account()
        _ready_ws(acct, authorized=True)
        res = HP.arm_hosted_workspace_execution(acct)
        self.assertTrue(res.ok, res.reason_code)
        self.assertEqual(res.reason_code, HP.ARM_OK)
        acct.hosted_workspace.refresh_from_db()
        self.assertTrue(acct.hosted_workspace.execution_enabled)

    def test_precondition_reason_is_authz_when_everything_else_holds(self):
        # Authorization is the ONLY thing missing → the reason must be the authz one (not a stale earlier code).
        _user, acct = _account()
        _ready_ws(acct, authorized=False)
        self.assertEqual(HP._arm_preconditions(acct).reason_code, HP.ARM_NOT_AUTHORIZED)


@override_settings(**FLAGS)
class AutoArmAuthorizationTests(TestCase):
    def test_ready_unauthorized_is_not_a_candidate(self):
        _user, acct = _account()
        _ready_ws(acct, authorized=False)
        summary = run_hosted_auto_arm()
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["candidates"], 0, summary)         # unauthorized ⇒ not even a candidate
        self.assertEqual(summary["armed"], 0, summary)
        acct.hosted_workspace.refresh_from_db()
        self.assertFalse(acct.hosted_workspace.execution_enabled)

    def test_ready_authorized_is_armed(self):
        _user, acct = _account()
        _ready_ws(acct, authorized=True)
        summary = run_hosted_auto_arm()
        self.assertEqual(summary["candidates"], 1, summary)
        self.assertEqual(summary["armed"], 1, summary)
        acct.hosted_workspace.refresh_from_db()
        self.assertTrue(acct.hosted_workspace.execution_enabled)


@override_settings(**FLAGS)
class ReadinessBeltAndBracesTests(TestCase):
    def test_legacy_armed_but_unauthorized_row_denied_at_order_gate(self):
        # A row armed autonomously BEFORE the correction: execution_enabled=True yet authorized_at=NULL.
        _user, acct = _account()
        _ready_ws(acct, authorized=False, execution_enabled=True)
        dec = R.PersistentWorkspaceProvider().evaluate(acct)
        self.assertFalse(dec.eligible)
        self.assertEqual(dec.reason_code, R.RW_EXECUTION_NOT_AUTHORIZED)

    def test_armed_and_authorized_row_is_eligible(self):
        _user, acct = _account()
        _ready_ws(acct, authorized=True, execution_enabled=True)
        dec = R.PersistentWorkspaceProvider().evaluate(acct)
        self.assertTrue(dec.eligible, dec.reason_code)


@override_settings(**FLAGS)
class AuthorizeWorkspaceExecutionTests(TestCase):
    def test_happy_path_records_authz_and_arms(self):
        user, acct = _account()
        ws = _ready_ws(acct, authorized=False)
        res = P.authorize_workspace_execution(user, ws)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.reason, P.AUTHZ_OK)
        self.assertEqual(res.arm_reason, HP.ARM_OK)
        ws.refresh_from_db()
        self.assertIsNotNone(ws.execution_authorized_at)            # durable authorization recorded
        self.assertTrue(ws.execution_enabled)                       # armed via the certified path

    def test_idempotent_second_authorization(self):
        user, acct = _account()
        ws = _ready_ws(acct, authorized=False)
        P.authorize_workspace_execution(user, ws)
        first = ws.__class__.objects.get(pk=ws.pk).execution_authorized_at
        res2 = P.authorize_workspace_execution(user, ws)
        self.assertTrue(res2.ok)
        self.assertEqual(res2.reason, P.AUTHZ_ALREADY)
        self.assertEqual(ws.__class__.objects.get(pk=ws.pk).execution_authorized_at, first)  # unchanged

    def test_refused_when_not_confirmed(self):
        user, acct = _account()
        ws = _ready_ws(acct, authorized=False, confirmed=False)     # identity not ACKed
        res = P.authorize_workspace_execution(user, ws)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.AUTHZ_NOT_CONFIRMED)
        self.assertIsNone(ws.__class__.objects.get(pk=ws.pk).execution_authorized_at)

    def test_refused_when_not_execution_ready(self):
        user, acct = _account()
        ws = _ready_ws(acct, authorized=False, canonical_state=S.CONNECTED, proj_execution_ready=False)
        res = P.authorize_workspace_execution(user, ws)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.AUTHZ_NOT_READY)
        self.assertIsNone(ws.__class__.objects.get(pk=ws.pk).execution_authorized_at)

    def test_refused_for_non_owner(self):
        _owner, acct = _account()
        ws = _ready_ws(acct, authorized=False)
        other = U.objects.create_user(username="intruder", email="intruder@x.invalid", password="x")
        BetaTester.objects.create(email=other.email, is_active=True)
        res = P.authorize_workspace_execution(other, ws)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.AUTHZ_NOT_OWNER)
        self.assertIsNone(ws.__class__.objects.get(pk=ws.pk).execution_authorized_at)


class AuthorizeEndpointDarkTests(TestCase):
    def test_authorize_endpoint_404_while_subsystem_dark(self):
        user, acct = _account(admit=False)
        _ready_ws(acct, authorized=False)
        client = APIClient()
        client.force_authenticate(user=user)
        # No FLAGS override here → subsystem dark → 404 BEFORE any DB read.
        self.assertEqual(client.post(AUTHORIZE, {}, format="json").status_code, 404)


@override_settings(**FLAGS)
class AuthorizeEndpointTests(TestCase):
    def test_endpoint_arms_when_ready_and_confirmed(self):
        user, acct = _account()
        _ready_ws(acct, authorized=False)
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(AUTHORIZE, {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], P.AUTHZ_OK)
        acct.hosted_workspace.refresh_from_db()
        self.assertTrue(acct.hosted_workspace.execution_enabled)

    def test_endpoint_409_when_not_ready(self):
        user, acct = _account()
        _ready_ws(acct, authorized=False, canonical_state=S.CONNECTED, proj_execution_ready=False)
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(AUTHORIZE, {}, format="json")
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.json()["reason"], P.AUTHZ_NOT_READY)


@override_settings(**FLAGS)
class ProviderAUnaffectedTests(TestCase):
    def test_temporary_validation_provider_has_no_authorization_term(self):
        # Customer Zero / legacy accounts use Provider A — the ADR-0047 authorization term lives ONLY in
        # PersistentWorkspaceProvider, so the Provider-A gate is byte-unchanged (no authz field consulted).
        _user, acct = _account(provider=R.TEMPORARY_VALIDATION)
        prov = R.provider_for(acct)
        self.assertIsInstance(prov, R.TemporaryValidationProvider)
        # The Provider-A evaluate path must not raise on / reference execution_authorized_at.
        dec = prov.evaluate(acct)
        self.assertNotEqual(dec.reason_code, R.RW_EXECUTION_NOT_AUTHORIZED)
