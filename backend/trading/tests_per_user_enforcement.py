"""Phase C (Wayond POC) — PER-USER concurrent-account enforcement scope.

Adversarial matrix for the authoritative per-user activation gate: concurrency can be armed for ONE customer
(support@) without changing any other customer, the global flag is only a master kill, the limit is always
data/config-driven (never hard-coded), and enabling a user is inert (no order / terminal / strategy / sizing /
magic side effect). See trading/account_entitlement.py.
"""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from admin_ops.models import EntitlementOverride
from billing.models import UserSubscriptionState
from trading.account_entitlement import (
    check_can_activate, effective_concurrent_limit, enforcement_enabled, enforcement_master_enabled,
    grant_concurrent_enforcement, revoke_concurrent_enforcement, user_enforcement_enabled,
)
from billing.entitlements import resolve_effective_entitlements
from trading.models import TradingAccount

U = get_user_model()


def _user(name, *, plan=UserSubscriptionState.Plan.PRO):
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.create(user=u, current_plan=plan,
                                         plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False)
    return u


def _concurrent(u, *, limit=None):
    """Give a user CONCURRENT entitlement (mode + optional limit) via reversible EntitlementOverride."""
    EntitlementOverride.objects.create(user=u, capability="account_mode", override_value={"value": "concurrent"},
                                       reason="t", is_active=True, expires_at=timezone.now() + timedelta(days=1))
    if limit is not None:
        EntitlementOverride.objects.create(user=u, capability="concurrent_broker_account_limit",
                                           override_value={"value": limit}, reason="t", is_active=True,
                                           expires_at=timezone.now() + timedelta(days=1))
    return u


def _acct(u, n, *, is_active=False):
    return TradingAccount.objects.create(user=u, name="A", account_number=n, broker_name="B",
                                         is_active=is_active)


class PerUserActivation(TestCase):
    """(1) support@ enabled / another user disabled — one grant never leaks to another customer."""

    def test_one_user_enabled_others_unaffected(self):
        support, other = _user("support"), _user("other")
        self.assertFalse(enforcement_enabled(support))   # empty allowlist by default
        self.assertFalse(enforcement_enabled(other))
        grant_concurrent_enforcement(support)
        self.assertTrue(enforcement_enabled(support))    # only support@ is armed
        self.assertFalse(enforcement_enabled(other))     # every other customer untouched
        self.assertTrue(user_enforcement_enabled(support))
        self.assertFalse(user_enforcement_enabled(other))

    def test_no_arg_is_failsafe_false(self):
        # A legacy un-scoped call never enforces — there is no user to scope to.
        self.assertFalse(enforcement_enabled())
        self.assertFalse(enforcement_enabled(None))


class DifferentLimitsPerUser(TestCase):
    """(2) two users with different concurrent limits — limit comes from entitlement/config, not hard-coded."""

    def test_two_users_distinct_limits(self):
        a = _concurrent(_user("a"), limit=5)
        b = _concurrent(_user("b"), limit=20)
        grant_concurrent_enforcement(a)
        grant_concurrent_enforcement(b)
        self.assertEqual(effective_concurrent_limit(resolve_effective_entitlements(a)), 5)
        self.assertEqual(effective_concurrent_limit(resolve_effective_entitlements(b)), 20)

    def test_five_to_twenty_is_a_data_change_only(self):
        u = _concurrent(_user("scale"), limit=5)
        grant_concurrent_enforcement(u)
        self.assertEqual(effective_concurrent_limit(resolve_effective_entitlements(u)), 5)
        # Raising the limit is a single data change — no code/redeploy.
        EntitlementOverride.objects.filter(user=u, capability="concurrent_broker_account_limit").update(
            override_value={"value": 20})
        self.assertEqual(effective_concurrent_limit(resolve_effective_entitlements(u)), 20)


class StandardUserUnaffected(TestCase):
    """(3) a STANDARD user is unaffected even when armed — the mode still yields concurrent limit 1."""

    def test_standard_user_limit_is_one_even_when_granted(self):
        u = _user("std")  # PRO plan, default STANDARD mode
        grant_concurrent_enforcement(u)
        self.assertTrue(enforcement_enabled(u))
        self.assertEqual(effective_concurrent_limit(resolve_effective_entitlements(u)), 1)
        _acct(u, "1", is_active=True)
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            check_can_activate(u)   # STANDARD → still only one active


