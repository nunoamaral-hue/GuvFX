"""Item 3 — physical-capacity resource-leak fix.

A SUCCESSFUL physical decommission must RELEASE the workspace's node slot (execution_node + workspace_node) and
RETIRE its endpoint, so a removed account stops consuming allocatable node capacity. The allocator count
defensively excludes SUCCEEDED-decommissioned workspaces; a FAILED/in-progress cleanup is NEVER prematurely freed
(cross-tenant collision safety); reclamation is idempotent + concurrency-safe; and a freed slot becomes reusable.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution.models import HostedExecutionEndpoint, TerminalNode
from hosted_workspace.decommission import (CLEANUP_FAILED_RETRYABLE, CLEANUP_RUNNING, CLEANUP_SUCCEEDED,
                                           reclaim_decommissioned_allocation)
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.provisioning import _node_has_capacity, node_occupant_count
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from trading.models import TradingAccount

U = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "7%05d" % _n


def _node(max_accounts=2):
    return TerminalNode.objects.create(hostname="cap-%s" % _uniq(), status=TerminalNode.Status.ACTIVE,
                                       rdp_host="100.0.0.9", max_accounts=max_accounts)


def _acct(node, *, tombstoned=False):
    login = _uniq()
    u = U.objects.create_user(username="cap%s" % login, email="%s@x.invalid" % login, password="x")
    return TradingAccount.objects.create(
        user=u, name="a", broker_name="B", account_number=login, is_demo=True,
        is_active=not tombstoned, terminal_node=node,
        disconnected_at=(timezone.now() if tombstoned else None))


def _ws(a, node, *, cleanup_state="NOT_REQUIRED"):
    return HostedMt5Workspace.objects.create(
        trading_account=a, execution_node=node, workspace_node=node,
        canonical_state=S.EXECUTION_READY, cleanup_state=cleanup_state)


def _endpoint(ws, a, node, *, state=HostedExecutionEndpoint.State.READY):
    return HostedExecutionEndpoint.objects.create(
        workspace=ws, trading_account=a, terminal_node=node, host="h", port=int(_uniq()[-4:]) + 8000,
        base_url="http://h:8800", windows_username="guvfx_u_%s" % a.id, runtime_path="C:/x",
        workspace_uuid=ws.workspace_uuid, state=state)


class ReclaimTests(TestCase):
    def test_succeeded_decommission_releases_node_and_retires_endpoint(self):
        n = _node()
        a = _acct(n, tombstoned=True)
        ws = _ws(a, n, cleanup_state=CLEANUP_SUCCEEDED)
        ep = _endpoint(ws, a, n, state=HostedExecutionEndpoint.State.READY)
        out = reclaim_decommissioned_allocation(ws.pk)
        self.assertEqual(out, {"node_released": 1, "endpoint_retired": 1})
        ws.refresh_from_db(); ep.refresh_from_db()
        self.assertIsNone(ws.execution_node_id)
        self.assertIsNone(ws.workspace_node_id)
        self.assertEqual(ep.state, HostedExecutionEndpoint.State.RETIRED)

    def test_reclaim_is_idempotent(self):
        n = _node(); a = _acct(n, tombstoned=True)
        ws = _ws(a, n, cleanup_state=CLEANUP_SUCCEEDED); _endpoint(ws, a, n)
        reclaim_decommissioned_allocation(ws.pk)
        again = reclaim_decommissioned_allocation(ws.pk)
        self.assertEqual(again, {"node_released": 0, "endpoint_retired": 0})

    def test_running_cleanup_is_not_released(self):
        n = _node(); a = _acct(n, tombstoned=True)
        ws = _ws(a, n, cleanup_state=CLEANUP_RUNNING)
        ep = _endpoint(ws, a, n, state=HostedExecutionEndpoint.State.READY)
        out = reclaim_decommissioned_allocation(ws.pk)
        self.assertEqual(out, {"node_released": 0, "endpoint_retired": 0})
        ws.refresh_from_db(); ep.refresh_from_db()
        self.assertEqual(ws.execution_node_id, n.pk)                 # still owns the slot — no premature free
        self.assertEqual(ep.state, HostedExecutionEndpoint.State.READY)

    def test_failed_cleanup_is_not_released(self):
        n = _node(); a = _acct(n, tombstoned=True)
        ws = _ws(a, n, cleanup_state=CLEANUP_FAILED_RETRYABLE)
        out = reclaim_decommissioned_allocation(ws.pk)
        self.assertEqual(out["node_released"], 0)
        ws.refresh_from_db()
        self.assertEqual(ws.execution_node_id, n.pk)

    def test_occupant_count_excludes_succeeded_but_counts_in_progress(self):
        n = _node(max_accounts=2)
        a_live = _acct(n); _ws(a_live, n, cleanup_state="NOT_REQUIRED")                 # counts
        a_succ = _acct(n, tombstoned=True); _ws(a_succ, n, cleanup_state=CLEANUP_SUCCEEDED)  # excluded (leak)
        # Defensive: the SUCCEEDED workspace must NOT consume capacity even though its execution_node is still set.
        self.assertEqual(node_occupant_count(n), 1)
        self.assertTrue(_node_has_capacity(n))
        # But an in-progress (RUNNING) cleanup STILL owns the slot (no premature free → no cross-tenant collision).
        a_run = _acct(n, tombstoned=True); _ws(a_run, n, cleanup_state=CLEANUP_RUNNING)
        self.assertEqual(node_occupant_count(n), 2)                  # live + running; succeeded excluded
        self.assertFalse(_node_has_capacity(n))

    def test_freed_slot_is_reusable(self):
        n = _node(max_accounts=1)
        a = _acct(n, tombstoned=True)
        ws = _ws(a, n, cleanup_state=CLEANUP_SUCCEEDED)
        # Even before reclaim, the defensive count frees the slot; reclaim then also cleans the data.
        self.assertTrue(_node_has_capacity(n))
        reclaim_decommissioned_allocation(ws.pk)
        ws.refresh_from_db()
        self.assertIsNone(ws.execution_node_id)
        self.assertTrue(_node_has_capacity(n))
