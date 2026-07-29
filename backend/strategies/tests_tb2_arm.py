"""TB-2 (Trusted Beta) — self-service Enable-Trading (arm) endpoint.

Proves: technical-validation gating (no admin review), owner isolation, idempotency, and the
single-tenant protection that refuses to create a router-ambiguous 2nd arm while the fan-out flag is
OFF. All new capability is behind the default-OFF BETA_SELF_SERVE_ARM_ENABLED flag.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from billing.models import BetaTester
from strategies.models import Strategy, StrategyAssignment
from strategies.views import _account_execution_ready
from trading.models import TradingAccount

User = get_user_model()
ARM_URL = "/api/strategies/strategies/signal-copy/arm/"  # include prefix + router prefix
MP = "mp-010"          # Wayond WIM Strategy → signal_source ti_signals
MP_NON_COPY = "mp-001"  # a normal (non-signal-copy) template
SRC = "ti_signals"
AM = StrategyAssignment.ExecutionMode

BASE = dict(BETA_SELF_SERVE_ARM_ENABLED=True, BETA_RUNTIMES_ENABLED=True, BETA_MAX_TESTERS=1000)
READY = ("strategies.views._account_execution_ready", )


def _admitted(username, *, staff=False):
    u = User.objects.create_user(username=username, email=f"{username}@x.invalid", password="x",
                                 is_staff=staff)
    if not staff:
        BetaTester.objects.create(email=u.email)
    return u


def _demo_acct(user, number, *, is_demo=True, pw="enc"):
    return TradingAccount.objects.create(
        user=user, name=number, account_number=number, broker_name="DemoBroker",
        is_demo=is_demo, is_active=True, password_enc=pw)


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@override_settings(**BASE)
class ArmGateTests(TestCase):
    def setUp(self):
        self.user = _admitted("g1")
        self.acct = _demo_acct(self.user, "G1")
        self.c = _client(self.user)

    def _post(self, **body):
        with mock.patch(READY[0], return_value=(True, "ready")):
            return self.c.post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": self.acct.id,
                                         **body}, format="json")

    @override_settings(BETA_SELF_SERVE_ARM_ENABLED=False)
    def test_flag_off_refuses(self):
        r = self._post()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["status"], "arming_disabled")

    def test_unknown_marketplace_id(self):
        self.assertEqual(self._post(marketplace_strategy_id="nope").status_code, 400)

    def test_non_signal_copy_rejected(self):
        self.assertEqual(self._post(marketplace_strategy_id=MP_NON_COPY).status_code, 400)

    def test_account_not_owned_is_404(self):
        other = _demo_acct(_admitted("g2"), "G2")
        self.assertEqual(self._post(account_id=other.id).status_code, 404)

    def test_plain_user_can_arm_admission_removed(self):
        # ADR-0021 removed per-user admission from Enable Trading: a plain (non-allowlisted) owner whose
        # owned runtime is ready can arm — governed by ownership + validation + runtime-ready, NOT
        # membership. (Arming only creates authority; nothing fires until the Class-B master levers.)
        plain = User.objects.create_user(username="plain", email="plain@x.invalid", password="x")
        acct = _demo_acct(plain, "PL1")
        with mock.patch(READY[0], return_value=(True, "ready")):
            r = _client(plain).post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": acct.id},
                                    format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "armed")

    def test_non_demo_account_refused(self):
        self.acct.is_demo = False
        self.acct.save(update_fields=["is_demo"])
        r = self._post()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["status"], "account_not_ready")

    def test_missing_credentials_refused(self):
        self.acct.password_enc = ""
        self.acct.save(update_fields=["password_enc"])
        r = self._post()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["status"], "credentials_missing")

    def test_runtime_not_ready_refused(self):
        # No runtime exists → the REAL readiness gate fails closed (not mocked here).
        r = self.c.post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": self.acct.id},
                        format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["status"], "runtime_not_ready")


@override_settings(**BASE)
class ArmSuccessTests(TestCase):
    def setUp(self):
        self.user = _admitted("s1")
        self.acct = _demo_acct(self.user, "S1")
        self.c = _client(self.user)

    def _arm(self, account=None):
        with mock.patch(READY[0], return_value=(True, "ready")):
            return self.c.post(ARM_URL, {"marketplace_strategy_id": MP,
                                         "account_id": (account or self.acct).id}, format="json")

    def _routable(self, acct):
        return StrategyAssignment.objects.filter(
            account=acct, execution_mode=AM.AUTO_DEMO, signal_source=SRC,
            stage=StrategyAssignment.STAGE_LIVE, is_active=True)

    def test_arm_creates_auto_demo_live_assignment(self):
        r = self._arm()
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["status"], "armed")
        self.assertTrue(body["created"])
        self.assertEqual(self._routable(self.acct).count(), 1)

    def test_arm_is_idempotent(self):
        self.assertTrue(self._arm().json()["created"])
        second = self._arm().json()
        self.assertFalse(second["created"])
        self.assertEqual(self._routable(self.acct).count(), 1)

    def test_arm_reactivates_paused_or_downgraded(self):
        strat = Strategy.objects.create(owner=self.user, name="Wayond WIM Strategy")
        StrategyAssignment.objects.create(
            strategy=strat, account=self.acct, execution_mode=AM.MANUAL, signal_source="",
            stage=StrategyAssignment.STAGE_TEST, is_active=False)
        r = self._arm()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._routable(self.acct).count(), 1)   # reactivated + upgraded, not a 2nd

    def test_cannot_arm_another_users_account(self):
        victim = _demo_acct(_admitted("s2"), "S2")
        r = self._arm(account=victim)
        self.assertEqual(r.status_code, 404)
        self.assertFalse(self._routable(victim).exists())


@override_settings(**BASE)
class ArmSingleTenantProtectionTests(TestCase):
    def setUp(self):
        self.user = _admitted("t1")
        self.acct = _demo_acct(self.user, "T1")
        # Another account already armed on the SAME source (e.g. Nuno).
        other_user = _admitted("t2")
        other_acct = _demo_acct(other_user, "T2")
        strat = Strategy.objects.create(owner=other_user, name="Existing")
        StrategyAssignment.objects.create(
            strategy=strat, account=other_acct, execution_mode=AM.AUTO_DEMO, signal_source=SRC,
            stage=StrategyAssignment.STAGE_LIVE, is_active=True)
        self.c = _client(self.user)

    def _arm(self):
        with mock.patch(READY[0], return_value=(True, "ready")):
            return self.c.post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": self.acct.id},
                               format="json")

    def test_refused_when_fanout_off(self):
        # Default: MULTI_ACCOUNT_ROUTING_ENABLED is OFF → a 2nd arm on the source is refused so the
        # router can never go ambiguous (which would stop auto-copy for everyone).
        r = self._arm()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["status"], "source_single_tenant")

    def test_refused_when_incumbent_is_paused(self):
        # B-1: a PAUSED incumbent arm (is_active=False) is a latent 2nd — it must still block, else
        # reactivating it later would make the router ambiguous and stop auto-copy for everyone.
        StrategyAssignment.objects.filter(signal_source=SRC).update(is_active=False)
        r = self._arm()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["status"], "source_single_tenant")

    def test_refused_second_strategy_on_same_account(self):
        # M-1: a second DIFFERENT strategy already armed on the SAME source + the arming account also
        # blocks (two arms on one account are two routable rows → router None).
        s2 = Strategy.objects.create(owner=self.user, name="Other WIM")
        StrategyAssignment.objects.create(
            strategy=s2, account=self.acct, execution_mode=AM.AUTO_DEMO, signal_source=SRC,
            stage=StrategyAssignment.STAGE_LIVE, is_active=True)
        r = self._arm()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["status"], "source_single_tenant")

    @override_settings(MULTI_ACCOUNT_ROUTING_ENABLED=True)
    def test_allowed_when_fanout_on(self):
        r = self._arm()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "armed")


@override_settings(**BASE)
class ToggleResumeSingleTenantTests(TestCase):
    """The resume (enable) path must ALSO refuse to activate a 2nd routable arm on a source while
    fan-out is OFF — source-global, not owner-scoped (a non-staff user could otherwise resume theirs
    into a cross-user 2nd)."""
    TOGGLE_URL = "/api/strategies/strategies/signal-copy/toggle/"

    def setUp(self):
        self.userA = _admitted("ra")
        self.acctA = _demo_acct(self.userA, "RA1")
        stratA = Strategy.objects.create(owner=self.userA, name="Wayond WIM Strategy")
        # A's own arm, PAUSED.
        StrategyAssignment.objects.create(
            strategy=stratA, account=self.acctA, execution_mode=AM.AUTO_DEMO, signal_source=SRC,
            stage=StrategyAssignment.STAGE_LIVE, is_active=False)
        # Another account ACTIVELY copying the same source.
        other = _admitted("rb")
        acctB = _demo_acct(other, "RB1")
        stratB = Strategy.objects.create(owner=other, name="Existing")
        StrategyAssignment.objects.create(
            strategy=stratB, account=acctB, execution_mode=AM.AUTO_DEMO, signal_source=SRC,
            stage=StrategyAssignment.STAGE_LIVE, is_active=True)

    def test_resume_refused_when_another_account_active(self):
        r = _client(self.userA).post(
            self.TOGGLE_URL, {"marketplace_strategy_id": MP, "enabled": True}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["status"], "source_single_tenant")

    @override_settings(MULTI_ACCOUNT_ROUTING_ENABLED=True)
    def test_resume_allowed_when_fanout_on(self):
        r = _client(self.userA).post(
            self.TOGGLE_URL, {"marketplace_strategy_id": MP, "enabled": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "enabled")


@override_settings(**BASE)
class ReadinessHelperTests(TestCase):
    def _ready_runtime(self, acct):
        from terminal_provisioning import beta_capacity as cap
        from terminal_provisioning.models import ProvisioningVerificationReport, RuntimeState
        from terminal_provisioning.runtime_state import record_transition
        rt = cap.reserve_beta_slot(acct)
        for st in (RuntimeState.STARTING, RuntimeState.AUTHENTICATING, RuntimeState.RUNNING):
            rt = record_transition(rt, st, reason_code="t")
        rt.last_heartbeat_at = timezone.now()
        rt.save(update_fields=["last_heartbeat_at"])
        ProvisioningVerificationReport.objects.create(runtime=rt, runtime_uuid=rt.runtime_uuid)
        return rt

    def test_no_runtime_not_ready(self):
        acct = _demo_acct(_admitted("r0"), "700100")
        self.assertEqual(_account_execution_ready(acct), (False, "runtime_not_ready"))

    def test_ready_runtime_is_ready(self):
        acct = _demo_acct(_admitted("r1"), "700101")
        self._ready_runtime(acct)
        self.assertEqual(_account_execution_ready(acct), (True, "ready"))

    @override_settings(PROVISIONING_REQUIRE_BROKER_LOGIN=True)
    def test_broker_login_required_but_not_connected(self):
        acct = _demo_acct(_admitted("r2"), "700102")
        self._ready_runtime(acct)   # runtime up but no broker_login_verified report
        self.assertEqual(_account_execution_ready(acct), (False, "broker_not_connected"))
