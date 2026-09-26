"""Phase C3 — Add Broker Account (DARK): adversarial matrix.

Covers requirement #14: first account; adds 2-5; attempted 6th; same login on different servers; different
brokers; DEMO+LIVE mix; duplicate/idempotent add; concurrent final-slot race; cross-user account-id + workspace-
uuid IDOR; wrong broker/server pairing; one account's provisioning failure leaving siblings untouched; tombstoned
account semantics. Everything rides the SAME certified service (request_hosted_workspace) — no second provisioning
architecture — and stays account-explicit (no ambiguous user-level .first()).
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from admin_ops.models import EntitlementOverride
from billing.models import UserSubscriptionState
from hosted_workspace import provisioning as P
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.onboarding_views import (OnboardingAddBrokerAccountView, OnboardingBindView,
                                               OnboardingConfirmView, OnboardingJourneyView)

U = get_user_model()
# Subsystem visible + Phase-C enforcement armed (so the multi-account path is exercised in tests).
_ARMED = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_WORKSPACE_ONBOARDING_ENABLED="1",
              CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=True)


def _user(name, *, plan=UserSubscriptionState.Plan.BETA):
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.update_or_create(
        user=u, defaults=dict(current_plan=plan, plan_status=UserSubscriptionState.PlanStatus.ACTIVE,
                              viewer_mode=False))
    # Per-user enforcement scope: grant the per-user activation so ARMED classes (master ON via _ARMED)
    # enforce this user. DARK classes/tests force the master OFF, so the grant is inert there.
    from trading.account_entitlement import grant_concurrent_enforcement
    grant_concurrent_enforcement(u)
    return u


def _override(user, capability, value):
    EntitlementOverride.objects.create(user=user, capability=capability, override_value=value, reason="t",
                                       is_active=True, expires_at=timezone.now() + timedelta(days=1))


def _add(user, login, server="IS6-Demo", is_demo=True, broker="B"):
    """Add a broker account via the C3 API endpoint (proves ownership + the endpoint contract)."""
    body = {"expected_login": login, "expected_server": server, "broker_name": broker, "is_demo": is_demo}
    req = APIRequestFactory().post("/api/hosted-workspace/accounts/add/", body, format="json")
    force_authenticate(req, user=user)
    return OnboardingAddBrokerAccountView.as_view()(req)


def _ws_count(user):
    return HostedMt5Workspace.objects.filter(trading_account__user=user).count()


@override_settings(**_ARMED)
class AddBrokerAccountFlow(TestCase):
    def test_first_account(self):
        u = _user("f1")
        r = _add(u, "111")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["status"], "created")
        self.assertIn("trading_account_id", r.data)
        self.assertIn("workspace_uuid", r.data)
        self.assertEqual(_ws_count(u), 1)

    def test_adds_two_through_five(self):
        u = _user("f2")
        for i in range(5):
            r = _add(u, str(100 + i))
            self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(_ws_count(u), 5)           # exactly the 5 explicitly added — never pre-provisioned

    def test_attempted_sixth_denied(self):
        u = _user("f3")
        _override(u, "max_trading_accounts", {"value": 5})   # owned cap 5
        for i in range(5):
            self.assertEqual(_add(u, str(200 + i)).status_code, 201)
        r6 = _add(u, "999")
        self.assertEqual(r6.status_code, 409)
        self.assertEqual(r6.data["reason"], P.REQ_LIMIT_REACHED)
        self.assertEqual(_ws_count(u), 5)

    def test_same_login_different_servers_are_distinct(self):
        u = _user("f4")
        r1 = _add(u, "111", server="IS6-Demo")
        r2 = _add(u, "111", server="PepperstoneUK-Demo")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r1.data["trading_account_id"], r2.data["trading_account_id"])
        self.assertEqual(_ws_count(u), 2)

    def test_different_brokers(self):
        u = _user("f5")
        self.assertEqual(_add(u, "111", server="IS6-Demo", broker="IS6").status_code, 201)
        self.assertEqual(_add(u, "222", server="PepperstoneUK-Demo", broker="Pepperstone").status_code, 201)
        self.assertEqual(_ws_count(u), 2)

    def test_demo_and_live_mixture(self):
        u = _user("f6")
        r_demo = _add(u, "111", server="IS6-Demo", is_demo=True)
        r_live = _add(u, "222", server="IS6-Live", is_demo=False)
        self.assertEqual(r_demo.status_code, 201)
        self.assertEqual(r_live.status_code, 201)
        from trading.models import TradingAccount
        self.assertFalse(TradingAccount.objects.get(id=r_live.data["trading_account_id"]).is_demo)
        self.assertTrue(TradingAccount.objects.get(id=r_demo.data["trading_account_id"]).is_demo)

    def test_duplicate_add_is_idempotent(self):
        u = _user("f7")
        r1 = _add(u, "111", server="IS6-Demo")
        r2 = _add(u, "111", server="IS6-Demo")            # identical (login, server)
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.data["status"], "exists")
        self.assertEqual(r1.data["trading_account_id"], r2.data["trading_account_id"])
        self.assertEqual(_ws_count(u), 1)

    def test_password_bearing_body_rejected(self):
        u = _user("f8")
        body = {"expected_login": "111", "password": "hunter2"}
        req = APIRequestFactory().post("/api/hosted-workspace/accounts/add/", body, format="json")
        force_authenticate(req, user=u)
        r = OnboardingAddBrokerAccountView.as_view()(req)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(_ws_count(u), 0)                 # nothing created


@override_settings(**_ARMED)
class TombstoneSemantics(TestCase):
    def test_tombstoned_account_frees_an_owned_slot(self):
        u = _user("t1")
        _override(u, "max_trading_accounts", {"value": 1})
        r1 = _add(u, "111")
        from trading.models import TradingAccount
        acct = TradingAccount.objects.get(id=r1.data["trading_account_id"])
        acct.disconnected_at = timezone.now(); acct.is_active = False
        acct.save(update_fields=["disconnected_at", "is_active"])
        r2 = _add(u, "222")                               # owned count now 0 (tombstone excluded)
        self.assertEqual(r2.status_code, 201)


@override_settings(**_ARMED)
class CrossUserIdorAttacks(TestCase):
    """A user must NEVER reach another user's account via a spoofed account_id / workspace_uuid selector."""

    def _post(self, view, user, **selector):
        req = APIRequestFactory().post("/api/hosted-workspace/onboarding/confirm/", selector, format="json")
        force_authenticate(req, user=user)
        return view.as_view()(req)

    def setUp(self):
        self.a = _user("victim")
        self.b = _user("attacker")
        self.a_ws = P.request_hosted_workspace(self.a, expected_login="111", expected_server="IS6-Demo").workspace
        # attacker needs their own workspace so N>=1 for them
        P.request_hosted_workspace(self.b, expected_login="222", expected_server="IS6-Demo")

    def test_cross_user_account_id_attack_is_404(self):
        before = self.a_ws.canonical_state
        r = self._post(OnboardingConfirmView, self.b, account_id=self.a_ws.trading_account_id)
        self.assertEqual(r.status_code, 404)              # attacker cannot act on victim's account
        self.a_ws.refresh_from_db()
        self.assertEqual(self.a_ws.canonical_state, before)   # victim workspace state untouched

    def test_cross_user_workspace_uuid_attack_is_404(self):
        before = self.a_ws.canonical_state
        r = self._post(OnboardingConfirmView, self.b, workspace_uuid=str(self.a_ws.workspace_uuid))
        self.assertEqual(r.status_code, 404)
        self.a_ws.refresh_from_db()
        self.assertEqual(self.a_ws.canonical_state, before)

    def test_cross_user_authorize_attack_is_404(self):
        # The ARM endpoint (the only path that can enable execution) must also refuse a spoofed selector.
        from hosted_workspace.onboarding_views import OnboardingAuthorizeExecutionView
        before = self.a_ws.canonical_state
        req = APIRequestFactory().post("/api/hosted-workspace/onboarding/authorize-execution/",
                                       {"account_id": self.a_ws.trading_account_id}, format="json")
        force_authenticate(req, user=self.b)
        r = OnboardingAuthorizeExecutionView.as_view()(req)
        self.assertEqual(r.status_code, 404)
        self.a_ws.refresh_from_db()
        self.assertEqual(self.a_ws.canonical_state, before)   # victim never armed

    def test_malformed_workspace_uuid_is_404_not_500(self):
        r = self._post(OnboardingConfirmView, self.b, workspace_uuid="not-a-uuid")
        self.assertEqual(r.status_code, 404)              # malformed selector → no match → 404, never a 500

    def test_bind_cross_user_workspace_uuid_attack_is_404(self):
        req = APIRequestFactory().post("/api/hosted-workspace/onboarding/bind/",
                                       {"workspace_uuid": str(self.a_ws.workspace_uuid),
                                        "expected_login": "333"}, format="json")
        force_authenticate(req, user=self.b)
        r = OnboardingBindView.as_view()(req)
        self.assertEqual(r.status_code, 404)
        self.a_ws.refresh_from_db()
        self.assertEqual(self.a_ws.trading_account.account_number, "111")   # victim's identity untouched


