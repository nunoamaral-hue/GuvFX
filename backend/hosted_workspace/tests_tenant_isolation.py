"""hosted_workspace.tests_tenant_isolation — ADR-0043 Addendum B host-level co-residency guard.

Proves the fail-closed guard that keeps a NON-Customer-Zero hosted workspace off any TerminalNode serving
Customer Zero. Four surfaces:
  1. ``forbidden_execution_node_ids`` — both sources (DB-derived CZ-account node + configured rdp_host).
  2. ``assert_allocation_allowed`` — flag OFF no-op, CZ account exempt, non-CZ→CZ raises, non-CZ→clean ok.
  3. the execution-node single writer refuses a non-CZ→CZ binding (covering the management-command path) and
     mutates nothing; binds normally when the flag is OFF (zero behaviour change).
  4. the allocator skips a CZ node, fails closed with a DISTINCT reason when only a CZ node exists, and — flag
     OFF — binds the CZ node exactly as before.

Determinism note: Django ``TestCase`` does NOT reset DB sequences between tests, so an auto-created account id
cannot be assumed. "Who is Customer Zero" is therefore pinned per-test via ``mock.patch`` of
``customer_zero_account_ids`` rather than relying on an incidental id==1. Nothing here arms execution or
places an order.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from execution.hosted_provisioning import assign_workspace_execution_node
from execution.models import TerminalNode
from trading.models import TradingAccount

from hosted_workspace import provisioning as P
from hosted_workspace import provisioning_runner as R
from hosted_workspace import tenant_isolation as TI
from hosted_workspace.delivery_persistence import assign_workspace_node as assign_delivery_node
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from hosted_workspace.tests_provisioning import _FLAGS_ON, _node
from hosted_workspace.tests_provisioning_runner import _requested

U = get_user_model()
_CZ_HOST = "100.79.101.19"
_GUARD_ON = dict(HOSTED_TENANT_NODE_ISOLATION_ENABLED="1", HOSTED_BETA_FORBIDDEN_RDP_HOSTS=[_CZ_HOST])


def _no_cz():
    """Pin 'no account is Customer Zero' so the workspace under test is deterministically non-CZ (the guard is
    then never wrongly disabled by an incidental account id). Forbidden nodes come from the configured host."""
    return mock.patch.object(TI, "customer_zero_account_ids", return_value=frozenset())


def _acct(username, node=None):
    u = U.objects.create_user(username=username, email=f"{username}@x.invalid", password="x")
    return TradingAccount.objects.create(
        user=u, name="acct", account_number="700900", broker_name="Hosted", terminal_node=node)


class ForbiddenNodeIdsTests(TestCase):
    def test_empty_when_no_cz_and_no_config(self):
        TerminalNode.objects.create(hostname="n1", status=TerminalNode.Status.ACTIVE, rdp_host="10.0.0.1")
        with _no_cz():
            self.assertEqual(TI.forbidden_execution_node_ids(), set())

    def test_cz_account_bound_node_is_forbidden_db_source(self):
        # No settings config — the CZ node is discovered purely from the account binding (DB source).
        node = TerminalNode.objects.create(hostname="cz", status=TerminalNode.Status.ACTIVE, rdp_host=_CZ_HOST)
        acct = _acct("cz", node=node)
        with mock.patch.object(TI, "customer_zero_account_ids", return_value=frozenset({acct.id})):
            self.assertIn(node.id, TI.forbidden_execution_node_ids())

    @override_settings(HOSTED_BETA_FORBIDDEN_RDP_HOSTS=[_CZ_HOST])
    def test_configured_rdp_host_is_forbidden_config_source(self):
        node = TerminalNode.objects.create(hostname="cz", status=TerminalNode.Status.ACTIVE, rdp_host=_CZ_HOST)
        other = TerminalNode.objects.create(hostname="ok", status=TerminalNode.Status.ACTIVE, rdp_host="10.0.0.9")
        with _no_cz():
            forbidden = TI.forbidden_execution_node_ids()
        self.assertIn(node.id, forbidden)
        self.assertNotIn(other.id, forbidden)

    @override_settings(HOSTED_BETA_FORBIDDEN_RDP_HOSTS=["HOST.EXAMPLE.COM"])
    def test_configured_rdp_host_match_is_case_insensitive(self):
        # Review LOW #1: a config/stored case mismatch must not silently drop a forbidden host from the belt.
        node = TerminalNode.objects.create(hostname="cz", status=TerminalNode.Status.ACTIVE,
                                           rdp_host="host.example.com")
        with _no_cz():
            self.assertIn(node.id, TI.forbidden_execution_node_ids())

    @override_settings(**_FLAGS_ON)
    def test_forbidden_includes_cz_workspace_node_when_account_terminal_node_cleared(self):
        # Review finding #3: forbidden-set must track the AUTHORITATIVE hosted binding (workspace.execution_node),
        # not just account.terminal_node — which a legacy staff unassign clears while CZ's terminal keeps running.
        node = _node(hostname="czX", rdp_host="10.0.0.7", max_accounts=5)
        ws = _requested()
        ws.execution_node = node
        ws.save(update_fields=["execution_node"])
        ws.trading_account.terminal_node = None                       # account pointer cleared…
        ws.trading_account.save(update_fields=["terminal_node"])
        with mock.patch.object(TI, "customer_zero_account_ids", return_value=frozenset({ws.trading_account_id})):
            self.assertIn(node.id, TI.forbidden_execution_node_ids())  # …still forbidden via the workspace binding


class AssertAllocationAllowedTests(TestCase):
    # These use literal account ids (999 non-CZ, 1 CZ) against the canonical frozenset({1}); no DB rows, so no
    # sequence dependence.
    def setUp(self):
        self.cz = TerminalNode.objects.create(hostname="cz", status=TerminalNode.Status.ACTIVE, rdp_host=_CZ_HOST)
        self.ok = TerminalNode.objects.create(hostname="ok", status=TerminalNode.Status.ACTIVE, rdp_host="10.0.0.9")

    @override_settings(HOSTED_BETA_FORBIDDEN_RDP_HOSTS=[_CZ_HOST])
    def test_noop_when_flag_off(self):
        TI.assert_allocation_allowed(999, self.cz)   # flag OFF → must not raise even for non-CZ on CZ node

    @override_settings(**_GUARD_ON)
    def test_raises_non_cz_account_to_cz_node(self):
        with self.assertRaises(TI.CrossTenantCoResidencyError):
            TI.assert_allocation_allowed(999, self.cz)

    @override_settings(**_GUARD_ON)
    def test_cz_account_may_occupy_cz_node(self):
        TI.assert_allocation_allowed(min(TI.customer_zero_account_ids()), self.cz)   # CZ exempt → no raise

    @override_settings(**_GUARD_ON)
    def test_non_cz_account_to_clean_node_ok(self):
        TI.assert_allocation_allowed(999, self.ok)   # clean node → no raise


class SingleWriterGuardTests(TestCase):
    @override_settings(**dict(_FLAGS_ON, **_GUARD_ON))
    def test_single_writer_refuses_non_cz_to_cz_node_and_mutates_nothing(self):
        cz = TerminalNode.objects.create(hostname="cz", status=TerminalNode.Status.ACTIVE, rdp_host=_CZ_HOST)
        ws = _requested()                            # non-CZ workspace
        gen0 = ws.execution_binding_generation
        with _no_cz(), self.assertRaises(TI.CrossTenantCoResidencyError):
            assign_workspace_execution_node(ws.trading_account, cz, actor="test")
        ws.refresh_from_db()
        self.assertIsNone(ws.execution_node_id)                       # nothing bound
        self.assertEqual(ws.execution_binding_generation, gen0)       # no generation bump (raised before write)

    @override_settings(**dict(_FLAGS_ON, HOSTED_BETA_FORBIDDEN_RDP_HOSTS=[_CZ_HOST]))
    def test_single_writer_binds_when_flag_off(self):
        cz = TerminalNode.objects.create(hostname="cz", status=TerminalNode.Status.ACTIVE, rdp_host=_CZ_HOST)
        ws = _requested()
        with _no_cz():
            assign_workspace_execution_node(ws.trading_account, cz, actor="test")   # flag OFF → binds as before
        ws.refresh_from_db()
        self.assertEqual(ws.execution_node_id, cz.id)


class AllocationGuardTests(TestCase):
    @override_settings(**dict(_FLAGS_ON, **_GUARD_ON))
    def test_allocation_skips_cz_node_and_picks_clean_node(self):
        _node(hostname="cz", rdp_host=_CZ_HOST, max_accounts=5)        # lower id → first by id order, skipped
        good = _node(hostname="good", rdp_host="10.0.0.9", max_accounts=5)
        ws = _requested()
        with _no_cz():
            res = P.allocate_workspace_node(ws)
        self.assertTrue(res.ok, res.reason)
        ws.refresh_from_db()
        self.assertEqual(ws.execution_node_id, good.id)               # bound the clean node, NOT the CZ node

    @override_settings(**dict(_FLAGS_ON, **_GUARD_ON))
    def test_allocation_fails_closed_when_only_cz_node(self):
        _node(hostname="cz", rdp_host=_CZ_HOST, max_accounts=5)
        ws = _requested()
        with _no_cz():
            res = P.allocate_workspace_node(ws)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.ALLOC_CZ_NODE_FORBIDDEN)       # distinct signal: provision a non-CZ host
        ws.refresh_from_db()
        self.assertIsNone(ws.execution_node_id)
        self.assertEqual(str(ws.canonical_state), S.PROVISIONING)     # untouched → retry next cycle

    @override_settings(**dict(_FLAGS_ON, HOSTED_BETA_FORBIDDEN_RDP_HOSTS=[_CZ_HOST]))
    def test_flag_off_binds_cz_node_no_behaviour_change(self):
        cz = _node(hostname="cz", rdp_host=_CZ_HOST, max_accounts=5)
        ws = _requested()
        with _no_cz():
            res = P.allocate_workspace_node(ws)
        self.assertTrue(res.ok, res.reason)                          # guard OFF → original behaviour preserved
        ws.refresh_from_db()
        self.assertEqual(ws.execution_node_id, cz.id)

    @override_settings(**dict(_FLAGS_ON, **_GUARD_ON))
    def test_runner_counts_cz_forbidden_distinctly_not_as_error(self):
        _node(hostname="cz", rdp_host=_CZ_HOST, max_accounts=5)       # only a CZ node available
        _requested()
        with _no_cz():
            out = R.run_workspace_provisioning()
        self.assertEqual(out["candidates"], 1)
        self.assertEqual(out["cz_forbidden"], 1)                     # distinct "waiting for non-CZ host" signal
        self.assertEqual(out["errors"], 0)                          # NOT counted as a system error
        self.assertEqual(out["allocated"], 0)


class DeliveryWriterGuardTests(TestCase):
    # The delivery host IS the interactive RemoteApp session host; the same co-residency guard applies (defence
    # in depth — today the only caller is the guarded allocator).
    @override_settings(**dict(_FLAGS_ON, **_GUARD_ON))
    def test_delivery_writer_refuses_non_cz_to_cz_node(self):
        cz = TerminalNode.objects.create(hostname="cz", status=TerminalNode.Status.ACTIVE, rdp_host=_CZ_HOST)
        ws = _requested()
        with _no_cz(), self.assertRaises(TI.CrossTenantCoResidencyError):
            assign_delivery_node(ws, cz)
        ws.refresh_from_db()
        self.assertIsNone(ws.workspace_node_id)                     # delivery binding not written

    @override_settings(**dict(_FLAGS_ON, HOSTED_BETA_FORBIDDEN_RDP_HOSTS=[_CZ_HOST]))
    def test_delivery_writer_binds_when_flag_off(self):
        cz = TerminalNode.objects.create(hostname="cz", status=TerminalNode.Status.ACTIVE, rdp_host=_CZ_HOST)
        ws = _requested()
        with _no_cz():
            self.assertTrue(assign_delivery_node(ws, cz))           # flag OFF → binds as before
        ws.refresh_from_db()
        self.assertEqual(ws.workspace_node_id, cz.id)


class ProvisionCommandGuardTests(TestCase):
    # Review findings #1/#2/#4: the provision_hosted_execution command wrote account.terminal_node BEFORE the
    # guarded writer with no wrapping transaction — a refusal left the account durably on the CZ node. The
    # command now pre-checks, so a refusal persists nothing and returns a clean CommandError.
    @override_settings(**dict(_FLAGS_ON, **_GUARD_ON))
    def test_command_refuses_cz_node_without_persisting_account_binding(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        TerminalNode.objects.create(hostname="czhost", status=TerminalNode.Status.ACTIVE, rdp_host=_CZ_HOST)
        acct = _acct("beta")                                        # non-CZ account, terminal_node=None
        with _no_cz(), self.assertRaises(CommandError):
            call_command("provision_hosted_execution", account_id=acct.id, node_hostname="czhost")
        acct.refresh_from_db()
        self.assertIsNone(acct.terminal_node_id)                   # nothing persisted on refusal
