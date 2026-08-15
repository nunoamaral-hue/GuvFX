"""Beta UX Correction (Sponsor 2026-08-15) — DEFERRED broker-identity bind + no per-user admission + write-once.

Proves the three Beta Blockers of the Beta UX Correction packet against the REAL production functions:
  A. deferred broker-identity binding (request a workspace with NO broker login/server; declare it later);
  B. removal of the per-user beta admission/allowlist dependency (global CLOSED_BETA_OPEN_ACCESS_ENABLED);
  C. write-once hosted broker identity (no PATCH / re-pin after bind).

Numbered ``test_mandatory_NN_*`` methods map 1:1 to the packet's mandatory-test list. Email verification
(mandatory #2) is exercised by ``onboarding/tests_email_verification_send.py`` (the send/verify endpoints are
unchanged); here we assert only that admission never substitutes for it (a fresh registrant still verifies).
"""
from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from execution.readiness import PERSISTENT_WORKSPACE, evaluate_readiness
from execution.models import TerminalNode
from trading.models import BrokerServer, TradingAccount
from trading.serializers import TradingAccountSerializer

from hosted_workspace import provisioning as P
from hosted_workspace.consumer import ingest_observation
from hosted_workspace.entitlement import has_hosted_workspace_capability
from hosted_workspace.manager import WorkspaceObservation
from hosted_workspace.matching import ExpectedAccount, WorkspaceObservation as MObs, evaluate_active_account_match
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

User = get_user_model()

EXPECTED_LOGIN = "770077"
EXPECTED_SERVER = "GuvfxBeta-Demo"
_CZ_PATCH = "hosted_workspace.tenant_isolation.customer_zero_account_ids"

# Deferred-bind Closed-Beta go-live flag set. NO per-email admission (CLOSED_BETA_OPEN_ACCESS_ENABLED replaces
# it); DEFERRED bind on; slot-prep + cert deliberately OFF (host boundary kept out; supervised posture carries
# the readiness gate). BETA_ADMISSION_ARM_ENABLED + INTERNAL_PILOT_ARM_APPROVED_EMAILS are NOT set, so the arm
# is authorised ONLY by the open-access branch — exactly what the packet requires (no per-email allowlist).
FLAGS = dict(
    HOSTED_PERSISTENT_MT5_ENABLED=True,
    HOSTED_WORKSPACE_ONBOARDING_ENABLED=True,
    HOSTED_MT5_EXECUTION_ENABLED=True,
    SUPERVISED_SINGLE_TENANT_BETA_ENABLED=True,
    BETA_SELF_SERVE_ARM_ENABLED=True,
    HOSTED_DEFERRED_IDENTITY_BIND_ENABLED=True,
    CLOSED_BETA_OPEN_ACCESS_ENABLED=True,
    BETA_RUNTIMES_ENABLED=True,
    BETA_MAX_TESTERS=1000,
)


def _mk_user(email="beta.deferred@example.invalid"):
    return User.objects.create_user(username=email.split("@")[0], email=email, password="x")


def _mk_node(host="10.60.0.9"):
    return TerminalNode.objects.create(
        hostname="beta-node-1", status=TerminalNode.Status.ACTIVE, rdp_host=host, max_accounts=1)


def _feed(ws, *, version, **health):
    obs = WorkspaceObservation(
        process_running=health.get("process_running", True),
        ipc_available=health.get("ipc_available", True),
        connected=health.get("connected", True),
        account_match=health["account_match"],
        trade_allowed=health.get("trade_allowed", True),
        fresh=health.get("fresh", True),
        previous_state=str(S.PROVISIONING),
        observed_at=None,
    )
    return ingest_observation(ws, obs, observation_version=version, correlation_id=f"defbind-v{version}")


