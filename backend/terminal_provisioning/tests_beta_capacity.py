"""GFX-BETA-HEADLESS Increment 1 — runtime ownership + capacity service.

Covers compensating controls: canonical server-side paths (1/11/12), immutable owner/UUID/cohort
binding (3), one BETA runtime per account with server-set path + bridge id (2/4/6), atomic global
5-runtime + per-user-1 caps (13/14), kill switch (16), quarantine/stop (17), and PRODUCTION exclusion.
"""
import uuid

from django.test import TestCase, TransactionTestCase, override_settings
from django.contrib.auth import get_user_model

from trading.models import TradingAccount
from terminal_provisioning.models import AccountRuntime, RuntimeState
from terminal_provisioning import beta_capacity as cap
from terminal_provisioning import beta_paths

U = get_user_model()
ENABLED = override_settings(BETA_RUNTIMES_ENABLED=True)


def _user(n):
    return U.objects.create_user(username=f"u{n}", email=f"u{n}@x.invalid", password="x")


def _acct(user, n):
    # account_number is the MT5 login: a positive integer (offset so the range-0 loops stay valid).
    return TradingAccount.objects.create(user=user, name=f"A{n}", account_number=str(1000 + n),
                                         broker_name="B", is_demo=True)


class CanonicalPathTests(TestCase):
    def test_path_is_server_generated_from_uuid(self):
        u = uuid.uuid4()
        root = beta_paths.canonical_beta_runtime_root(u)
        self.assertTrue(root.endswith(f"\\{u}\\terminal"))
        self.assertIn(r"\beta\accounts", root)

    def test_non_uuid_input_is_rejected(self):
        # traversal / injection attempts are not valid UUIDs → fail closed
        for bad in ["../../etc", r"..\\..\\windows", "1; rm -rf", "", "not-a-uuid", "6/../7"]:
            with self.assertRaises(ValueError):
                beta_paths.canonical_beta_runtime_root(bad)

    def test_client_supplied_path_is_refused(self):
        with self.assertRaises(ValueError):
            beta_paths.assert_no_client_path(r"C:\attacker\path")
        beta_paths.assert_no_client_path("")   # empty is fine (no client path)

    @override_settings(BETA_RUNTIME_BASE=r"D:\custom\beta")
    def test_base_is_configurable(self):
        u = uuid.uuid4()
        self.assertEqual(beta_paths.canonical_beta_runtime_root(u), rf"D:\custom\beta\{u}\terminal")


class BetaRuntimeOwnershipTests(TestCase):
    def setUp(self):
        self.user = _user(1)
        self.acct = _acct(self.user, 1)

    def test_get_or_create_sets_cohort_path_and_bridge(self):
        rt = cap.get_or_create_beta_runtime(self.acct)
        self.assertEqual(rt.cohort, AccountRuntime.Cohort.BETA)
        self.assertEqual(rt.runtime_root, beta_paths.canonical_beta_runtime_root(rt.runtime_uuid))
        self.assertTrue(rt.bridge_identity.startswith("brt-"))
        self.assertIsNotNone(rt.runtime_uuid)

    def test_owner_uuid_cohort_binding_is_immutable(self):
        rt = cap.get_or_create_beta_runtime(self.acct)
        # cohort
        rt.cohort = AccountRuntime.Cohort.PRODUCTION
        with self.assertRaises(ValueError):
            rt.save()
        rt.refresh_from_db()
        # uuid
        rt.runtime_uuid = uuid.uuid4()
        with self.assertRaises(ValueError):
            rt.save()
        rt.refresh_from_db()
        # owner account
        other = _acct(self.user, 99)
        rt.trading_account = other
        with self.assertRaises(ValueError):
            rt.save()

    def test_transitions_still_work_without_tripping_immutability(self):
        # record_transition uses update_fields that never touch the binding → no false positive.
        rt = cap.get_or_create_beta_runtime(self.acct)
        from terminal_provisioning.runtime_state import record_transition
        record_transition(rt, RuntimeState.QUEUED)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.QUEUED)

    def test_existing_production_runtime_is_not_converted(self):
        prod = AccountRuntime.objects.create(
            trading_account=self.acct, cohort=AccountRuntime.Cohort.PRODUCTION)
        rt = cap.get_or_create_beta_runtime(self.acct)
        self.assertEqual(rt.pk, prod.pk)
        self.assertEqual(rt.cohort, AccountRuntime.Cohort.PRODUCTION)
        self.assertEqual(rt.runtime_root, "")   # not given a beta path