class DisableRollback(TestCase):
    """(4) per-user disable rollback — deactivating the grant returns the user to legacy behaviour."""

    def test_revoke_disables(self):
        u = _user("roll")
        grant_concurrent_enforcement(u)
        self.assertTrue(enforcement_enabled(u))
        n = revoke_concurrent_enforcement(u)
        self.assertEqual(n, 1)
        self.assertFalse(enforcement_enabled(u))  # back to legacy, instantly

    def test_expired_grant_does_not_enable(self):
        u = _user("exp")
        EntitlementOverride.objects.create(user=u, capability="concurrent_accounts_enforcement",
                                           override_value={"granted": True}, reason="t", is_active=True,
                                           expires_at=timezone.now() - timedelta(days=1))  # already expired
        self.assertFalse(enforcement_enabled(u))

    def test_granted_false_does_not_enable(self):
        u = _user("gf")
        EntitlementOverride.objects.create(user=u, capability="concurrent_accounts_enforcement",
                                           override_value={"granted": False}, reason="t", is_active=True,
                                           expires_at=timezone.now() + timedelta(days=1))
        self.assertFalse(enforcement_enabled(u))


class GlobalMasterKill(TestCase):
    """(5) global master kill — an explicit off value disables enforcement for EVERYONE, even granted users."""

    def test_master_default_on(self):
        self.assertTrue(enforcement_master_enabled())  # unset ⇒ available

    @override_settings(CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=False)
    def test_master_off_kills_granted_user(self):
        u = _user("killed")
        grant_concurrent_enforcement(u)
        self.assertTrue(user_enforcement_enabled(u))     # the grant exists
        self.assertFalse(enforcement_master_enabled())   # but master is killed
        self.assertFalse(enforcement_enabled(u))         # ⇒ nobody enforced

    @override_settings(CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED="off")
    def test_master_off_string_kills(self):
        u = _user("killed2")
        grant_concurrent_enforcement(u)
        self.assertFalse(enforcement_enabled(u))


class ForgedOwnership(TestCase):
    """(8) forged user / account ownership — a grant is strictly per user object; missing/foreign users fail
    closed."""

    def test_grant_is_scoped_to_the_exact_user(self):
        a, b = _user("owna"), _user("ownb")
        grant_concurrent_enforcement(a)
        self.assertTrue(enforcement_enabled(a))
        self.assertFalse(enforcement_enabled(b))  # b cannot inherit a's grant

    def test_unsaved_or_none_user_fails_closed(self):
        self.assertFalse(user_enforcement_enabled(None))
        self.assertFalse(user_enforcement_enabled(U()))  # unsaved (no pk)


