"""P0 trade-sync freshness — the periodic breakeven position-sync must cover HOSTED per-tenant accounts.

Root cause of the observed ~1h lag: ``_ensure_position_sync`` resolved windows_username from the legacy
``mt5_instance`` (None for hosted accounts), so every hosted tenant was silently SKIPPED and only got the
hourly order-triggered auto-sync. Hosted accounts carry their Windows identity on ``AccountProvisioning``.
This widens the READ-ONLY sync only — the protection MODIFY path is deliberately unchanged.
"""
from __future__ import annotations

from django.test import TestCase

from execution.breakeven import _ensure_position_sync, _sync_windows_username, _windows_username
from execution.models import ExecutionJob, TerminalNode
from execution.tests_per_tenant_transport import _make_tenant
from trading.models import TradingAccount
from mt5.models import Mt5Instance
from django.contrib.auth import get_user_model


class SyncUsernameResolution(TestCase):
    def setUp(self):
        self.node = TerminalNode.objects.create(hostname="beta", rdp_host="100.79.101.19")

    def test_hosted_account_resolves_username_from_provisioning(self):
        acct, ws = _make_tenant("1302575", "guvfx_u_28", node=self.node)   # hosted: AccountProvisioning, no mt5_instance
        self.assertIsNone(_windows_username(acct))                          # legacy resolver: None
        self.assertEqual(_sync_windows_username(acct), "guvfx_u_28")        # hosted-aware: from provisioning

    def test_legacy_mt5_instance_still_preferred(self):
        u = get_user_model().objects.create_user(username="cz", email="cz@x.invalid", password="x")
        inst = Mt5Instance.objects.create(hostname="cz-host", windows_username="Administrator")
        cz = TradingAccount.objects.create(user=u, name="cz", account_number="1302561",
                                           broker_name="DemoBroker", mt5_instance=inst)
        self.assertEqual(_sync_windows_username(cz), "Administrator")       # legacy path unchanged

    def test_no_identity_returns_none(self):
        u = get_user_model().objects.create_user(username="x", email="x@x.invalid", password="x")
        bare = TradingAccount.objects.create(user=u, name="b", account_number="B1", broker_name="DemoBroker")
        self.assertIsNone(_sync_windows_username(bare))                     # fail-closed, never guesses

    def test_not_provisioned_profile_is_skipped(self):
        # A half-provisioned (not PROVISIONED) profile resolves to None -> clean skip, not repeating FAILED
        # syncs against a not-ready terminal (adversarial-review LOW).
        from terminal_provisioning.models import AccountProvisioning
        acct, ws = _make_tenant("1302599", "guvfx_u_99", node=self.node)
        AccountProvisioning.objects.filter(trading_account_id=acct.id).update(
            status=AccountProvisioning.Status.PENDING)
        self.assertIsNone(_sync_windows_username(acct))


class HostedPeriodicSyncCoverage(TestCase):
    def setUp(self):
        self.node = TerminalNode.objects.create(hostname="beta", rdp_host="100.79.101.19")

    def test_hosted_account_now_gets_a_periodic_sync(self):
        acct, ws = _make_tenant("1302575", "guvfx_u_28", node=self.node)
        queued = _ensure_position_sync({acct.id})
        self.assertEqual(queued, 1)                                         # was 0 (skipped) before the fix
        job = ExecutionJob.objects.get(account_id=acct.id, job_type=ExecutionJob.JobType.SYNC_POSITIONS)
        self.assertEqual(job.payload.get("windows_username"), "guvfx_u_28")  # its OWN identity
        self.assertEqual(job.terminal_node_id, self.node.id)
        self.assertTrue(job.payload.get("breakeven_sync"))

    def test_dedup_no_duplicate_when_one_pending(self):
        acct, ws = _make_tenant("1302575", "guvfx_u_28", node=self.node)
        self.assertEqual(_ensure_position_sync({acct.id}), 1)
        self.assertEqual(_ensure_position_sync({acct.id}), 0)              # single-flight: none while one pending
        self.assertEqual(ExecutionJob.objects.filter(account_id=acct.id).count(), 1)

    def test_two_hosted_tenants_get_their_OWN_sync_no_cross_routing(self):
        a, aws = _make_tenant("1302575", "guvfx_u_28", node=self.node)
        b, bws = _make_tenant("1302587", "guvfx_u_25", node=self.node)
        _ensure_position_sync({a.id, b.id})
        ja = ExecutionJob.objects.get(account_id=a.id, job_type=ExecutionJob.JobType.SYNC_POSITIONS)
        jb = ExecutionJob.objects.get(account_id=b.id, job_type=ExecutionJob.JobType.SYNC_POSITIONS)
        self.assertEqual(ja.payload.get("windows_username"), "guvfx_u_28")  # A's identity on A's job
        self.assertEqual(jb.payload.get("windows_username"), "guvfx_u_25")  # B's identity on B's job

    def test_account_with_no_identity_still_skipped(self):
        u = get_user_model().objects.create_user(username="n", email="n@x.invalid", password="x")
        bare = TradingAccount.objects.create(user=u, name="n", account_number="N1", broker_name="DemoBroker",
                                             terminal_node=self.node)
        self.assertEqual(_ensure_position_sync({bare.id}), 0)             # no username -> skip (unchanged)

    def test_protection_resolver_unchanged_for_hosted(self):
        # The protection MODIFY path uses _windows_username (NOT _sync_windows_username): still None for a
        # hosted account, so this fix changes NO execution semantics (protection stays as-is, out of scope).
        acct, ws = _make_tenant("1302575", "guvfx_u_28", node=self.node)
        self.assertIsNone(_windows_username(acct))