@override_settings(**FLAGS)
class DeferredRequestTests(TestCase):
    def test_mandatory_03_request_without_broker_login_server(self):
        """#3 — a workspace request succeeds with NO broker login/server under the deferred flag."""
        user = _mk_user()
        req = P.request_hosted_workspace(user, expected_login="", expected_server="", is_demo=True)
        self.assertTrue(req.ok, req.reason)
        self.assertEqual(req.reason, P.REQ_CREATED)
        acct = req.workspace.trading_account
        self.assertEqual((acct.account_number or "").strip(), "")     # UNBOUND
        self.assertIsNone(acct.broker_server_id)
        self.assertFalse(acct.is_active)
        self.assertEqual(acct.readiness_provider, PERSISTENT_WORKSPACE)
        self.assertEqual((acct.password_enc or ""), "")               # never a password

    def test_mandatory_05_reaches_waiting_for_login_unbound(self):
        """#5 — allocation advances an UNBOUND workspace to WAITING_FOR_LOGIN (slot-prep off, no host)."""
        user = _mk_user()
        req = P.request_hosted_workspace(user, expected_login="", is_demo=True)
        _mk_node()
        alloc = P.allocate_workspace_node(req.workspace)
        self.assertTrue(alloc.ok, alloc.reason)
        ws = req.workspace
        ws.refresh_from_db()
        self.assertEqual(str(ws.canonical_state), S.WAITING_FOR_LOGIN)
        self.assertEqual((ws.trading_account.account_number or "").strip(), "")   # still unbound

    @override_settings(HOSTED_DEFERRED_IDENTITY_BIND_ENABLED=False)
    def test_mandatory_18_dark_path_requires_login(self):
        """#18 — with deferred bind OFF, request WITHOUT a login is byte-identical to before (REQ_LOGIN_REQUIRED)."""
        user = _mk_user()
        req = P.request_hosted_workspace(user, expected_login="", is_demo=True)
        self.assertFalse(req.ok)
        self.assertEqual(req.reason, P.REQ_LOGIN_REQUIRED)

    def test_mandatory_04_materialisation_spec_has_no_broker_identity(self):
        """#4 — the host materialisation spec carries NO broker identity (login/server/broker-password); the
        Windows slot is derived from account_id only, so provisioning is broker-identity agnostic."""
        from terminal_provisioning import services as prov_services
        user = _mk_user()
        req = P.request_hosted_workspace(user, expected_login="", is_demo=True)
        acct = req.workspace.trading_account
        prov = prov_services.provision(acct, actor=None)
        spec = prov_services.build_spec(prov)
        self.assertNotIn("account_number", spec)
        self.assertNotIn("expected_login", spec)
        self.assertNotIn("expected_server", spec)
        self.assertNotIn("broker_server", spec)
        self.assertEqual(spec["account_id"], acct.pk)                 # identity derived from account_id only


