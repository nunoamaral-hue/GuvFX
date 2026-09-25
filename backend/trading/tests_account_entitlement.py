"""Phase C (Concurrent Broker Accounts) — per-user account entitlement helpers + DARK enforcement flag.

Proves: owned/active counts are tombstone-correct; effective limits follow account_mode; the add-account
and concurrent-active predicates fail closed at the config-driven limits (override-aware); and enforcement
is DARK by default (``CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED`` OFF).
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from admin_ops.models import EntitlementOverride
from billing.models import UserSubscriptionState
from trading.account_entitlement import (active_account_count, check_can_activate, check_can_add_account,
                                         effective_concurrent_limit, effective_owned_limit,
                                         enforcement_enabled, owned_account_count)
from billing.entitlements import resolve_effective_entitlements
from trading.models import TradingAccount

U = get_user_model()


def _user(name, plan=UserSubscriptionState.Plan.PRO):
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.create(user=u, current_plan=plan,
                                         plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False)
    return u


def _acct(user, number, *, is_active=False, disconnected=False):
    return TradingAccount.objects.create(
        user=user, name="A", account_number=number, broker_name="DemoBroker", is_demo=True,
        is_active=is_active, disconnected_at=(timezone.now() if disconnected else None))


class CountsAreTombstoneCorrect(TestCase):
    def setUp(self):
        self.u = _user("counts")

    def test_owned_excludes_disconnected(self):
        _acct(self.u, "1"); _acct(self.u, "2"); _acct(self.u, "3", disconnected=True)
        self.assertEqual(owned_account_count(self.u), 2)   # tombstoned row does not consume the owned cap

    def test_active_counts_only_active_non_tombstoned(self):
        _acct(self.u, "1", is_active=True); _acct(self.u, "2", is_active=True)
        _acct(self.u, "3", is_active=False); _acct(self.u, "4", is_active=True, disconnected=True)
        self.assertEqual(active_account_count(self.u), 2)


class EffectiveLimits(TestCase):
    def test_standard_concurrent_limit_is_one(self):
        ent = resolve_effective_entitlements(_user("std"))  # PRO plan, default mode STANDARD
        self.assertEqual(effective_owned_limit(ent), 5)
        self.assertEqual(effective_concurrent_limit(ent), 1)  # STANDARD => 1 regardless of the 5 limit

    def test_concurrent_mode_uses_configured_limit(self):
        u = _user("con")
        EntitlementOverride.objects.create(user=u, capability="account_mode",
                                           override_value={"value": "concurrent"}, reason="t",
                                           is_active=True, expires_at=timezone.now() + timedelta(days=1))
        ent = resolve_effective_entitlements(u)
        self.assertEqual(effective_concurrent_limit(ent), 5)


class AddAccountPredicate(TestCase):
    def test_raises_at_owned_limit(self):
        u = _user("add", plan=UserSubscriptionState.Plan.STANDARD)  # max_trading_accounts = 2
        _acct(u, "1"); _acct(u, "2")
        with self.assertRaises(ValidationError):
            check_can_add_account(u)

    def test_tombstoned_frees_a_slot(self):
        u = _user("add2", plan=UserSubscriptionState.Plan.STANDARD)  # limit 2
        _acct(u, "1"); _acct(u, "2", disconnected=True)             # only 1 owned
        check_can_add_account(u)  # must NOT raise

    def test_override_raises_limit(self):
        u = _user("add3", plan=UserSubscriptionState.Plan.STANDARD)  # base limit 2
        EntitlementOverride.objects.create(user=u, capability="max_trading_accounts",
                                           override_value={"value": 4}, reason="t",
                                           is_active=True, expires_at=timezone.now() + timedelta(days=1))
        _acct(u, "1"); _acct(u, "2"); _acct(u, "3")
        check_can_add_account(u)  # 3 < 4 override => allowed (dead-override path fixed)


class ConcurrentActivatePredicate(TestCase):
    def test_standard_blocks_second_active(self):
        u = _user("act")  # STANDARD default => concurrent limit 1
        _acct(u, "1", is_active=True)
        with self.assertRaises(ValidationError):
            check_can_activate(u)  # one already active; STANDARD allows only 1

    def test_concurrent_allows_up_to_limit(self):
        u = _user("act2")
        EntitlementOverride.objects.create(user=u, capability="account_mode",
                                           override_value={"value": "concurrent"}, reason="t",
                                           is_active=True, expires_at=timezone.now() + timedelta(days=1))
        for i in range(4):
            _acct(u, str(i), is_active=True)   # 4 active, limit 5
        check_can_activate(u)                  # 4 < 5 => allowed
        _acct(u, "5", is_active=True)          # now 5 active
        with self.assertRaises(ValidationError):
            check_can_activate(u)              # 5 >= 5 => blocked

    def test_exclude_account_id_not_double_counted(self):
        u = _user("act3")  # STANDARD, limit 1
        a = _acct(u, "1", is_active=True)
        check_can_activate(u, exclude_account_id=a.id)  # reactivating THE active account is fine


class EnforcementIsDarkByDefault(TestCase):
    def test_flag_off_by_default(self):
        self.assertFalse(enforcement_enabled())

    @override_settings(CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=True)
    def test_flag_honours_setting(self):
        self.assertTrue(enforcement_enabled())
