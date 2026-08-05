"""WS-D/E (packet: Customer Journey Consolidation) — the customer readiness projection behind the
marketplace "Enable Trading" panel.

The endpoint is READ-ONLY, un-gated (reachable like status — no arm flag, no cohort gate), ownership-scoped,
and returns ONLY machine values (state / checklist keys / next-action code) so no runtime/model/backend
terminology can leak to the customer. It must mirror the arm endpoint's fail-closed gates EXACTLY, so the
panel can never say "ready" where arm would refuse. These tests pin that equivalence.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from strategies.models import Strategy, StrategyAssignment
from terminal_provisioning import beta_capacity as cap
from terminal_provisioning.models import ProvisioningJob
from terminal_provisioning.provisioner import FakeProvisioner, advance_provisioning_job, enqueue_op
from trading.crypto import encrypt_password
from trading.models import TradingAccount

User = get_user_model()
URL = "/api/strategies/strategies/signal-copy/readiness/"
MP = "mp-010"          # Wayond WIM → signal_source ti_signals
SRC = "ti_signals"
BASE = dict(BETA_RUNTIMES_ENABLED=True, BETA_MAX_TESTERS=1000)
APPROVED = "pilot@x.invalid"
CHECK_KEYS = ["demo", "active", "credentials", "runtime_ready", "pilot_access"]


def _user(email):
    return User.objects.create_user(username=email, email=email, password="x")


def _acct(user, *, is_demo=True, is_active=True, creds=True, number="990100"):
    return TradingAccount.objects.create(
        user=user, name=number, account_number=number, broker_name="DemoBroker",
        is_demo=is_demo, is_active=is_active,
        password_enc=encrypt_password("pw") if creds else "")


def _ready_runtime(account):
    rt = cap.reserve_beta_slot(account)
    advance_provisioning_job(enqueue_op(rt, ProvisioningJob.Op.PROVISION), FakeProvisioner())
    rt.refresh_from_db()
    return rt


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _checks(data):
    return {c["key"]: c["ok"] for c in data["checklist"]}


@override_settings(**BASE)
class ReadinessShapeTests(TestCase):
    def test_requires_signal_copy_template_and_account_id(self):
        u = _user("a@x.invalid")
        # non-signal-copy template
        r = _client(u).get(URL, {"marketplace_strategy_id": "mp-005", "account_id": 1})
        self.assertEqual(r.status_code, 400)
        # missing account_id
        acct = _acct(u)
        r = _client(u).get(URL, {"marketplace_strategy_id": MP})
        self.assertEqual(r.status_code, 400)
        self.assertIn("account_id", r.json().get("detail", ""))

    def test_ownership_scoped_404_for_other_users_account(self):
        owner = _user("owner@x.invalid")
        acct = _acct(owner)
        other = _user("other@x.invalid")
        r = _client(other).get(URL, {"marketplace_strategy_id": MP, "account_id": acct.id})
        self.assertEqual(r.status_code, 404)

    def test_response_is_machine_only_no_customer_prose(self):
        # Every customer string is chosen by the FE from i18n; the payload carries only codes.
        u = _user("m@x.invalid")
        acct = _acct(u, creds=False)
        data = _client(u).get(URL, {"marketplace_strategy_id": MP, "account_id": acct.id}).json()
        self.assertEqual([c["key"] for c in data["checklist"]], CHECK_KEYS)
        # next_action is a short snake_case code, never a sentence.
        self.assertRegex(data["next_action"], r"^[a-z_]+$")
        self.assertIn(data["state"], {
            "SETUP_INCOMPLETE", "PREPARING", "CONNECTING", "READY", "TRADING_ON", "NEEDS_ATTENTION", "CLOSED"})


@override_settings(**BASE)
class ReadinessStateTests(TestCase):
    def _ready_account(self, email, **kw):
        u = _user(email)
        acct = _acct(u, **kw)
        _ready_runtime(acct)
        acct.refresh_from_db()
        return u, acct

    def test_missing_credentials_is_setup_incomplete(self):
        u = _user("c@x.invalid")
        acct = _acct(u, creds=False)
        data = _client(u).get(URL, {"marketplace_strategy_id": MP, "account_id": acct.id}).json()
        self.assertFalse(_checks(data)["credentials"])
        self.assertEqual(data["state"], "SETUP_INCOMPLETE")
        self.assertEqual(data["next_action"], "add_credentials")
        self.assertFalse(data["can_arm"])

    def test_no_runtime_is_preparing(self):
        u = _user("p@x.invalid")
        acct = _acct(u)  # creds present, but no runtime provisioned
        data = _client(u).get(URL, {"marketplace_strategy_id": MP, "account_id": acct.id}).json()
        self.assertFalse(_checks(data)["runtime_ready"])
        self.assertEqual(data["state"], "PREPARING")
        self.assertEqual(data["next_action"], "preparing")
        self.assertFalse(data["can_arm"])

    def test_ready_but_not_pilot_approved_blocks_arm(self):
        # The decisive per-USER gate: a fully technically-ready account still can't arm without approval.
        u, acct = self._ready_account("ready@x.invalid")
        data = _client(u).get(URL, {"marketplace_strategy_id": MP, "account_id": acct.id}).json()
        checks = _checks(data)
        self.assertTrue(checks["demo"] and checks["active"] and checks["credentials"] and checks["runtime_ready"])
        self.assertFalse(checks["pilot_access"])       # not on the allowlist
        self.assertEqual(data["state"], "READY")
        self.assertEqual(data["next_action"], "request_access")
        self.assertFalse(data["can_arm"])

    @override_settings(INTERNAL_PILOT_ARM_APPROVED_EMAILS=APPROVED, BETA_SELF_SERVE_ARM_ENABLED=True)
    def test_fully_ready_approved_can_arm(self):
        u, acct = self._ready_account(APPROVED)
        data = _client(u).get(URL, {"marketplace_strategy_id": MP, "account_id": acct.id}).json()
        self.assertTrue(all(_checks(data).values()))
        self.assertEqual(data["state"], "READY")
        self.assertEqual(data["next_action"], "ready_enable")
        self.assertTrue(data["can_arm"])

    @override_settings(INTERNAL_PILOT_ARM_APPROVED_EMAILS=APPROVED, BETA_SELF_SERVE_ARM_ENABLED=True)
    def test_armed_and_enabled_is_trading_on(self):
        u, acct = self._ready_account(APPROVED)
        strat = Strategy.objects.create(owner=u, name="Wayond WIM Strategy")
        StrategyAssignment.objects.create(
            strategy=strat, account=acct, execution_mode=StrategyAssignment.ExecutionMode.AUTO_DEMO,
            signal_source=SRC, is_active=True, stage=StrategyAssignment.STAGE_LIVE)
        data = _client(u).get(URL, {"marketplace_strategy_id": MP, "account_id": acct.id}).json()
        self.assertTrue(data["armed"] and data["enabled"])
        self.assertEqual(data["state"], "TRADING_ON")
        self.assertEqual(data["next_action"], "trading_on")

    @override_settings(INTERNAL_PILOT_ARM_APPROVED_EMAILS=APPROVED, BETA_SELF_SERVE_ARM_ENABLED=True)
    def test_single_tenant_slot_taken_blocks_can_arm(self):
        # DIVERGENCE GUARD: another account already holds the ti_signals router slot (fan-out OFF). Even a
        # fully-ready, approved pilot account must NOT show can_arm — arm would 409 source_single_tenant.
        incumbent = _user("incumbent@x.invalid")
        inc_acct = _acct(incumbent, number="990900")
        inc_strat = Strategy.objects.create(owner=incumbent, name="Wayond WIM Strategy")
        StrategyAssignment.objects.create(
            strategy=inc_strat, account=inc_acct, execution_mode=StrategyAssignment.ExecutionMode.AUTO_DEMO,
            signal_source=SRC, is_active=True, stage=StrategyAssignment.STAGE_LIVE)
        # A different, fully-ready, approved pilot account.
        user, acct = self._ready_account(APPROVED)
        data = _client(user).get(URL, {"marketplace_strategy_id": MP, "account_id": acct.id}).json()
        self.assertTrue(all(_checks(data).values()))     # technically everything is ✓ …
        self.assertFalse(data["can_arm"])                # … but the router slot is taken
        self.assertEqual(data["state"], "NEEDS_ATTENTION")
        self.assertEqual(data["next_action"], "single_tenant")

    def test_validation_failure_is_needs_attention(self):
        u, acct = self._ready_account("v@x.invalid")
        acct.validation_status = TradingAccount.ValidationStatus.CONNECTION_FAILED
        acct.save(update_fields=["validation_status"])
        data = _client(u).get(URL, {"marketplace_strategy_id": MP, "account_id": acct.id}).json()
        self.assertEqual(data["state"], "NEEDS_ATTENTION")
        self.assertEqual(data["next_action"], "attention_validation")
        self.assertFalse(data["can_arm"])

    def test_reachable_without_arm_flag_or_approval(self):
        # Read-only: the endpoint must answer even in the fully DARK default (no arm flag, empty allowlist).
        u, acct = self._ready_account("dark@x.invalid")
        r = _client(u).get(URL, {"marketplace_strategy_id": MP, "account_id": acct.id})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["can_arm"])          # default-deny cohort → cannot arm, but state is visible
