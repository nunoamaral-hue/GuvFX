"""AJ#7 — "Get Strategy" (acquire without enabling execution).

Proves the core safety contract: Get creates (or returns) the signal-copy assignment in a NON-EXECUTING
state (``is_active=False``) that the auto-router can never route; it is owner-scoped, demo-only, cohort +
flag gated, and idempotent; and it NEVER enables or disables an existing arm. Enabling stays the separate,
deliberate ``signal_copy_arm`` action.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from billing.models import BetaTester
from execution.auto_router import _resolve_target
from strategies.models import StrategyAssignment
from trading.models import TradingAccount

User = get_user_model()
GET_URL = "/api/strategies/strategies/signal-copy/get/"
ARM_URL = "/api/strategies/strategies/signal-copy/arm/"
STATUS_URL = "/api/strategies/strategies/signal-copy/status/"
MP = "mp-010"           # Wayond WIM → signal_source ti_signals
MP_NON_COPY = "mp-001"  # a normal (non-signal-copy) template
SRC = "ti_signals"
AM = StrategyAssignment.ExecutionMode

BASE = dict(BETA_SELF_SERVE_ARM_ENABLED=True, BETA_RUNTIMES_ENABLED=True, BETA_MAX_TESTERS=1000)


def _admitted(username, *, staff=False):
    u = User.objects.create_user(username=username, email=f"{username}@x.invalid", password="x", is_staff=staff)
    if not staff:
        BetaTester.objects.create(email=u.email)
    return u


def _demo_acct(user, number, *, is_demo=True, is_active=True):
    return TradingAccount.objects.create(
        user=user, name=number, account_number=number, broker_name="DemoBroker",
        is_demo=is_demo, is_active=is_active, password_enc="enc")


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@override_settings(**BASE)
@mock.patch("strategies.views._arm_cohort_approved", new=lambda user: True)
class GetStrategyTests(TestCase):
    def setUp(self):
        self.user = _admitted("g1")
        self.acct = _demo_acct(self.user, "G1")
        self.c = _client(self.user)

    def _get(self, **body):
        return self.c.post(GET_URL, {"marketplace_strategy_id": MP, "account_id": self.acct.id, **body},
                           format="json")

    # ── The core safety property ──────────────────────────────────────────────────────────────────────
    def test_get_creates_non_executing_owned_assignment_that_never_routes(self):
        r = self._get()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["status"], "owned")
        self.assertIs(r.json()["enabled"], False)
        a = StrategyAssignment.objects.get(id=r.json()["assignment_id"])
        # Owned/draft: AUTO_DEMO + ti_signals + stage LIVE but is_active=False (inert).
        self.assertEqual(a.execution_mode, AM.AUTO_DEMO)
        self.assertEqual(a.signal_source, SRC)
        self.assertEqual(a.stage, StrategyAssignment.STAGE_LIVE)
        self.assertFalse(a.is_active)
        # The auto-router can NEVER route it (requires is_active=True) — Get alone never trades.
        self.assertIsNone(_resolve_target(AM.AUTO_DEMO, SRC))

    def test_get_serializes_with_advisory_lock(self):
        # AJ#7.1 adversarial fix #5: Get must take the per-source advisory lock (as arm does) so two concurrent
        # first-ever Gets cannot race into duplicate Strategy rows → duplicate inactive assignments → ambiguous.
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            r = self._get()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(
            any("pg_advisory_xact_lock" in q["sql"] for q in ctx.captured_queries),
            "signal_copy_get must acquire the per-source advisory lock to serialize concurrent Gets",
        )

    def test_get_is_idempotent_and_never_creates_duplicates(self):
        first = self._get()
        self.assertEqual(first.status_code, 201)
        second = self._get()
        self.assertEqual(second.status_code, 200)  # already owned → 200, not a new create
        self.assertEqual(first.json()["assignment_id"], second.json()["assignment_id"])
        self.assertEqual(StrategyAssignment.objects.filter(account=self.acct, signal_source=SRC).count(), 1)
        self.assertIsNone(_resolve_target(AM.AUTO_DEMO, SRC))  # still inert

    def test_get_never_disables_an_already_enabled_strategy(self):
        # Arm first (owned → enabled), then Get again must NOT pause it.
        with mock.patch("strategies.views._account_execution_ready", return_value=(True, "ready")):
            armed = self.c.post(ARM_URL, {"marketplace_strategy_id": MP, "account_id": self.acct.id}, format="json")
        self.assertEqual(armed.status_code, 200, armed.content)
        a = StrategyAssignment.objects.get(account=self.acct, signal_source=SRC)
        self.assertTrue(a.is_active)
        self.assertIsNotNone(_resolve_target(AM.AUTO_DEMO, SRC))  # routable while enabled
        # Get again — idempotent, must leave the enabled arm ENABLED (Get never toggles is_active).
        again = self._get()
        self.assertEqual(again.status_code, 200)
        self.assertIs(again.json()["enabled"], True)
        a.refresh_from_db()
        self.assertTrue(a.is_active)
        self.assertIsNotNone(_resolve_target(AM.AUTO_DEMO, SRC))

    # ── Gating / ownership ────────────────────────────────────────────────────────────────────────────
    @override_settings(BETA_SELF_SERVE_ARM_ENABLED=False)
    def test_flag_off_refuses(self):
        r = self._get()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["status"], "arming_disabled")

    def test_non_signal_copy_rejected(self):
        self.assertEqual(self._get(marketplace_strategy_id=MP_NON_COPY).status_code, 400)

    def test_unknown_marketplace_id_rejected(self):
        self.assertEqual(self._get(marketplace_strategy_id="nope").status_code, 400)

    def test_account_not_owned_is_404(self):
        other = _admitted("g2")
        foreign = _demo_acct(other, "G2")
        self.assertEqual(self._get(account_id=foreign.id).status_code, 404)
        # And no assignment leaked onto the foreign account.
        self.assertFalse(StrategyAssignment.objects.filter(account=foreign).exists())

    def test_non_demo_account_rejected(self):
        live = _demo_acct(self.user, "G1L", is_demo=False)
        r = self._get(account_id=live.id)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["status"], "account_not_ready")

    def test_status_reports_owned_account_id(self):
        got = self._get()
        r = self.c.get(STATUS_URL, {"marketplace_strategy_id": MP})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["armed"])
        self.assertIs(body["enabled"], False)   # owned, not enabled
        self.assertEqual(body["account_id"], self.acct.id)
        self.assertEqual(body["assignment_id"], got.json()["assignment_id"])


@override_settings(**BASE)
class GetStrategyCohortTests(TestCase):
    """Cohort gating is independent of the flag (a non-approved caller is refused even with the flag ON)."""
    def setUp(self):
        self.user = _admitted("gc1")
        self.acct = _demo_acct(self.user, "GC1")
        self.c = _client(self.user)

    @mock.patch("strategies.views._arm_cohort_approved", new=lambda user: False)
    def test_not_cohort_approved_is_403(self):
        r = self.c.post(GET_URL, {"marketplace_strategy_id": MP, "account_id": self.acct.id}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["status"], "not_pilot_approved")
        self.assertFalse(StrategyAssignment.objects.filter(account=self.acct).exists())