@override_settings(**_ARMED)
class AccountExplicitOnboarding(TestCase):
    """With N>1 the onboarding write/journey ops must refuse to guess and require an explicit selector."""

    def setUp(self):
        self.u = _user("ax")
        self.ws1 = P.request_hosted_workspace(self.u, expected_login="111", expected_server="IS6-Demo").workspace
        self.ws2 = P.request_hosted_workspace(self.u, expected_login="222", expected_server="IS6-Demo").workspace

    def test_confirm_without_selector_is_400(self):
        req = APIRequestFactory().post("/api/hosted-workspace/onboarding/confirm/", {}, format="json")
        force_authenticate(req, user=self.u)
        r = OnboardingConfirmView.as_view()(req)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["reason"], "account_selector_required")

    def test_journey_without_selector_lists_accounts(self):
        req = APIRequestFactory().get("/api/hosted-workspace/onboarding/journey/")
        force_authenticate(req, user=self.u)
        r = OnboardingJourneyView.as_view()(req)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], "multiple_accounts")
        ids = {a["account_id"] for a in r.data["accounts"]}
        self.assertEqual(ids, {self.ws1.trading_account_id, self.ws2.trading_account_id})

    def test_journey_with_selector_targets_that_account(self):
        req = APIRequestFactory().get("/api/hosted-workspace/onboarding/journey/",
                                      {"account_id": self.ws2.trading_account_id})
        force_authenticate(req, user=self.u)
        r = OnboardingJourneyView.as_view()(req)
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.data.get("status"), "multiple_accounts")   # resolved to the one requested


