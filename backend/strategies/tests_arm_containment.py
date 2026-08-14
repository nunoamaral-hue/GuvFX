"""CONTAIN-1 (Sponsor 2026-08-05) — fail-closed cohort containment on the self-service arm endpoint.

Proves the authenticated `signal-copy/arm` path is contained by a DEDICATED approved-cohort allowlist
EVEN WHILE BETA_SELF_SERVE_ARM_ENABLED=1 — hiding the frontend button is NOT the boundary; this is.

Required proofs (per Sponsor):
 * an ordinary authenticated user is refused even while the arm flag is ON;
 * an approved pilot user can reach the existing arm service;
 * frontend visibility is not relied upon for authorisation (these are direct API calls, no UI);
 * direct API calls cannot bypass the cohort gate;
 * Customer Zero (an admitted BetaTester) is NOT implicitly approved.

No live order is placed; the arm master levers stay OFF (arming only creates authority).
"""
from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from billing.models import BetaTester
from core.models import AuditEvent
from strategies.models import StrategyAssignment
from terminal_provisioning import beta_capacity as cap
from terminal_provisioning.models import ProvisioningJob
from terminal_provisioning.provisioner import FakeProvisioner, advance_provisioning_job, enqueue_op
from trading.crypto import encrypt_password
from trading.models import TradingAccount

User = get_user_model()
ARM_URL = "/api/strategies/strategies/signal-copy/arm/"
TOGGLE_URL = "/api/strategies/strategies/signal-copy/toggle/"
MP = "mp-010"          # Wayond WIM → signal_source ti_signals
SRC = "ti_signals"
# Arm flag ON globally — the whole point is that containment holds regardless.
BASE = dict(BETA_SELF_SERVE_ARM_ENABLED=True, BETA_RUNTIMES_ENABLED=True, BETA_MAX_TESTERS=1000)
APPROVED = "pilot@x.invalid"


def _user(email):
    return User.objects.create_user(username=email, email=email, password="x")


def _acct(user, number="990100", *, is_demo=True, is_active=True):
    return TradingAccount.objects.create(
        user=user, name=number, account_number=number, broker_name="DemoBroker",
        is_demo=is_demo, is_active=is_active, password_enc=encrypt_password("pw"))