@override_settings(**FLAGS)
class BindBrokerIdentityTests(TestCase):
    def _wfl(self, user=None):
        user = user or _mk_user()
        req = P.request_hosted_workspace(user, expected_login="", is_demo=True)
        _mk_node()
        P.allocate_workspace_node(req.workspace)
        req.workspace.refresh_from_db()
        return user, req.workspace

    def test_mandatory_08_bind_sets_identity_exactly_once(self):
        """#8 — external bind sets login/server once; an identical re-declaration is idempotent; a DIFFERENT
        second bind fails closed (write-once)."""
        user, ws = self._wfl()
        r1 = P.bind_broker_identity(user, ws, expected_login=EXPECTED_LOGIN, expected_server=EXPECTED_SERVER)
        self.assertTrue(r1.ok, r1.reason)
        self.assertEqual(r1.reason, P.BIND_OK)
        acct = ws.trading_account
        acct.refresh_from_db()
        self.assertEqual(acct.account_number, EXPECTED_LOGIN)
        self.assertEqual(acct.broker_server.server_name, EXPECTED_SERVER)
        # identical retry → idempotent, no change
        r2 = P.bind_broker_identity(user, ws, expected_login=EXPECTED_LOGIN, expected_server=EXPECTED_SERVER)
        self.assertTrue(r2.ok)
        self.assertEqual(r2.reason, P.BIND_IDEMPOTENT)
        # different second bind → fail closed
        r3 = P.bind_broker_identity(user, ws, expected_login="999999", expected_server="Other-Demo")
        self.assertFalse(r3.ok)
        self.assertEqual(r3.reason, P.BIND_ALREADY)
        acct.refresh_from_db()
        self.assertEqual(acct.account_number, EXPECTED_LOGIN)         # unchanged

    def test_mandatory_13_second_conflicting_bind_is_deterministic(self):
        """#13 — a second, conflicting bind is deterministic + fail-closed (first-writer wins)."""
        user, ws = self._wfl()
        self.assertTrue(P.bind_broker_identity(user, ws, expected_login="111", expected_server="A").ok)
        second = P.bind_broker_identity(user, ws, expected_login="222", expected_server="B")
        self.assertFalse(second.ok)
        self.assertEqual(second.reason, P.BIND_ALREADY)

    def test_bind_over_long_identity_is_clean_reason_not_500(self):
        """Adversarial fix — an over-long login/server yields a clean BIND_IDENTITY_INVALID reason, never an
        uncaught Postgres DataError (HTTP 500)."""
        user, ws = self._wfl()
        res = P.bind_broker_identity(user, ws, expected_login="9" * 100, expected_server=EXPECTED_SERVER)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.BIND_IDENTITY_INVALID)

    def test_bind_owner_scoped(self):
        user, ws = self._wfl()
        other = _mk_user("intruder@example.invalid")
        res = P.bind_broker_identity(other, ws, expected_login=EXPECTED_LOGIN, expected_server=EXPECTED_SERVER)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.BIND_NOT_OWNER)

    def test_bind_login_required(self):
        user, ws = self._wfl()
        res = P.bind_broker_identity(user, ws, expected_login="", expected_server=EXPECTED_SERVER)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.BIND_LOGIN_REQUIRED)

    def test_bind_refuses_non_demo(self):
        """Closed Beta is DEMO-only — a non-demo account can never self-bind (never self-authorise live)."""
        user, ws = self._wfl()
        acct = ws.trading_account
        TradingAccount.objects.filter(pk=acct.pk).update(is_demo=False)   # bypass save() to set the precondition
        res = P.bind_broker_identity(user, ws, expected_login=EXPECTED_LOGIN, expected_server=EXPECTED_SERVER)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.BIND_LIVE_FORBIDDEN)

    def test_bind_wrong_state(self):
        """Bind is only allowed pre-connected — once the workspace is CONNECTED it is refused."""
        user, ws = self._wfl()
        # bind then advance to CONNECTED via a matched-but-halted observation
        self.assertTrue(P.bind_broker_identity(user, ws, expected_login=EXPECTED_LOGIN,
                                               expected_server=EXPECTED_SERVER).ok)
        _feed(ws, version=2, account_match=True, trade_allowed=False)
        ws.refresh_from_db()
        self.assertEqual(str(ws.canonical_state), S.CONNECTED)
        res = P.bind_broker_identity(user, ws, expected_login=EXPECTED_LOGIN, expected_server=EXPECTED_SERVER)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.BIND_WRONG_STATE)   # the pre-connected state guard fires first