@override_settings(**_ARMED)
class WrongBrokerServerPairing(TestCase):
    def test_missing_identity_is_400_not_a_workspace(self):
        u = _user("w1")
        # empty login + empty server with a live (non-deferred) path → invalid identity, no workspace created
        with override_settings(**{**_ARMED, "HOSTED_DEFERRED_IDENTITY_BIND_ENABLED": "0"}):
            r = _add(u, "", server="")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(_ws_count(u), 0)


@override_settings(**_ARMED)
class SiblingFailureIsolation(TestCase):
    """Requirement #7 — one account's provisioning failure must NOT stop/skip its siblings. The per-workspace
    driver isolates each account: account A's allocation exploding leaves account B allocated in the same cycle."""

    def test_one_account_failure_leaves_sibling_provisioned(self):
        from unittest import mock
        from hosted_workspace.provisioning import ALLOC_OK
        from hosted_workspace.provisioning_runner import run_workspace_provisioning
        from hosted_workspace.state_machine import WorkspaceLifecycleState as S

        u = _user("sib")
        _override(u, "max_trading_accounts", {"value": 5})
        ws_a = P.request_hosted_workspace(u, expected_login="111", expected_server="IS6-Demo").workspace
        ws_b = P.request_hosted_workspace(u, expected_login="222", expected_server="IS6-Demo").workspace
        HostedMt5Workspace.objects.filter(pk__in=[ws_a.pk, ws_b.pk]).update(canonical_state=str(S.PROVISIONING))

        class _Res:
            def __init__(self, ok, reason):
                self.ok, self.reason = ok, reason

        def side_effect(ws, *a, **k):
            if ws.pk == ws_a.pk:
                raise RuntimeError("account A provisioning exploded")
            return _Res(True, ALLOC_OK)

        with mock.patch("hosted_workspace.provisioning.allocate_workspace_node", side_effect=side_effect):
            summary = run_workspace_provisioning()
        self.assertEqual(summary["candidates"], 2)
        self.assertEqual(summary["errors"], 1)        # account A failed
        self.assertEqual(summary["allocated"], 1)     # account B STILL allocated — the cycle did not abort on A


@override_settings(**_ARMED)
class SelectorResolvesExactAccount(TestCase):
    """The account-explicit resolver must return the EXACT selected account — a regression back to .first()
    would silently act on (e.g. arm) the WRONG owned account. Tests the resolver every write/arm view uses."""

    def test_own_workspace_selector_targets_the_right_account(self):
        from hosted_workspace.onboarding_views import _own_workspace
        u = _user("sel")
        ws1 = P.request_hosted_workspace(u, expected_login="111", expected_server="IS6-Demo").workspace
        ws2 = P.request_hosted_workspace(u, expected_login="222", expected_server="IS6-Demo").workspace
        self.assertEqual(_own_workspace(u, account_id=ws2.trading_account_id).pk, ws2.pk)
        self.assertEqual(_own_workspace(u, account_id=ws1.trading_account_id).pk, ws1.pk)
        self.assertEqual(_own_workspace(u, workspace_uuid=str(ws2.workspace_uuid)).pk, ws2.pk)
        # never resolves the other owned account for a given selector (would-be .first() bug)
        self.assertNotEqual(_own_workspace(u, account_id=ws2.trading_account_id).pk, ws1.pk)
        # no selector + N>1 → ambiguous → None (never an arbitrary pick)
        self.assertIsNone(_own_workspace(u))


@override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_WORKSPACE_ONBOARDING_ENABLED="1",
                   CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=False)   # master KILL => DARK despite per-user grant