def _ready_runtime(account):
    rt = cap.reserve_beta_slot(account)
    advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
    rt.refresh_from_db()
    return rt


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@override_settings(**BASE)
class ArmContainmentTests(TestCase):
    def _armable_account(self, email):
        """A user + fully technically-armable account (demo/active/creds/ready runtime)."""
        user = _user(email)
        acct = _acct(user)
        _ready_runtime(acct)
        acct.refresh_from_db()
        return user, acct

    def test_ordinary_user_refused_even_with_flag_on(self):
        # Not in INTERNAL_PILOT_ARM_APPROVED_EMAILS (empty) → refused, though flag ON + runtime ready.
        user, acct = self._armable_account("ordinary@x.invalid")
        r = _client(user).post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": acct.id},
                               format="json")
        self.assertEqual(r.status_code, 403, r.content)
        self.assertEqual(r.json()["status"], "not_pilot_approved")
        # NO trading authority was created.
        self.assertFalse(StrategyAssignment.objects.filter(account=acct).exists())

    def test_direct_api_call_cannot_bypass_the_gate(self):
        # A raw authenticated API call (no frontend involved) is still refused — visibility ≠ authz.
        user, acct = self._armable_account("raw@x.invalid")
        r = _client(user).post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": acct.id},
                               format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["status"], "not_pilot_approved")

    def test_customer_zero_betatester_not_implicitly_approved(self):
        # CZ is an admitted BetaTester. BetaTester membership must NOT confer arm approval.
        BetaTester.objects.create(email="cz@x.invalid", is_active=True)
        user, acct = self._armable_account("cz@x.invalid")
        r = _client(user).post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": acct.id},
                               format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["status"], "not_pilot_approved")

    def test_staff_not_implicitly_approved(self):
        # No staff bypass on the cohort gate (default deny).
        user = User.objects.create_user(username="s@x.invalid", email="s@x.invalid",
                                        password="x", is_staff=True)
        acct = _acct(user)
        _ready_runtime(acct)
        r = _client(user).post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": acct.id},
                               format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["status"], "not_pilot_approved")

    @override_settings(INTERNAL_PILOT_ARM_APPROVED_EMAILS=APPROVED)
    def test_approved_pilot_can_reach_arm_service(self):
        user, acct = self._armable_account(APPROVED)
        r = _client(user).post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": acct.id},
                               format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "armed")
        self.assertTrue(StrategyAssignment.objects.filter(
            account=acct, execution_mode="AUTO_DEMO", stage="LIVE", is_active=True).exists())

    @override_settings(INTERNAL_PILOT_ARM_APPROVED_EMAILS=APPROVED)
    def test_approved_pilot_still_gated_on_runtime_ready(self):
        # Cohort approval does not bypass the technical gates — a not-ready runtime still refuses.
        user = _user(APPROVED)
        acct = _acct(user)  # no runtime
        r = _client(user).post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": acct.id},
                               format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["status"], "runtime_not_ready")

    @override_settings(INTERNAL_PILOT_ARM_APPROVED_EMAILS="a@x.invalid, " + APPROVED + " ,b@x.invalid")
    def test_allowlist_is_comma_multi_and_trimmed(self):
        user, acct = self._armable_account(APPROVED)
        r = _client(user).post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": acct.id},
                               format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def test_refusal_is_durably_audited(self):
        user, acct = self._armable_account("audit@x.invalid")
        _client(user).post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": acct.id},
                           format="json")
        self.assertTrue(
            AuditEvent.objects.filter(event_type="SIGNAL_COPY_ARM_REFUSED").exists(),
            "a fail-closed arm refusal must write a durable audit event")

    @override_settings(INTERNAL_PILOT_ARM_APPROVED_EMAILS=APPROVED)
    def test_toggle_enable_also_cohort_gated_but_disable_allowed(self):
        # Arm as the approved user, then a NON-approved co-owner cannot enable — but disable is allowed.
        user, acct = self._armable_account(APPROVED)
        _client(user).post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": acct.id},
                           format="json")
        # Same user loses approval (simulate) → cannot ENABLE, but CAN DISABLE (safety stop).
        with override_settings(INTERNAL_PILOT_ARM_APPROVED_EMAILS=""):
            dis = _client(user).post(TOGGLE_URL, {"marketplace_strategy_id": MP, "enabled": False},
                                     format="json")
            self.assertEqual(dis.status_code, 200)
            self.assertEqual(dis.json()["status"], "disabled")
            en = _client(user).post(TOGGLE_URL, {"marketplace_strategy_id": MP, "enabled": True},
                                    format="json")
            self.assertEqual(en.status_code, 403)
            self.assertEqual(en.json()["status"], "not_pilot_approved")