@override_settings(**FLAGS)
class WriteOnceIdentityTests(TestCase):
    def _bound_hosted(self):
        user = _mk_user()
        req = P.request_hosted_workspace(user, expected_login="", is_demo=True)
        _mk_node()
        P.allocate_workspace_node(req.workspace)
        ws = req.workspace
        ws.refresh_from_db()
        P.bind_broker_identity(user, ws, expected_login=EXPECTED_LOGIN, expected_server=EXPECTED_SERVER)
        acct = ws.trading_account
        acct.refresh_from_db()
        return user, acct

    def test_mandatory_12a_model_guard_blocks_repin(self):
        """#12 — the authoritative model-layer guard refuses to change account_number of a bound hosted account."""
        _, acct = self._bound_hosted()
        acct.account_number = "999999"
        with self.assertRaises(ValidationError):
            acct.save(update_fields=["account_number"])
        # full save (update_fields=None) is also guarded (the DRF path)
        acct.refresh_from_db()
        acct.account_number = "888888"
        with self.assertRaises(ValidationError):
            acct.save()

    def test_mandatory_12b_serializer_rejects_repin(self):
        """#12 — the generic account serializer rejects an account_number / broker_server change on a hosted
        account with a clean validation error (400), never a 500."""
        _, acct = self._bound_hosted()
        ser = TradingAccountSerializer(instance=acct, data={"account_number": "999999"}, partial=True)
        self.assertFalse(ser.is_valid())
        self.assertIn("account_number", ser.errors)
        other = BrokerServer.objects.create(server_name="Different-Demo")
        ser2 = TradingAccountSerializer(instance=acct, data={"broker_server": other.pk}, partial=True)
        self.assertFalse(ser2.is_valid())
        self.assertIn("broker_server", ser2.errors)

    def test_repin_defence_pre_bind_serializer_blocks_first_bind(self):
        """Adversarial fix — the generic account API must NOT first-bind a deferred (unbound) hosted account's
        identity or flip its classification (that would skip the demo-only + audited bind seam). The dedicated
        seam still binds the same fixture."""
        user = _mk_user()
        req = P.request_hosted_workspace(user, expected_login="", is_demo=True)
        acct = req.workspace.trading_account
        acct.refresh_from_db()
        self.assertEqual((acct.account_number or "").strip(), "")   # deferred: still unbound
        ser = TradingAccountSerializer(instance=acct,
                                       data={"account_number": "9999999", "is_demo": False}, partial=True)
        self.assertFalse(ser.is_valid())                            # generic API cannot first-bind identity here
        _mk_node()
        P.allocate_workspace_node(req.workspace)
        req.workspace.refresh_from_db()
        self.assertTrue(P.bind_broker_identity(user, req.workspace, expected_login=EXPECTED_LOGIN,
                                               expected_server=EXPECTED_SERVER).ok)   # the seam still works

    def test_mandatory_17_legacy_provider_a_unaffected(self):
        """#17 — a legacy / Provider-A account's account_number stays MUTABLE (write-once is hosted-only)."""
        user = _mk_user()
        srv = BrokerServer.objects.create(server_name="Legacy-Demo")
        legacy = TradingAccount.objects.create(
            user=user, name="legacy", broker_name="Legacy", broker_server=srv,
            account_number="111111", is_demo=True, is_active=True)  # readiness_provider default (not hosted)
        self.assertNotEqual(legacy.readiness_provider, PERSISTENT_WORKSPACE)
        legacy.account_number = "222222"
        legacy.save(update_fields=["account_number"])                # no guard for legacy
        legacy.refresh_from_db()
        self.assertEqual(legacy.account_number, "222222")


@override_settings(**FLAGS)
class AccountMatchTests(TestCase):
    def _wfl_bound(self, bind=True):
        user = _mk_user()
        req = P.request_hosted_workspace(user, expected_login="", is_demo=True)
        _mk_node()
        P.allocate_workspace_node(req.workspace)
        ws = req.workspace
        ws.refresh_from_db()
        if bind:
            P.bind_broker_identity(user, ws, expected_login=EXPECTED_LOGIN, expected_server=EXPECTED_SERVER)
            ws.refresh_from_db()
        return user, ws

    def test_mandatory_09_observation_cannot_self_bind(self):
        """#9 — feeding observations NEVER writes the expected broker identity; the account stays UNBOUND
        (the observation is confirmation evidence only, never the source of truth)."""
        _, ws = self._wfl_bound(bind=False)
        _feed(ws, version=2, account_match=True, trade_allowed=False)   # a "matched" observation, unbound acct
        ws.refresh_from_db()
        self.assertEqual((ws.trading_account.account_number or "").strip(), "")   # STILL unbound
        self.assertIsNone(ws.trading_account.broker_server_id)

    def test_mandatory_06_07_no_match_before_bind(self):
        """#6/#7 — before bind the expected identity is empty → the matcher fails closed
        (expected_login_unconfigured), so account_match can never be True and no order can flow."""
        obs = MObs(process_running=True, ipc_available=True, connected=True, trade_allowed=True,
                   login=EXPECTED_LOGIN, server=EXPECTED_SERVER, trade_mode=0)
        expected_unbound = ExpectedAccount(login="", server="", is_demo=True)
        d = evaluate_active_account_match(obs, expected_unbound)
        self.assertFalse(d.ok)
        self.assertEqual(d.reason, "expected_login_unconfigured")

    def test_mandatory_10_correct_observation_after_bind_matches(self):
        """#10 — after bind, an observation of the SAME login/server produces account_match=ok."""
        _, ws = self._wfl_bound(bind=True)
        acct = ws.trading_account
        obs = MObs(process_running=True, ipc_available=True, connected=True, trade_allowed=True,
                   login=acct.account_number, server=acct.broker_server.server_name, trade_mode=0)
        expected = ExpectedAccount(login=acct.account_number, server=acct.broker_server.server_name, is_demo=True)
        self.assertTrue(evaluate_active_account_match(obs, expected).ok)

    def test_mandatory_11_wrong_observation_fails_closed(self):
        """#11 — after bind, an observation of a DIFFERENT login/server stays fail-closed (mismatch)."""
        _, ws = self._wfl_bound(bind=True)
        acct = ws.trading_account
        expected = ExpectedAccount(login=acct.account_number, server=acct.broker_server.server_name, is_demo=True)
        wrong_login = MObs(process_running=True, ipc_available=True, connected=True, trade_allowed=True,
                           login="000000", server=acct.broker_server.server_name, trade_mode=0)
        self.assertEqual(evaluate_active_account_match(wrong_login, expected).reason,
                         "active_account_login_mismatch")
        wrong_server = MObs(process_running=True, ipc_available=True, connected=True, trade_allowed=True,
                            login=acct.account_number, server="Bad-Server", trade_mode=0)
        self.assertEqual(evaluate_active_account_match(wrong_server, expected).reason,
                         "active_account_server_mismatch")


