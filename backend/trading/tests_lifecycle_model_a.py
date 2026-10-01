"""Model-A lifecycle + decommission credential-destruction correctness (Sponsor packet, 2026-10-01).

Adversarial coverage of the approved invariant:
    REMOVE -> tombstone (history retained) -> physical decommission (credentials destroyed, entitlement released)
    then later ADD SAME broker/login/server -> a BRAND-NEW independent lifecycle instance
    -> no revive-in-place, no collision with the old cleanup, no stale-credential/runtime inheritance,
       entitlement consumed exactly once.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from billing.models import UserSubscriptionState
from terminal_provisioning.models import AccountProvisioning
from trading.account_removal import remove_account, destroy_runtime_provisioning_credential
from trading.models import TradingAccount, Trade
from trading.views import TradingAccountViewSet

U = get_user_model()


def _user(name):
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.create(user=u, current_plan=UserSubscriptionState.Plan.BETA,
                                         plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False)
    return u


def _add(user, number, broker="B"):
    req = APIRequestFactory().post("/api/trading/accounts/",
                                   {"name": "A", "account_number": number, "broker_name": broker,
                                    "password": "pw", "is_demo": True}, format="json")
    force_authenticate(req, user=user)
    return TradingAccountViewSet.as_view({"post": "create"})(req)


def _acct(user, number, **kw):
    return TradingAccount.objects.create(user=user, name="A", account_number=number, broker_name="B",
                                         is_demo=True, **kw)


def _prov(account, suffix, *, cred="runtime-cred", status=AccountProvisioning.Status.PROVISIONED):
    return AccountProvisioning.objects.create(
        trading_account=account, windows_username=f"guvfx_u_{suffix}", password_enc=cred,
        is_admin=False, runtime_root=rf"C:\GuvFX\accounts\{suffix}", status=status)


class ModelALifecycleTests(TestCase):
    def setUp(self):
        self.user = _user("mdla")

    def _active(self, number):
        return TradingAccount.objects.filter(user=self.user, account_number=number, disconnected_at__isnull=True)

    def _active_id(self, number):
        a = self._active(number).first()
        return a.id if a else None

    def test_remove_then_exact_readd_is_new_instance_tombstone_retained(self):   # scenario 1 + 7
        _add(self.user, "5001")
        first_id = self._active_id("5001")
        remove_account(TradingAccount.objects.get(id=first_id))
        _add(self.user, "5001")
        actives = self._active("5001")
        self.assertEqual(actives.count(), 1)                                      # exactly one active
        self.assertNotEqual(actives.first().id, first_id)                         # a NEW instance (new pk)
        self.assertTrue(TradingAccount.objects.filter(id=first_id,
                        disconnected_at__isnull=False).exists())                  # old tombstone retained
        self.assertEqual(TradingAccount.objects.filter(user=self.user, account_number="5001").count(), 2)

    def test_duplicate_concurrent_readd_yields_one_active(self):                  # scenario 3
        a = _acct(self.user, "5002"); remove_account(a)
        _add(self.user, "5002"); _add(self.user, "5002")                         # two re-adds
        self.assertEqual(self._active("5002").count(), 1)                         # only ONE active (2nd idempotent)

    def test_readd_safe_while_old_cleanup_running(self):                          # scenarios 2 + 4
        from hosted_workspace.decommission import CLEANUP_RUNNING
        from hosted_workspace.models import HostedMt5Workspace
        a = _acct(self.user, "5003")
        ws = HostedMt5Workspace.objects.create(trading_account=a)
        remove_account(a)
        HostedMt5Workspace.objects.filter(pk=ws.pk).update(cleanup_state=CLEANUP_RUNNING)  # teardown in-flight
        _add(self.user, "5003")
        self.assertEqual(self._active("5003").count(), 1)                         # new instance created safely
        a.refresh_from_db(); self.assertIsNotNone(a.disconnected_at)              # old tombstone untouched
        ws.refresh_from_db(); self.assertEqual(ws.cleanup_state, CLEANUP_RUNNING)  # old cleanup unaffected

    def test_history_remains_queryable_after_remove_and_readd(self):             # scenario 6
        a = _acct(self.user, "5004")
        Trade.objects.create(account=a, ticket="T1", symbol="XAUUSD", side="BUY", volume="0.01",
                             open_time=timezone.now(), close_time=timezone.now(), open_price="2000.00")
        remove_account(a); _add(self.user, "5004")
        self.assertTrue(Trade.objects.filter(account_id=a.id, ticket="T1").exists())  # old history retained

    def test_entitlement_consumed_exactly_once(self):                            # scenario 8
        from trading.account_entitlement import owned_account_count
        a = _acct(self.user, "5005")
        self.assertEqual(owned_account_count(self.user), 1)
        remove_account(a)
        self.assertEqual(owned_account_count(self.user), 0)                       # tombstone releases the slot
        _add(self.user, "5005")
        self.assertEqual(owned_account_count(self.user), 1)                       # new instance consumes exactly one


class DecommissionCredentialTests(TestCase):
    def setUp(self):
        self.user = _user("cred")

    def test_remove_destroys_both_credential_stores_and_retires_provisioning(self):   # scenario 5
        a = _acct(self.user, "6001", password_enc="customer-cred")
        prov = _prov(a, "6001")
        remove_account(a)
        a.refresh_from_db(); prov.refresh_from_db()
        self.assertFalse(a.password_enc)                                          # customer credential destroyed
        self.assertFalse(prov.password_enc)                                       # runtime credential destroyed
        self.assertEqual(prov.status, AccountProvisioning.Status.RETIRED)         # provisioning truthfully retired

    def test_reconcile_command_retires_historical_tombstone(self):
        from io import StringIO
        from django.core.management import call_command
        a = _acct(self.user, "6002", is_active=False, disconnected_at=timezone.now())   # pre-fix tombstone
        prov = _prov(a, "6002", cred="lingering-cred")
        call_command("reconcile_decommissioned_provisioning", "--apply", stdout=StringIO())
        prov.refresh_from_db()
        self.assertFalse(prov.password_enc)                                       # lingering credential destroyed
        self.assertEqual(prov.status, AccountProvisioning.Status.RETIRED)

    def test_reconcile_dry_run_changes_nothing(self):
        from io import StringIO
        from django.core.management import call_command
        a = _acct(self.user, "6004", is_active=False, disconnected_at=timezone.now())
        prov = _prov(a, "6004", cred="keep-me")
        call_command("reconcile_decommissioned_provisioning", stdout=StringIO())   # no --apply
        prov.refresh_from_db()
        self.assertTrue(prov.password_enc)                                        # untouched in dry-run
        self.assertEqual(prov.status, AccountProvisioning.Status.PROVISIONED)

    def test_reconcile_never_touches_active_account(self):
        from io import StringIO
        from django.core.management import call_command
        a = _acct(self.user, "6003", is_active=True)                              # ACTIVE, not tombstoned
        prov = _prov(a, "6003", cred="active-cred")
        call_command("reconcile_decommissioned_provisioning", "--apply", stdout=StringIO())
        prov.refresh_from_db()
        self.assertTrue(prov.password_enc)                                        # active credential UNTOUCHED
        self.assertEqual(prov.status, AccountProvisioning.Status.PROVISIONED)
