"""Stream D4a — LiveExecutionAuthorization model + centralized validity policy + readiness condition-11.

Invariants pinned here:
  * A LIVE account with NO valid authorization is execution-blocked (flag off OR on) — condition 11 still
    returns RW_REAL_ACCOUNT_NOT_ENABLED.
  * A LIVE account becomes able to PASS condition 11 only with the D4 flag on AND a valid, identity-matched,
    non-revoked authorization — and passing 11 is NECESSARY, never SUFFICIENT (the rest of the conjunction
    still applies; here it then fails at workspace-missing, proving 11 was passed, not that an order flows).
  * DEMO EQUIVALENCE (hard merge criterion): a correctly-classified DEMO account's readiness decision is
    IDENTICAL with the D4 flag off and on — D4a never changes the DEMO path.
  * The validity policy is fail-closed: absent / revoked / identity-drifted / unpinned ⇒ not authorized.
"""
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution.models import LiveExecutionAuthorization
from execution.readiness import (
    PERSISTENT_WORKSPACE, PersistentWorkspaceProvider, RW_REAL_ACCOUNT_NOT_ENABLED, RW_WORKSPACE_MISSING)
from execution.live_authz import is_live_execution_authorized
from trading.models import TradingAccount, BrokerServer
from django.db import IntegrityError, transaction

U = get_user_model()

# Reach condition 11: the subsystem + execution flags on (so evaluate() passes conditions 1/2 and the
# lifecycle checks before condition 11). HOSTED_LIVE_EXECUTION_ENABLED varies per test.
_BASE = {"HOSTED_PERSISTENT_MT5_ENABLED": "1", "HOSTED_MT5_EXECUTION_ENABLED": "1"}
_D4_ON = {**_BASE, "HOSTED_LIVE_EXECUTION_ENABLED": "1"}


def _stub(account, **over):
    """A readiness stub carrying the REAL account id (so the DB authz query resolves) + no workspace."""
    base = dict(id=account.id, pk=account.id, readiness_provider=PERSISTENT_WORKSPACE, mt5_instance_id=None,
                is_active=True, disconnected_at=None, is_demo=account.is_demo,
                account_number=account.account_number, broker_server=account.broker_server,
                hosted_workspace=None)
    base.update(over)
    return SimpleNamespace(**base)


class LiveAuthzModelTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="d4a", email="d4a@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="L", account_number="L1", broker_name="B", is_demo=False)

    def test_at_most_one_active_authorization_per_account(self):
        LiveExecutionAuthorization.objects.create(trading_account=self.acct, user=self.user,
                                                  broker_identity_snapshot={"login": "L1", "server": ""})
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LiveExecutionAuthorization.objects.create(
                    trading_account=self.acct, user=self.user,
                    broker_identity_snapshot={"login": "L1", "server": ""})

    def test_revoked_row_frees_the_active_slot(self):
        a1 = LiveExecutionAuthorization.objects.create(trading_account=self.acct, user=self.user,
                                                       broker_identity_snapshot={"login": "L1", "server": ""})
        a1.is_active = False
        a1.revoked_at = timezone.now()
        a1.save(update_fields=["is_active", "revoked_at"])
        # a fresh active authorization is now allowed (revoked row retained as history)
        LiveExecutionAuthorization.objects.create(trading_account=self.acct, user=self.user,
                                                  broker_identity_snapshot={"login": "L1", "server": ""})
        self.assertEqual(LiveExecutionAuthorization.objects.filter(trading_account=self.acct).count(), 2)


class LiveAuthzPolicyTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="pol", email="pol@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="L", account_number="L1", broker_name="B", is_demo=False)

    def _authz(self, **over):
        data = dict(trading_account=self.acct, user=self.user,
                    broker_identity_snapshot={"login": "L1", "server": ""})
        data.update(over)
        return LiveExecutionAuthorization.objects.create(**data)

    def test_no_authorization_is_not_authorized(self):
        self.assertFalse(is_live_execution_authorized(self.acct))

    def test_active_matching_is_authorized(self):
        self._authz()
        self.assertTrue(is_live_execution_authorized(self.acct))

    def test_revoked_is_not_authorized(self):
        self._authz(is_active=False, revoked_at=timezone.now())
        self.assertFalse(is_live_execution_authorized(self.acct))

    def test_identity_drift_is_not_authorized(self):
        self._authz(broker_identity_snapshot={"login": "DIFFERENT", "server": ""})
        self.assertFalse(is_live_execution_authorized(self.acct))

    def test_unpinned_login_is_not_authorized(self):
        self.acct.account_number = ""
        self.acct.save(update_fields=["account_number"])
        self._authz(broker_identity_snapshot={"login": "", "server": ""})
        self.assertFalse(is_live_execution_authorized(self.acct))


class ReadinessConditionElevenTests(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="c11", email="c11@x.invalid", password="x")
        self.demo = TradingAccount.objects.create(
            user=self.user, name="D", account_number="D1", broker_name="B", is_demo=True)
        self.live = TradingAccount.objects.create(
            user=self.user, name="L", account_number="L1", broker_name="B", is_demo=False)

    def _reason(self, account, **flags):
        with override_settings(**flags):
            return PersistentWorkspaceProvider().evaluate(_stub(account)).reason_code

    # ---- DEMO EQUIVALENCE (hard merge criterion) ----
    def test_demo_reason_identical_flag_off_and_on(self):
        off = self._reason(self.demo, **_BASE)
        on = self._reason(self.demo, **_D4_ON)
        self.assertEqual(off, on)                       # D4a never changes the DEMO path
        self.assertEqual(off, RW_WORKSPACE_MISSING)     # demo passed condition 11 (as before), then no workspace

    # ---- LIVE stays blocked without a valid authorization ----
    def test_live_blocked_flag_off(self):
        self.assertEqual(self._reason(self.live, **_BASE), RW_REAL_ACCOUNT_NOT_ENABLED)

    def test_live_blocked_flag_on_without_authorization(self):
        self.assertEqual(self._reason(self.live, **_D4_ON), RW_REAL_ACCOUNT_NOT_ENABLED)

    def test_live_blocked_flag_on_with_revoked_authorization(self):
        LiveExecutionAuthorization.objects.create(
            trading_account=self.live, user=self.user, is_active=False, revoked_at=timezone.now(),
            broker_identity_snapshot={"login": "L1", "server": ""})
        self.assertEqual(self._reason(self.live, **_D4_ON), RW_REAL_ACCOUNT_NOT_ENABLED)

    # ---- LIVE passes condition 11 ONLY with flag + valid authorization (necessary, not sufficient) ----
    def test_live_passes_condition_11_with_flag_and_valid_authorization(self):
        LiveExecutionAuthorization.objects.create(
            trading_account=self.live, user=self.user,
            broker_identity_snapshot={"login": "L1", "server": ""})
        # Passes condition 11 → the NEXT gate (no workspace) fails → proves 11 was passed, NOT that an order flows.
        self.assertEqual(self._reason(self.live, **_D4_ON), RW_WORKSPACE_MISSING)

    def test_live_with_valid_authorization_still_blocked_when_flag_off(self):
        LiveExecutionAuthorization.objects.create(
            trading_account=self.live, user=self.user,
            broker_identity_snapshot={"login": "L1", "server": ""})
        self.assertEqual(self._reason(self.live, **_BASE), RW_REAL_ACCOUNT_NOT_ENABLED)   # DARK: flag off ⇒ blocked