class ActivationIsInert(TestCase):
    """(9) enabling support@ must place zero orders, restart zero terminals, and change zero existing
    strategy / sizing / magic state — the grant only flips the gate."""

    def test_grant_changes_no_trading_or_strategy_state(self):
        from strategies.models import Strategy, StrategyAssignment
        from trading.models import Trade
        u = _concurrent(_user("inert"), limit=5)
        a = _acct(u, "1302587", is_active=True)
        strat = Strategy.objects.create(name="S", owner=u)
        asn = StrategyAssignment.objects.create(account=a, strategy=strat, magic_number=1000000010,
                                                is_active=True)
        # ``risk_per_trade_override_pct`` must be a REAL field (guards against a getattr(...,None) tautology).
        self.assertIn("risk_per_trade_override_pct", [f.name for f in StrategyAssignment._meta.get_fields()])
        trades_before = Trade.objects.count()
        overrides_before = EntitlementOverride.objects.filter(user=u).count()
        acct_before = {"is_active": a.is_active, "number": a.account_number, "server_id": a.broker_server_id}
        asn_before = {"magic": asn.magic_number, "active": asn.is_active,
                      "risk": asn.risk_per_trade_override_pct}

        grant_concurrent_enforcement(u)   # THE activation mutation

        self.assertTrue(enforcement_enabled(u))
        a.refresh_from_db(); asn.refresh_from_db()
        # Zero side effects on trading/strategy/magic/sizing state:
        self.assertEqual({"is_active": a.is_active, "number": a.account_number,
                          "server_id": a.broker_server_id}, acct_before)
        self.assertEqual({"magic": asn.magic_number, "active": asn.is_active,
                          "risk": asn.risk_per_trade_override_pct}, asn_before)
        self.assertEqual(Trade.objects.count(), trades_before)  # no order manufactured
        # The grant inserts EXACTLY ONE EntitlementOverride (the enforcement grant) — nothing else.
        self.assertEqual(EntitlementOverride.objects.filter(user=u).count(), overrides_before + 1)
        self.assertEqual(EntitlementOverride.objects.filter(
            user=u, capability="concurrent_accounts_enforcement", is_active=True).count(), 1)

    def test_grant_is_idempotent_no_second_row_no_crash(self):
        # D1 regression: a repeat grant must NOT raise (partial-unique constraint) and must NOT stack rows.
        u = _user("idem")
        grant_concurrent_enforcement(u)
        grant_concurrent_enforcement(u)   # would IntegrityError if not idempotent
        self.assertEqual(EntitlementOverride.objects.filter(
            user=u, capability="concurrent_accounts_enforcement", is_active=True).count(), 1)
        self.assertTrue(enforcement_enabled(u))


class DowngradeFailsClosed(TestCase):
    """(7) downgrade while too many accounts active → fail closed (existing actives kept, new ones refused)."""

    def test_downgrade_refuses_new_but_keeps_existing(self):
        from rest_framework.exceptions import ValidationError
        u = _concurrent(_user("down"), limit=5)
        grant_concurrent_enforcement(u)
        for i in range(5):
            _acct(u, str(i), is_active=True)   # 5 active at limit 5
        # Downgrade the concurrent limit to 1 (data change).
        EntitlementOverride.objects.filter(user=u, capability="concurrent_broker_account_limit").update(
            override_value={"value": 1})
        # Existing 5 are NOT silently killed; a NEW activation is refused (fail closed).
        self.assertEqual(TradingAccount.objects.filter(user=u, is_active=True).count(), 5)
        with self.assertRaises(ValidationError):
            check_can_activate(u)


class FinalSlotRacePerUser(TransactionTestCase):
    """(6) concurrent final-slot race — two simultaneous activations of the last slot: exactly one wins.

    Drives the REAL ``TradingAccountViewSet.set_active`` view concurrently (not a hand-rolled copy), so a
    regression that dropped the ``select_for_update`` user-row lock in views.py would fail this test."""

    def test_two_racers_one_final_slot_via_real_view(self):
        from unittest import mock
        from rest_framework.test import APIRequestFactory, force_authenticate
        from trading.views import TradingAccountViewSet

        u = _concurrent(_user("race"), limit=2)
        grant_concurrent_enforcement(u)
        _acct(u, "existing", is_active=True)  # 1 active; limit 2 ⇒ exactly ONE more may activate
        b = _acct(u, "B", is_active=False)    # hosted (mt5_instance=None) → runtime-ready branch
        c = _acct(u, "C", is_active=False)

        view = TradingAccountViewSet.as_view({"post": "set_active"})
        results = {}

        def activate(acc, key):
            try:
                req = APIRequestFactory().post(f"/api/trading/accounts/{acc.id}/set-active/",
                                               {"is_active": True}, format="json")
                force_authenticate(req, user=u)
                resp = view(req, pk=acc.id)
                results[key] = resp.status_code
            finally:
                connection.close()

        # Hosted accounts are runtime-ready; patch is module-level so both worker threads see it.
        with mock.patch("trading.views._account_runtime_ready", return_value=True):
            with ThreadPoolExecutor(max_workers=2) as ex:
                f1 = ex.submit(activate, b, "b"); f2 = ex.submit(activate, c, "c")
                f1.result(); f2.result()

        # Exactly one racer wins the final slot (200); the other is refused (409). Cap never exceeded.
        self.assertEqual(sorted(results.values()), [200, 409], results)
        self.assertEqual(TradingAccount.objects.filter(user=u, is_active=True).count(), 2)
