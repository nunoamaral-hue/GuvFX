"""Phase C2 — beta runtime per-user cap re-scoped to the C1 concurrent-accounts entitlement (DARK).

Proves: while the enforcement flag is OFF the legacy hard cap of 1 active runtime/user is byte-identical;
while ON the per-user active cap follows the user's entitlement (STANDARD=1, CONCURRENT=configured/override),
a 6th active is denied, downgrade 5->1 denies NEW without killing existing, released runtimes free the cap,
the global pool cap is config-driven, and two users on the same pool never leak into each other's cap.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from admin_ops.models import EntitlementOverride
from billing.models import UserSubscriptionState
from terminal_provisioning import beta_capacity as cap
from terminal_provisioning.models import RuntimeState
from trading.models import TradingAccount

U = get_user_model()


def _user(n, *, plan=UserSubscriptionState.Plan.PRO):
    u = U.objects.create_user(username=f"c2u{n}", email=f"c2u{n}@x.invalid", password="x")
    UserSubscriptionState.objects.create(user=u, current_plan=plan,
                                         plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False)
    # Per-user enforcement scope: grant the per-user activation so ARMED classes (master ON) enforce this user.
    # DARK classes force the master OFF, so the grant is inert there (master kill wins).
    from trading.account_entitlement import grant_concurrent_enforcement
    grant_concurrent_enforcement(u)
    return u


def _override(user, capability, value):
    EntitlementOverride.objects.create(user=user, capability=capability, override_value=value, reason="t",
                                       is_active=True, expires_at=timezone.now() + timedelta(days=1))


def _concurrent(user, *, limit=None):
    _override(user, "account_mode", {"value": "concurrent"})
    if limit is not None:
        _override(user, "concurrent_broker_account_limit", {"value": limit})
    return user


def _acct(user, n):
    return TradingAccount.objects.create(user=user, name=f"A{n}", account_number=str(2000 + n),
                                         broker_name="B", is_demo=True)


@override_settings(BETA_RUNTIMES_ENABLED=True, CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=False)  # master KILL => DARK
class DarkPreservesLegacyPerUserOne(TestCase):
    """Flag OFF (default) — the per-user cap is byte-identical to the legacy hard-coded 1, even for a user
    whose entitlement WOULD allow 5 once armed."""

    def test_standard_effective_even_for_concurrent_entitled_user_while_dark(self):
        u = _concurrent(_user(1), limit=5)   # would be 5 if armed
        cap.reserve_beta_slot(_acct(u, 1))
        with self.assertRaises(cap.CapacityError) as ctx:
            cap.reserve_beta_slot(_acct(u, 2))
        self.assertEqual(ctx.exception.reason_code, "per_user_runtime_cap")   # still 1 while DARK


@override_settings(BETA_RUNTIMES_ENABLED=True, CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=True,
                   BETA_MAX_ACTIVE_RUNTIMES=20)
class ArmedPerUserEntitlementCap(TestCase):
    def test_standard_user_capped_at_one(self):
        u = _user(1)   # PRO plan, default account_mode STANDARD => concurrent limit 1
        cap.reserve_beta_slot(_acct(u, 1))
        with self.assertRaises(cap.CapacityError) as ctx:
            cap.reserve_beta_slot(_acct(u, 2))
        self.assertEqual(ctx.exception.reason_code, "per_user_runtime_cap")

    def test_concurrent_user_up_to_five_then_sixth_denied(self):
        u = _concurrent(_user(1))    # PRO => concurrent_broker_account_limit 5
        for i in range(5):
            cap.reserve_beta_slot(_acct(u, i))
        self.assertEqual(cap.active_beta_runtime_count_for_user(u), 5)
        with self.assertRaises(cap.CapacityError) as ctx:
            cap.reserve_beta_slot(_acct(u, 5))          # attempted 6th active
        self.assertEqual(ctx.exception.reason_code, "per_user_runtime_cap")
        self.assertEqual(cap.active_beta_runtime_count_for_user(u), 5)

    def test_override_raises_limit_beyond_five(self):
        u = _concurrent(_user(1), limit=8)
        for i in range(6):
            cap.reserve_beta_slot(_acct(u, i))
        self.assertEqual(cap.active_beta_runtime_count_for_user(u), 6)   # 6 <= 8 override

    def test_downgrade_five_to_one_denies_new_but_keeps_existing(self):
        u = _concurrent(_user(1))
        for i in range(5):
            cap.reserve_beta_slot(_acct(u, i))
        # Downgrade to STANDARD (remove the concurrent override) => effective per-user cap becomes 1.
        EntitlementOverride.objects.filter(user=u, capability="account_mode").update(is_active=False)
        with self.assertRaises(cap.CapacityError) as ctx:
            cap.reserve_beta_slot(_acct(u, 9))          # a NEW reservation is denied at the lowered cap
        self.assertEqual(ctx.exception.reason_code, "per_user_runtime_cap")
        self.assertEqual(cap.active_beta_runtime_count_for_user(u), 5)   # existing NOT silently killed

    def test_released_runtime_frees_the_cap(self):
        u = _concurrent(_user(1))
        rt = cap.reserve_beta_slot(_acct(u, 1))
        cap.release_beta_slot(rt)                        # STOPPED -> releases the slot
        self.assertEqual(cap.active_beta_runtime_count_for_user(u), 0)
        cap.reserve_beta_slot(_acct(u, 2))              # can reserve again
        self.assertEqual(cap.active_beta_runtime_count_for_user(u), 1)

    def test_two_users_different_limits_capped_from_their_own_entitlement(self):
        # DIFFERENT per-user limits (A=2, B=5) so the test detects a cap sourced from the WRONG user: A must
        # be denied at 2 while B reaches 5, proving _per_user_max_active_runtimes keys on account.user.
        a = _concurrent(_user(1), limit=2)
        b = _concurrent(_user(2), limit=5)
        for i in range(2):
            cap.reserve_beta_slot(_acct(a, i))
        with self.assertRaises(cap.CapacityError) as ctx:            # A hits ITS limit of 2
            cap.reserve_beta_slot(_acct(a, 9))
        self.assertEqual(ctx.exception.reason_code, "per_user_runtime_cap")
        for i in range(5):
            cap.reserve_beta_slot(_acct(b, 10 + i))                  # B reaches ITS limit of 5, unaffected by A
        self.assertEqual(cap.active_beta_runtime_count_for_user(a), 2)
        self.assertEqual(cap.active_beta_runtime_count_for_user(b), 5)
        self.assertEqual(cap.active_beta_runtime_count(), 7)          # no cross-user leakage in the counts

    def test_sequential_second_at_limit_is_blocked_holding_no_slot(self):
        # Sequential (not concurrent) proof that a denial writes the truthful BLOCKED state and holds no slot.
        # True concurrency is proven separately in ArmedConcurrencyRace below (real threads).
        u = _user(1)   # STANDARD => 1
        cap.reserve_beta_slot(_acct(u, 1))
        loser = _acct(u, 2)
        with self.assertRaises(cap.CapacityError):
            cap.reserve_beta_slot(loser)
        loser.runtime.refresh_from_db()
        self.assertEqual(loser.runtime.state, RuntimeState.BLOCKED)
        self.assertEqual(cap.active_beta_runtime_count_for_user(u), 1)


@override_settings(BETA_RUNTIMES_ENABLED=True, BETA_MAX_ACTIVE_RUNTIMES=2)
class GlobalPoolCapConfigDriven(TestCase):
    def test_global_cap_from_config(self):
        cap.reserve_beta_slot(_acct(_user(1), 1))
        cap.reserve_beta_slot(_acct(_user(2), 2))
        with self.assertRaises(cap.CapacityError) as ctx:
            cap.reserve_beta_slot(_acct(_user(3), 3))
        self.assertEqual(ctx.exception.reason_code, "beta_pool_full")   # config-lowered to 2


class EntryGateHonoursConfigDrivenPool(TestCase):
    """The entry-time admission gate (billing.beta.runtime_capacity_available) must read the SAME
    config-driven global cap as reserve_beta_slot, so a config pool-raise actually admits new customers
    (regression guard for the half-wired-cap finding)."""

    @override_settings(BETA_RUNTIMES_ENABLED=True, BETA_MAX_ACTIVE_RUNTIMES=2)
    def test_entry_gate_blocks_when_config_cap_reached(self):
        from billing.beta import runtime_capacity_available
        cap.reserve_beta_slot(_acct(_user(1), 1))
        cap.reserve_beta_slot(_acct(_user(2), 2))
        self.assertFalse(runtime_capacity_available())          # 2 held, config cap 2 → full at the entry gate

    @override_settings(BETA_RUNTIMES_ENABLED=True, BETA_MAX_ACTIVE_RUNTIMES=10)
    def test_entry_gate_admits_when_config_cap_raised(self):
        from billing.beta import runtime_capacity_available
        cap.reserve_beta_slot(_acct(_user(1), 1))
        cap.reserve_beta_slot(_acct(_user(2), 2))
        self.assertTrue(runtime_capacity_available())           # config-raised to 10 → entry gate admits


class ArmedConcurrencyRace(TransactionTestCase):
    """Real-threads proof (TransactionTestCase) that the entitlement-derived per-user cap is enforced
    ATOMICALLY under the BetaCapacityLock when ARMED: three concurrent reservations for a CONCURRENT user
    whose limit is 2 → exactly two win, one is denied (per_user_runtime_cap), never three."""

    @override_settings(BETA_RUNTIMES_ENABLED=True, CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=True,
                       BETA_MAX_ACTIVE_RUNTIMES=20)
    def test_three_concurrent_reservations_respect_armed_limit_of_two(self):
        import threading
        from django.db import connection
        u = _concurrent(_user(1), limit=2)
        accts = [_acct(u, i) for i in range(3)]
        for a in accts:
            cap.get_or_create_beta_runtime(a)   # pre-create so the race is purely on the armed per-user cap
        barrier = threading.Barrier(3)
        results = {}

        def worker(name, acct):
            barrier.wait()
            try:
                rt = cap.reserve_beta_slot(acct)
                results[name] = ("ok", rt.state)
            except cap.CapacityError as e:
                results[name] = ("denied", e.reason_code)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(f"t{i}", accts[i])) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        outcomes = sorted(v[0] for v in results.values())
        self.assertEqual(outcomes, ["denied", "ok", "ok"], f"expected exactly two winners; got {results}")
        self.assertEqual(cap.active_beta_runtime_count_for_user(u), 2)   # armed cap held under real concurrency
