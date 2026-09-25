"""Phase C (Concurrent Broker Accounts) — entitlement model + override-aware resolver.

Proves: the new config-driven fields (account_mode / concurrent_broker_account_limit) exist with
conservative defaults (STANDARD / 1) so an unset user is unchanged; the concurrent limit is carried per
plan; and resolve_effective_entitlements layers ACTIVE, non-expired EntitlementOverride rows on top
(fixing the previously-dead override path) while ignoring inactive/expired/malformed overrides.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from admin_ops.models import EntitlementOverride
from billing.entitlements import (AccountMode, resolve_effective_entitlements, resolve_entitlements)
from billing.models import UserSubscriptionState

U = get_user_model()


def _state(user, plan, status=UserSubscriptionState.PlanStatus.ACTIVE):
    return UserSubscriptionState.objects.create(user=user, current_plan=plan, plan_status=status,
                                                viewer_mode=False)


def _override(user, capability, value, *, active=True, expires_in=timedelta(days=1)):
    return EntitlementOverride.objects.create(
        user=user, capability=capability, override_value=value, reason="test",
        is_active=active, expires_at=timezone.now() + expires_in)


class EntitlementModelDefaults(TestCase):
    def test_defaults_are_conservative(self):
        # No subscription row -> viewer -> STANDARD / 1 (unchanged, safe).
        ent = resolve_entitlements(None)
        self.assertEqual(ent.account_mode, AccountMode.STANDARD)
        self.assertEqual(ent.concurrent_broker_account_limit, 1)

    def test_concurrent_limit_carried_per_plan(self):
        cases = {UserSubscriptionState.Plan.STARTER_TRIAL: 1, UserSubscriptionState.Plan.STANDARD: 1,
                 UserSubscriptionState.Plan.PRO: 5, UserSubscriptionState.Plan.ADVANCED: 5,
                 UserSubscriptionState.Plan.BETA: 5}
        for plan, expect in cases.items():
            u = U.objects.create_user(username=f"u{plan}", email=f"{plan}@x.invalid", password="x")
            ent = resolve_entitlements(_state(u, plan))
            self.assertEqual(ent.concurrent_broker_account_limit, expect, plan)
            self.assertEqual(ent.account_mode, AccountMode.STANDARD)  # mode is opt-in per user, not per plan

    def test_to_dict_is_json_safe_with_new_fields(self):
        import json
        d = resolve_entitlements(None).to_dict()
        json.dumps(d)  # must not raise
        self.assertIn("concurrent_broker_account_limit", d)
        self.assertIn("account_mode", d)


class OverrideResolver(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="ov", email="ov@x.invalid", password="x")
        _state(self.user, UserSubscriptionState.Plan.PRO)  # base: max_trading_accounts=5, concurrent=5

    def test_no_override_is_base(self):
        ent = resolve_effective_entitlements(self.user)
        self.assertEqual(ent.max_trading_accounts, 5)
        self.assertEqual(ent.concurrent_broker_account_limit, 5)

    def test_numeric_override_raises_limit(self):
        _override(self.user, "max_trading_accounts", {"value": 20})
        _override(self.user, "concurrent_broker_account_limit", {"value": 10})
        ent = resolve_effective_entitlements(self.user)
        self.assertEqual(ent.max_trading_accounts, 20)          # previously-dead path now works
        self.assertEqual(ent.concurrent_broker_account_limit, 10)

    def test_account_mode_override(self):
        _override(self.user, "account_mode", {"value": "concurrent"})
        self.assertEqual(resolve_effective_entitlements(self.user).account_mode, AccountMode.CONCURRENT)

    def test_bool_override(self):
        _override(self.user, "can_deploy_automation", {"granted": False})
        self.assertFalse(resolve_effective_entitlements(self.user).can_deploy_automation)

    def test_inactive_and_expired_overrides_ignored(self):
        _override(self.user, "max_trading_accounts", {"value": 99}, active=False)
        _override(self.user, "concurrent_broker_account_limit", {"value": 99},
                  expires_in=timedelta(days=-1))  # already expired
        ent = resolve_effective_entitlements(self.user)
        self.assertEqual(ent.max_trading_accounts, 5)           # inactive ignored
        self.assertEqual(ent.concurrent_broker_account_limit, 5)  # expired ignored

    def test_malformed_and_unknown_overrides_ignored(self):
        _override(self.user, "max_trading_accounts", {"value": "not-an-int"})  # malformed -> ignored
        _override(self.user, "account_mode", {"value": "bogus"})               # invalid mode -> ignored
        _override(self.user, "not_a_real_capability", {"value": 1})            # unknown -> ignored
        ent = resolve_effective_entitlements(self.user)
        self.assertEqual(ent.max_trading_accounts, 5)
        self.assertEqual(ent.account_mode, AccountMode.STANDARD)
