"""Phase C2 — the one-workspace-per-user request funnel re-scoped to the C1 entitlement model (DARK).

Proves: while the enforcement flag is OFF a user keeps exactly one workspace (legacy, byte-identical);
while ON a user may hold up to their OWNED-account entitlement of workspaces, a re-request for the SAME
broker identity is idempotent, a NEW identity beyond the owned limit is refused (REQ_LIMIT_REACHED),
tombstoned accounts free an owned slot, and one user's request can NEVER return another user's workspace.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from admin_ops.models import EntitlementOverride
from billing.models import UserSubscriptionState
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.provisioning import REQ_EXISTS, REQ_LIMIT_REACHED, request_hosted_workspace

U = get_user_model()
_FLAGS_ON = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_WORKSPACE_ONBOARDING_ENABLED="1")


def _user(name):
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.update_or_create(
        user=u, defaults=dict(current_plan=UserSubscriptionState.Plan.BETA,  # beta: max_trading_accounts 10
                              plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False))
    return u


def _ws_count(user):
    return HostedMt5Workspace.objects.filter(trading_account__user=user).count()


def _override(user, capability, value):
    EntitlementOverride.objects.create(user=user, capability=capability, override_value=value, reason="t",
                                       is_active=True, expires_at=timezone.now() + timedelta(days=1))


@override_settings(**_FLAGS_ON)   # admission passes; enforcement flag OFF (default) => DARK
class FunnelDarkOnePerUser(TestCase):
    def test_second_distinct_request_returns_the_first(self):
        u = _user("d1")
        r1 = request_hosted_workspace(u, expected_login="111", expected_server="IS6-Demo")
        self.assertTrue(r1.ok and r1.created)
        r2 = request_hosted_workspace(u, expected_login="222", expected_server="IS6-Demo")  # DIFFERENT login
        self.assertTrue(r2.ok)
        self.assertFalse(r2.created)                       # DARK: still one workspace per user
        self.assertEqual(r2.reason, REQ_EXISTS)
        self.assertEqual(r2.workspace.pk, r1.workspace.pk)
        self.assertEqual(_ws_count(u), 1)


@override_settings(CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=True, **_FLAGS_ON)
class FunnelArmedMultiWorkspace(TestCase):
    def test_distinct_identities_create_distinct_workspaces(self):
        u = _user("a1")
        r1 = request_hosted_workspace(u, expected_login="111", expected_server="IS6-Demo")
        r2 = request_hosted_workspace(u, expected_login="222", expected_server="IS6-Demo")
        self.assertTrue(r1.created and r2.created)
        self.assertNotEqual(r1.workspace.pk, r2.workspace.pk)
        self.assertEqual(_ws_count(u), 2)

    def test_same_identity_is_idempotent(self):
        u = _user("a2")
        r1 = request_hosted_workspace(u, expected_login="111", expected_server="IS6-Demo")
        r2 = request_hosted_workspace(u, expected_login="111", expected_server="IS6-Demo")  # SAME login+server
        self.assertTrue(r1.created)
        self.assertFalse(r2.created)
        self.assertEqual(r2.workspace.pk, r1.workspace.pk)
        self.assertEqual(_ws_count(u), 1)

    def test_same_login_different_server_is_a_distinct_identity(self):
        # Idempotency is keyed on (login, server), not login alone — the same login on two brokers is two
        # legitimate accounts, each getting its own workspace (regression guard for the login-only match bug).
        u = _user("a2b")
        r1 = request_hosted_workspace(u, expected_login="111", expected_server="IS6-Demo")
        r2 = request_hosted_workspace(u, expected_login="111", expected_server="PepperstoneUK-Demo")
        self.assertTrue(r1.created and r2.created)
        self.assertNotEqual(r1.workspace.pk, r2.workspace.pk)   # NOT collapsed to the first server's workspace
        self.assertEqual(_ws_count(u), 2)

    def test_owned_limit_refuses_new_identity(self):
        u = _user("a3")
        _override(u, "max_trading_accounts", {"value": 2})   # owned cap 2
        request_hosted_workspace(u, expected_login="111", expected_server="IS6-Demo")
        request_hosted_workspace(u, expected_login="222", expected_server="IS6-Demo")
        r3 = request_hosted_workspace(u, expected_login="333", expected_server="IS6-Demo")   # 3rd > cap 2
        self.assertFalse(r3.ok)
        self.assertEqual(r3.reason, REQ_LIMIT_REACHED)
        self.assertEqual(_ws_count(u), 2)

    def test_tombstoned_account_frees_an_owned_slot(self):
        u = _user("a4")
        _override(u, "max_trading_accounts", {"value": 1})   # owned cap 1
        r1 = request_hosted_workspace(u, expected_login="111", expected_server="IS6-Demo")
        acct = r1.workspace.trading_account
        acct.disconnected_at = timezone.now()               # tombstone (retained row, excluded from owned count)
        acct.is_active = False
        acct.save(update_fields=["disconnected_at", "is_active"])
        r2 = request_hosted_workspace(u, expected_login="222", expected_server="IS6-Demo")
        self.assertTrue(r2.ok and r2.created)               # allowed: owned count is 0 (tombstone excluded)

    def test_cross_user_never_returns_other_users_workspace(self):
        a, b = _user("ua"), _user("ub")
        ra = request_hosted_workspace(a, expected_login="111", expected_server="IS6-Demo")
        rb = request_hosted_workspace(b, expected_login="111", expected_server="IS6-Demo")  # SAME login string
        self.assertTrue(ra.created and rb.created)
        self.assertNotEqual(ra.workspace.pk, rb.workspace.pk)
        self.assertEqual(ra.workspace.trading_account.user_id, a.pk)
        self.assertEqual(rb.workspace.trading_account.user_id, b.pk)   # B got B's, never A's
