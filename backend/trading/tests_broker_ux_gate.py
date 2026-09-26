"""Phase 9 — per-user Broker Accounts UX gate (customer-facing experience selection).

The new multi-account UX is shown ONLY to a per-user-granted customer (support@ in the POC); every other
customer stays on the legacy experience. Data-driven, reversible, fail-closed, and decoupled from concurrent
enforcement. See trading.account_entitlement (broker_accounts_ux capability).
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from admin_ops.models import EntitlementOverride
from billing.models import UserSubscriptionState
from trading.account_entitlement import (
    grant_broker_ux, revoke_broker_ux, user_broker_ux_enabled, user_enforcement_enabled,
    grant_concurrent_enforcement,
)

U = get_user_model()


def _user(name):
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.create(user=u, current_plan=UserSubscriptionState.Plan.PRO,
                                         plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False)
    return u


class BrokerUxGate(TestCase):
    def test_empty_allowlist_default_legacy(self):
        self.assertFalse(user_broker_ux_enabled(_user("a")))

    def test_grant_enables_only_that_user(self):
        a, b = _user("ux_a"), _user("ux_b")
        grant_broker_ux(a)
        self.assertTrue(user_broker_ux_enabled(a))
        self.assertFalse(user_broker_ux_enabled(b))   # never leaks to another customer

    def test_revoke_rolls_back(self):
        u = _user("ux_roll")
        grant_broker_ux(u)
        self.assertTrue(user_broker_ux_enabled(u))
        self.assertEqual(revoke_broker_ux(u), 1)
        self.assertFalse(user_broker_ux_enabled(u))

    def test_idempotent_grant_no_second_row(self):
        u = _user("ux_idem")
        grant_broker_ux(u); grant_broker_ux(u)
        self.assertEqual(EntitlementOverride.objects.filter(
            user=u, capability="broker_accounts_ux", is_active=True).count(), 1)

    def test_expired_and_granted_false_do_not_enable(self):
        u = _user("ux_exp")
        EntitlementOverride.objects.create(user=u, capability="broker_accounts_ux",
                                           override_value={"granted": True}, reason="t", is_active=True,
                                           expires_at=timezone.now() - timedelta(days=1))
        self.assertFalse(user_broker_ux_enabled(u))
        v = _user("ux_false")
        EntitlementOverride.objects.create(user=v, capability="broker_accounts_ux",
                                           override_value={"granted": False}, reason="t", is_active=True,
                                           expires_at=timezone.now() + timedelta(days=1))
        self.assertFalse(user_broker_ux_enabled(v))

    def test_none_or_unsaved_user_fails_closed(self):
        self.assertFalse(user_broker_ux_enabled(None))
        self.assertFalse(user_broker_ux_enabled(U()))

    def test_ux_gate_is_decoupled_from_enforcement(self):
        # UX and concurrent enforcement are independent per-user switches.
        u = _user("ux_decoup")
        grant_broker_ux(u)
        self.assertTrue(user_broker_ux_enabled(u))
        self.assertFalse(user_enforcement_enabled(u))   # UX granted, enforcement NOT
        w = _user("enf_only")
        grant_concurrent_enforcement(w)
        self.assertTrue(user_enforcement_enabled(w))
        self.assertFalse(user_broker_ux_enabled(w))     # enforcement granted, UX NOT