@override_settings(**BASE)
class ArmAdmissionAuthorizationTests(TestCase):
    """ADR-0045 (Beta Launch, 2026-08-14) — beta-admission-derived ARM authorization.

    Removes the redundant SECOND per-user operator step (a hand-added
    ``INTERNAL_PILOT_ARM_APPROVED_EMAILS`` entry) that blocked an already-admitted beta user at "Enable
    Trading" — the autonomy Beta Blocker. When ``BETA_ADMISSION_ARM_ENABLED`` is on, an admitted ACTIVE
    ``BetaTester`` (NOT Customer Zero) is arm-authorized directly. Proven here: DARK by default; grants
    only for an admitted active non-CZ tester when on; Customer Zero excluded by construction even when
    on; the technical gates and the original email allowlist are untouched."""

    def _armable(self, email):
        user = _user(email)
        acct = _acct(user)
        _ready_runtime(acct)
        acct.refresh_from_db()
        return user, acct

    def _arm(self, user, acct):
        return _client(user).post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": acct.id},
                                  format="json")

    def test_admitted_tester_refused_when_admission_arm_flag_off(self):
        # DARK by default: admission alone does NOT confer arm authority — byte-identical to pre-ADR-0045.
        BetaTester.objects.create(email="beta1@x.invalid", is_active=True)
        user, acct = self._armable("beta1@x.invalid")
        r = self._arm(user, acct)
        self.assertEqual(r.status_code, 403, r.content)
        self.assertEqual(r.json()["status"], "not_pilot_approved")
        self.assertFalse(StrategyAssignment.objects.filter(account=acct).exists())

    @override_settings(BETA_ADMISSION_ARM_ENABLED=True)
    def test_admitted_active_tester_can_arm_when_flag_on(self):
        BetaTester.objects.create(email="beta2@x.invalid", is_active=True)
        user, acct = self._armable("beta2@x.invalid")
        # Pin "this user is NOT Customer Zero" deterministically (the reserved id is a hardcoded {1} and
        # Postgres does not reset the auto-id sequence between TestCases, so a test account can otherwise
        # coincidentally land on pk=1). The genuine CZ-exclusion behaviour is proven in the test below.
        with mock.patch("hosted_workspace.tenant_isolation.customer_zero_account_ids",
                        return_value=frozenset()):
            r = self._arm(user, acct)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "armed")
        self.assertTrue(StrategyAssignment.objects.filter(
            account=acct, execution_mode="AUTO_DEMO", stage="LIVE", is_active=True).exists())

    @override_settings(BETA_ADMISSION_ARM_ENABLED=True)
    def test_non_admitted_user_still_refused_when_flag_on(self):
        # Not a BetaTester at all → the admission source cannot grant; still denied.
        user, acct = self._armable("stranger@x.invalid")
        r = self._arm(user, acct)
        self.assertEqual(r.status_code, 403, r.content)
        self.assertEqual(r.json()["status"], "not_pilot_approved")

    @override_settings(BETA_ADMISSION_ARM_ENABLED=True)
    def test_inactive_tester_refused_when_flag_on(self):
        # An admitted-but-DEACTIVATED tester is not admitted → refused (revocation still bites).
        BetaTester.objects.create(email="inactive@x.invalid", is_active=False)
        user, acct = self._armable("inactive@x.invalid")
        r = self._arm(user, acct)
        self.assertEqual(r.status_code, 403, r.content)
        self.assertEqual(r.json()["status"], "not_pilot_approved")

    @override_settings(BETA_ADMISSION_ARM_ENABLED=True)
    def test_customer_zero_excluded_even_when_admitted_and_flag_on(self):
        # THE safety proof: CZ is an admitted BetaTester, but owning a reserved-CZ account excludes it
        # from admission-derived arm authorization — CZ is NEVER re-authorized onto the arm path.
        BetaTester.objects.create(email="cz@x.invalid", is_active=True)
        user, acct = self._armable("cz@x.invalid")
        with mock.patch("hosted_workspace.tenant_isolation.customer_zero_account_ids",
                        return_value=frozenset({acct.id})):
            r = self._arm(user, acct)
        self.assertEqual(r.status_code, 403, r.content)
        self.assertEqual(r.json()["status"], "not_pilot_approved")
        self.assertFalse(StrategyAssignment.objects.filter(account=acct).exists())

    @override_settings(BETA_ADMISSION_ARM_ENABLED=True)
    def test_admitted_tester_still_gated_on_runtime_ready(self):
        # Admission grants AUTHORIZATION, not a bypass of technical readiness — a not-ready runtime 409s.
        BetaTester.objects.create(email="notready@x.invalid", is_active=True)
        user = _user("notready@x.invalid")
        acct = _acct(user)  # no runtime provisioned
        with mock.patch("hosted_workspace.tenant_isolation.customer_zero_account_ids",
                        return_value=frozenset()):  # pin non-CZ (see note above)
            r = self._arm(user, acct)
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(r.json()["status"], "runtime_not_ready")

    @override_settings(BETA_ADMISSION_ARM_ENABLED=True, INTERNAL_PILOT_ARM_APPROVED_EMAILS=APPROVED)
    def test_email_allowlist_still_authorizes_independently(self):
        # Source 1 (the original email allowlist) is unchanged: a non-BetaTester on it still arms.
        user, acct = self._armable(APPROVED)
        r = self._arm(user, acct)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "armed")