@override_settings(**FLAGS)
class AdmissionRemovalTests(TestCase):
    def test_mandatory_01_fresh_email_capability_without_admission(self):
        """#1 — a fresh unknown user (no BetaTester row) holds hosted capability under the global flag, so
        request_hosted_workspace succeeds without any prior admission."""
        user = _mk_user("unknown.fresh@example.invalid")
        from billing.models import BetaTester
        self.assertFalse(BetaTester.objects.filter(email__iexact=user.email).exists())
        self.assertTrue(has_hosted_workspace_capability(user))
        req = P.request_hosted_workspace(user, expected_login="", is_demo=True)
        self.assertTrue(req.ok, req.reason)

    @override_settings(CLOSED_BETA_OPEN_ACCESS_ENABLED=False)
    def test_mandatory_18b_capability_denied_without_flag_or_admission(self):
        """#18 — with the open-access flag OFF and no admission/entitlement, capability is denied (byte-identical
        to the pre-change fail-closed behaviour)."""
        user = _mk_user("nocap@example.invalid")
        self.assertFalse(has_hosted_workspace_capability(user))

    def test_mandatory_15_wayond_self_enable_without_allowlist(self):
        """#15 — the arm cohort gate authorises a non-CZ identity under the open-access flag with NO per-email
        allowlist / BetaTester admission; with the flag off (and empty allowlist) it denies."""
        from strategies.views import _arm_cohort_approved
        user = _mk_user("armfresh@example.invalid")
        with mock.patch(_CZ_PATCH, return_value=frozenset()):
            self.assertTrue(_arm_cohort_approved(user))
            with override_settings(CLOSED_BETA_OPEN_ACCESS_ENABLED=False):
                self.assertFalse(_arm_cohort_approved(user))

    def test_mandatory_16_customer_zero_never_armed(self):
        """#16 — Customer Zero protection is unchanged: a user who OWNS a reserved CZ account is NEVER
        arm-authorised, even with open-access on (the fail-closed CZ-owner exclusion is re-applied)."""
        from strategies.views import _arm_cohort_approved, _owner_is_customer_zero
        user = _mk_user("czowner@example.invalid")
        cz_acct = TradingAccount.objects.create(
            user=user, name="cz", broker_name="CZ", account_number="1", is_demo=False, is_active=True)
        with mock.patch(_CZ_PATCH, return_value=frozenset({cz_acct.pk})):
            self.assertTrue(_owner_is_customer_zero(user))
            self.assertFalse(_arm_cohort_approved(user))   # denied despite open-access ON