class AddEndpointDark(TestCase):
    """Subsystem visible but Phase-C enforcement OFF (default) — the add endpoint must NOT create a 2nd
    account; a second add returns the caller's existing workspace (exists/200)."""

    def test_second_add_returns_existing_no_new_account(self):
        u = _user("dark")
        r1 = _add(u, "111", server="IS6-Demo")
        self.assertEqual(r1.status_code, 201)
        r2 = _add(u, "222", server="PepperstoneUK-Demo")   # different identity, but DARK → one-per-user
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.data["status"], "exists")
        self.assertEqual(_ws_count(u), 1)                  # no 2nd production account while DARK

    def test_add_endpoint_404_when_subsystem_dark(self):
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED="0"):
            r = _add(_user("dark2"), "111")
            self.assertEqual(r.status_code, 404)           # invisible while the subsystem is OFF


@override_settings(CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=False)   # class default DARK; armed methods override
class OnboardingAccountResolutionDarkSafe(TestCase):
    """The onboarding milestone account resolver must be DARK-byte-identical: while enforcement is OFF it keeps
    the legacy single-pick even for a multi-TradingAccount user (which is possible TODAY via the Accounts page),
    and only refuses to guess once ARMED — never a production onboarding regression."""

    def _two_accounts(self, name):
        from trading.models import TradingAccount
        u = _user(name)
        a1 = TradingAccount.objects.create(user=u, name="A", account_number="1", broker_name="B", is_demo=True)
        TradingAccount.objects.create(user=u, name="A", account_number="2", broker_name="B", is_demo=True)
        return u, a1

    def test_dark_multi_account_uses_first_no_ambiguity_error(self):
        from onboarding.services import _resolve_onboarding_account
        u, a1 = self._two_accounts("res1")                 # enforcement OFF (default)
        self.assertEqual(_resolve_onboarding_account(u).id, a1.id)   # legacy oldest-pick, byte-identical

    @override_settings(CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=True)
    def test_armed_multi_account_refuses_to_guess(self):
        from onboarding.services import OnboardingStepError, _resolve_onboarding_account
        u, _ = self._two_accounts("res2")
        with self.assertRaises(OnboardingStepError) as ctx:
            _resolve_onboarding_account(u)
        self.assertIn("account_selector_required", str(ctx.exception))

    @override_settings(CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=True)
    def test_armed_explicit_account_id_resolves(self):
        from onboarding.services import _resolve_onboarding_account
        u, a1 = self._two_accounts("res3")
        self.assertEqual(_resolve_onboarding_account(u, a1.id).id, a1.id)


@override_settings(**_ARMED)
class ArbitraryBrokerServerPairing(TestCase):
    """Add-time records the (login, server) as an INTENT pairing — login-belongs-to-server is validated later at
    broker connect, not at add. A well-formed but arbitrary pairing is accepted as intent (no false rejection),
    and two different pairings are distinct accounts."""

    def test_arbitrary_pairing_recorded_as_intent(self):
        u = _user("pair")
        r = _add(u, "555", server="SomeOther-Demo", broker="OtherBroker")
        self.assertEqual(r.status_code, 201)               # accepted as intent; connect-time validates the pair
        from trading.models import TradingAccount
        acct = TradingAccount.objects.get(id=r.data["trading_account_id"])
        self.assertEqual(acct.account_number, "555")
        self.assertEqual(acct.broker_server.server_name, "SomeOther-Demo")


class ConcurrentFinalSlotRace(TransactionTestCase):
    """Two simultaneous add-account attempts racing the user's FINAL owned slot: exactly one wins; the owned
    cap is never exceeded (the per-user select_for_update row lock serialises them)."""

    @override_settings(**_ARMED)
    def test_two_racers_one_final_slot(self):
        import threading
        from django.db import connection
        u = _user("race")
        _override(u, "max_trading_accounts", {"value": 1})   # exactly one owned slot
        barrier = threading.Barrier(2)
        results = {}

        def worker(name, login):
            barrier.wait()
            try:
                res = P.request_hosted_workspace(u, expected_login=login, expected_server="IS6-Demo")
                results[name] = ("ok" if (res.ok and res.created) else ("exists" if res.ok else res.reason))
            except Exception as e:  # noqa: BLE001
                results[name] = f"err:{e}"
            finally:
                connection.close()

        t1 = threading.Thread(target=worker, args=("t1", "111"))
        t2 = threading.Thread(target=worker, args=("t2", "222"))
        t1.start(); t2.start(); t1.join(); t2.join()

        # exactly one created a workspace; the other was refused at the owned cap. Never two.
        created = [k for k, v in results.items() if v == "ok"]
        denied = [k for k, v in results.items() if v == P.REQ_LIMIT_REACHED]
        self.assertEqual(len(created), 1, results)
        self.assertEqual(len(denied), 1, results)
        self.assertEqual(_ws_count(u), 1)