class KillSwitchTests(TestCase):
    def setUp(self):
        self.acct = _acct(_user(1), 1)

    def test_default_closed_no_reservation(self):
        # BETA_RUNTIMES_ENABLED default off → kill switch engaged → nothing reserves
        self.assertFalse(cap.beta_runtimes_enabled())
        with self.assertRaises(cap.CapacityError) as ctx:
            cap.reserve_beta_slot(self.acct)
        self.assertEqual(ctx.exception.reason_code, "beta_runtimes_disabled")


@ENABLED
class BetaCapacityTests(TestCase):
    def test_reserve_moves_runtime_to_queued_and_holds_slot(self):
        acct = _acct(_user(1), 1)
        rt = cap.reserve_beta_slot(acct)
        self.assertEqual(rt.state, RuntimeState.QUEUED)
        self.assertEqual(cap.active_beta_runtime_count(), 1)

    def test_per_user_cap_of_one(self):
        user = _user(1)
        a1, a2 = _acct(user, 1), _acct(user, 2)
        cap.reserve_beta_slot(a1)
        with self.assertRaises(cap.CapacityError) as ctx:
            cap.reserve_beta_slot(a2)
        self.assertEqual(ctx.exception.reason_code, "per_user_runtime_cap")
        # the rejected account's runtime shows the truthful BLOCKED state, holding no slot
        a2.runtime.refresh_from_db()
        self.assertEqual(a2.runtime.state, RuntimeState.BLOCKED)
        self.assertEqual(cap.active_beta_runtime_count(), 1)

    def test_global_cap_of_five(self):
        # 5 users each reserve 1 → pool full; a 6th user is rejected
        for i in range(5):
            cap.reserve_beta_slot(_acct(_user(i), i))
        self.assertEqual(cap.active_beta_runtime_count(), 5)
        sixth = _acct(_user(99), 99)
        with self.assertRaises(cap.CapacityError) as ctx:
            cap.reserve_beta_slot(sixth)
        self.assertEqual(ctx.exception.reason_code, "beta_pool_full")
        self.assertEqual(cap.active_beta_runtime_count(), 5)

    def test_production_runtimes_excluded_from_beta_count(self):
        # A production runtime holding RUNNING must NOT consume a beta slot.
        pacct = _acct(_user(50), 50)
        AccountRuntime.objects.create(
            trading_account=pacct, cohort=AccountRuntime.Cohort.PRODUCTION,
            state=RuntimeState.RUNNING)
        self.assertEqual(cap.active_beta_runtime_count(), 0)
        # 5 beta users still fit
        for i in range(5):
            cap.reserve_beta_slot(_acct(_user(i), i))
        self.assertEqual(cap.active_beta_runtime_count(), 5)

    def test_reserve_is_idempotent(self):
        acct = _acct(_user(1), 1)
        r1 = cap.reserve_beta_slot(acct)
        r2 = cap.reserve_beta_slot(acct)
        self.assertEqual(r1.pk, r2.pk)
        self.assertEqual(cap.active_beta_runtime_count(), 1)

    def test_reserve_of_held_runtime_with_now_invalid_record_stays_held(self):
        # A slot-holding runtime must NOT be demoted to BLOCKED (nor lose its slot) by a re-reservation,
        # even if the account record has since become malformed. Broker-record validation runs only for
        # a NOT-yet-held runtime — the idempotency check returns first for a held one.
        acct = _acct(_user(1), 1)
        rt = cap.reserve_beta_slot(acct)
        self.assertEqual(rt.state, RuntimeState.QUEUED)
        acct.account_number = "not-a-number"   # record is now MT5-invalid
        acct.save(update_fields=["account_number"])
        rt2 = cap.reserve_beta_slot(acct)      # re-reservation of the held runtime
        self.assertEqual(rt2.state, RuntimeState.QUEUED)          # still HELD, not BLOCKED
        self.assertEqual(cap.active_beta_runtime_count(), 1)      # slot not released

    def test_malformed_record_blocks_and_does_not_consume_a_slot(self):
        acct = _acct(_user(1), 1)
        acct.account_number = "not-a-number"
        acct.save(update_fields=["account_number"])
        with self.assertRaises(cap.CapacityError) as ctx:
            cap.reserve_beta_slot(acct)
        self.assertEqual(ctx.exception.reason_code, "broker_record_invalid")
        rt = cap.get_or_create_beta_runtime(acct)
        self.assertEqual(rt.state, RuntimeState.BLOCKED)
        self.assertEqual(cap.active_beta_runtime_count(), 0)

    def test_production_account_with_invalid_record_rejected_as_not_beta(self):
        # A PRODUCTION account is rejected by the cohort guard BEFORE broker-record validation runs, so
        # a malformed record on Nuno's account never even reaches (or blocks via) the beta validator.
        pacct = _acct(_user(1), 1)
        AccountRuntime.objects.create(trading_account=pacct, cohort=AccountRuntime.Cohort.PRODUCTION)
        pacct.account_number = "not-a-number"
        pacct.save(update_fields=["account_number"])
        with self.assertRaises(cap.CapacityError) as ctx:
            cap.reserve_beta_slot(pacct)
        self.assertEqual(ctx.exception.reason_code, "not_a_beta_runtime")

    def test_release_frees_the_slot(self):
        acct = _acct(_user(1), 1)
        rt = cap.reserve_beta_slot(acct)
        cap.release_beta_slot(rt)
        rt.refresh_from_db()
        self.assertEqual(rt.state, RuntimeState.STOPPED)
        self.assertEqual(cap.active_beta_runtime_count(), 0)

    def test_quarantine_stops_and_blocks_reprovision(self):
        acct = _acct(_user(1), 1)
        rt = cap.reserve_beta_slot(acct)
        cap.quarantine_runtime(rt, reason="crash_storm")
        rt.refresh_from_db()
        self.assertTrue(rt.quarantined)
        self.assertEqual(rt.state, RuntimeState.STOPPED)
        self.assertEqual(cap.active_beta_runtime_count(), 0)
        # cannot re-reserve while quarantined
        with self.assertRaises(cap.CapacityError) as ctx:
            cap.reserve_beta_slot(acct)
        self.assertEqual(ctx.exception.reason_code, "runtime_quarantined")
        # clearing quarantine allows reservation again
        cap.clear_quarantine(rt)
        rt2 = cap.reserve_beta_slot(acct)
        self.assertEqual(rt2.state, RuntimeState.QUEUED)

    def test_host_pressure_probe_blocks_when_no_capacity(self):
        cap.register_host_capacity_probe(lambda: False)
        try:
            with self.assertRaises(cap.CapacityError) as ctx:
                cap.reserve_beta_slot(_acct(_user(1), 1))
            self.assertEqual(ctx.exception.reason_code, "host_at_capacity")
        finally:
            cap.register_host_capacity_probe(None)

    def test_host_probe_failure_fails_closed(self):
        def boom():
            raise RuntimeError("cannot read metrics")
        cap.register_host_capacity_probe(boom)
        try:
            self.assertFalse(cap.host_has_capacity())
        finally:
            cap.register_host_capacity_probe(None)

    def test_reserve_rejects_a_production_account(self):
        # control 14 — a PRODUCTION (Nuno) account can never pull a beta slot at the reserve entry point.
        pacct = _acct(_user(1), 1)
        AccountRuntime.objects.create(trading_account=pacct, cohort=AccountRuntime.Cohort.PRODUCTION)
        with self.assertRaises(cap.CapacityError) as ctx:
            cap.reserve_beta_slot(pacct)
        self.assertEqual(ctx.exception.reason_code, "not_a_beta_runtime")

    def test_mutators_refuse_production_runtime(self):
        # control 14 defense-in-depth — the exported mutators must refuse a PRODUCTION runtime.
        pacct = _acct(_user(2), 2)
        prod = AccountRuntime.objects.create(
            trading_account=pacct, cohort=AccountRuntime.Cohort.PRODUCTION,
            state=RuntimeState.RUNNING)
        for fn in (lambda: cap.release_beta_slot(prod),
                   lambda: cap.quarantine_runtime(prod, reason="x"),
                   lambda: cap.clear_quarantine(prod)):
            with self.assertRaises(ValueError):
                fn()
        prod.refresh_from_db()
        self.assertEqual(prod.state, RuntimeState.RUNNING)   # untouched
        self.assertFalse(prod.quarantined)


class BetaCapacityConcurrencyTests(TransactionTestCase):
    """Proves the BetaCapacityLock + select_for_update actually serialise concurrent reservations —
    the central atomicity claim. Uses real threads + connections (TransactionTestCase)."""

    @override_settings(BETA_RUNTIMES_ENABLED=True)
    def test_two_concurrent_reservations_same_user_only_one_wins(self):
        import threading
        from django.db import connection
        user = _user(1)
        a1, a2 = _acct(user, 1), _acct(user, 2)
        # pre-create both beta runtimes so the race is purely on the per-user cap (=1)
        cap.get_or_create_beta_runtime(a1)
        cap.get_or_create_beta_runtime(a2)
        barrier = threading.Barrier(2)
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

        t1 = threading.Thread(target=worker, args=("t1", a1))
        t2 = threading.Thread(target=worker, args=("t2", a2))
        t1.start(); t2.start(); t1.join(); t2.join()

        outcomes = sorted(v[0] for v in results.values())
        self.assertEqual(outcomes, ["denied", "ok"], f"expected exactly one winner; got {results}")
        self.assertEqual(cap.active_beta_runtime_count_for_user(user), 1)