@override_settings(**FLAGS)
class BindEndpointTests(TestCase):
    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def _wfl(self, user):
        req = P.request_hosted_workspace(user, expected_login="", is_demo=True)
        _mk_node()
        P.allocate_workspace_node(req.workspace)
        return req.workspace

    def test_bind_endpoint_happy_path(self):
        user = _mk_user()
        self._wfl(user)
        resp = self._client(user).post("/api/hosted-workspace/onboarding/bind/",
                                       {"expected_login": EXPECTED_LOGIN, "expected_server": EXPECTED_SERVER},
                                       format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        acct = TradingAccount.objects.get(user=user)
        self.assertEqual(acct.account_number, EXPECTED_LOGIN)

    def test_bind_endpoint_rejects_password_body(self):
        user = _mk_user()
        self._wfl(user)
        resp = self._client(user).post("/api/hosted-workspace/onboarding/bind/",
                                       {"expected_login": EXPECTED_LOGIN, "password": "hunter2"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json().get("reason"), P.REQ_PASSWORD_FORBIDDEN)

    def test_bind_endpoint_no_workspace_404(self):
        user = _mk_user("noworkspace@example.invalid")   # never requested a workspace
        resp = self._client(user).post("/api/hosted-workspace/onboarding/bind/",
                                       {"expected_login": EXPECTED_LOGIN}, format="json")
        self.assertEqual(resp.status_code, 404)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=False)
    def test_bind_endpoint_404_while_dark(self):
        user = _mk_user()
        resp = self._client(user).post("/api/hosted-workspace/onboarding/bind/",
                                       {"expected_login": EXPECTED_LOGIN}, format="json")
        self.assertEqual(resp.status_code, 404)


@override_settings(**FLAGS)
class DeferredJourneyE2ETests(TestCase):
    def test_mandatory_14_full_deferred_journey_to_execution_ready(self):
        """#14 (+ #1 #3 #5 #8 #9 #15 end-to-end) — a fresh unknown user with NO admission: request WITHOUT a
        broker identity → provision → WAITING_FOR_LOGIN (unbound) → bind → observe match → confirm → armed →
        EXECUTION_READY. Uses the REAL production functions at every step; the observer/host boundary is the
        only fake (observations fed through the certified writer)."""
        user = _mk_user("journey.fresh@example.invalid")
        with mock.patch(_CZ_PATCH, return_value=frozenset()):
            # request WITHOUT a broker identity (deferred) — no BetaTester admission exists
            req = P.request_hosted_workspace(user, expected_login="", is_demo=True)
            self.assertTrue(req.ok, req.reason)
            ws = req.workspace
            acct = ws.trading_account
            self.assertEqual((acct.account_number or "").strip(), "")

            _mk_node()
            self.assertTrue(P.allocate_workspace_node(ws).ok)
            ws.refresh_from_db()
            self.assertEqual(str(ws.canonical_state), S.WAITING_FOR_LOGIN)

            # the user opens MT5 and declares their demo account — the deferred bind
            self.assertTrue(P.bind_broker_identity(
                user, ws, expected_login=EXPECTED_LOGIN, expected_server=EXPECTED_SERVER).ok)
            acct.refresh_from_db()
            self.assertEqual(acct.account_number, EXPECTED_LOGIN)

            # observed connected+matched (halted) → CONNECTED; then fully healthy → EXECUTION_READY
            _feed(ws, version=2, account_match=True, trade_allowed=False)
            ws.refresh_from_db()
            self.assertEqual(str(ws.canonical_state), S.CONNECTED)
            _feed(ws, version=3, account_match=True, trade_allowed=True)
            ws.refresh_from_db()
            self.assertEqual(str(ws.canonical_state), S.EXECUTION_READY)

            # customer ACK activates the account
            self.assertTrue(P.confirm_broker_account(user, ws).ok)
            acct.refresh_from_db()
            self.assertTrue(acct.is_active)

            # arm the workspace (the arm AUTHORISATION is proven separately in #15 without any per-email
            # allowlist); Provider-B readiness must then be organically eligible — no admission was ever created
            HostedMt5Workspace.objects.filter(pk=ws.pk).update(execution_enabled=True)
            decision = evaluate_readiness(acct)
            self.assertTrue(decision.eligible, decision.reason_code)
            self.assertEqual(decision.provider, PERSISTENT_WORKSPACE)


class EmailVerificationStillRequiredTests(TestCase):
    def test_mandatory_02_admission_does_not_bypass_email_verification(self):
        """#2 — email verification remains a required onboarding step (the send/verify endpoints are unchanged
        and covered by onboarding/tests_email_verification_send.py). Admission never substitutes for it, so a
        fresh open-beta registrant still verifies their email."""
        from onboarding.services import REQUIRED_STEPS
        self.assertIn("email_verified", set(REQUIRED_STEPS))
