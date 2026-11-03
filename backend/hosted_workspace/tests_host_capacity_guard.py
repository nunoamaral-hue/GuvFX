"""ADR-0069 — physical-host-wide capacity guard tests.

Two layers:
  * UNIT — the pure helpers (host_occupant_count / host_capacity_ceiling / host_capacity_block): occupancy sums
    across co-resident logical nodes REGARDLESS of node status (a DRAINING/OFFLINE node still loads the box);
    ceiling read LIVE with None as the fail-closed signal; block refuses an uncertified host (blank rdp_host OR
    no row) and a host at its ceiling; a fresh same-host "node 3" with ample per-node room CANNOT bypass the host
    ceiling; Customer Zero counts toward the host total; ceiling=0 is a hard fail-closed stop.
  * INTEGRATION — the REAL allocate_workspace_node with the guard ON/OFF: it returns the right AllocResult reason,
    the DRAINING-co-resident under-count is closed end-to-end, guard OFF is unaffected, and a two-thread race on
    the real allocator cannot oversubscribe the certified ceiling.
Plus the set_host_capacity governed primitive (dry-run default, audit row, --safe preservation, validation).

Requires a real DB with row locking — the backend test DB is PostgreSQL.
"""
from __future__ import annotations

import threading
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from billing.models import UserSubscriptionState
from execution.models import HostCapacity, HostCapacityAudit, TerminalNode
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import provisioning as P
from hosted_workspace.flags import hosted_host_capacity_guard_enabled
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.provisioning import (ALLOC_HOST_CAPACITY_EXCEEDED, ALLOC_HOST_CAPACITY_UNCONFIGURED,
                                           host_capacity_block, host_capacity_ceiling, host_occupant_count)
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()
_n = 0

HOST = "10.9.9.9"
OTHER = "10.9.9.8"
_FLAGS_ON = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_WORKSPACE_ONBOARDING_ENABLED="1")


def _uniq():
    global _n
    _n += 1
    return f"79{_n:05d}"


def _entitled_user(name=None):
    name = name or f"hc{_uniq()}"
    u = U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.update_or_create(
        user=u, defaults=dict(current_plan="beta", plan_status="active", viewer_mode=False))
    return u


def _node(host=HOST, *, max_accounts=12, status=None):
    login = _uniq()
    return TerminalNode.objects.create(
        hostname=f"n-{login}", rdp_host=host, max_accounts=max_accounts,
        status=status or TerminalNode.Status.ACTIVE)


def _occupy(node, count):
    """Create ``count`` live accounts bound to ``node`` via ``terminal_node`` (is_active=True) so that
    ``node_occupant_count(node) == count`` — REGARDLESS of the node's own status (that is the point of the
    draining-node test: node_occupant_count filters on terminal_node_id + is_active, not node.status)."""
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    for _ in range(count):
        login = _uniq()
        user = U.objects.create_user(username=f"oc{login}", email=f"{login}@x.invalid", password="x")
        TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number=login,
                                      is_demo=True, is_active=True, broker_server=srv, terminal_node=node)


def _all():
    return list(TerminalNode.objects.all())   # ALL statuses — mirrors what the allocator locks+scans when guard ON


class HostOccupancyCountTests(TestCase):
    def test_sums_across_coresident_nodes_grouped_by_rdp_host(self):
        n1 = _node(HOST, max_accounts=10)
        n2 = _node(HOST, max_accounts=12)
        n_other = _node(OTHER, max_accounts=12)
        _occupy(n1, 3)
        _occupy(n2, 4)
        _occupy(n_other, 5)
        self.assertEqual(host_occupant_count(HOST, _all()), 7)     # 3 + 4 across the two co-resident logical nodes
        self.assertEqual(host_occupant_count(OTHER, _all()), 5)    # a different physical host is independent
        self.assertEqual(host_occupant_count("", _all()), 0)       # blank key → 0
        self.assertEqual(host_occupant_count(HOST), 7)             # nodes=None queries all co-resident (any status)
        self.assertEqual(P.node_occupant_count(n1), 3)             # the per-node count is unchanged

    def test_counts_occupants_on_non_active_coresident_nodes(self):
        # THE under-count fix: a DRAINING/OFFLINE co-resident node still has occupants physically loading the box.
        n_active = _node(HOST, max_accounts=10, status=TerminalNode.Status.ACTIVE)
        n_drain = _node(HOST, max_accounts=12, status=TerminalNode.Status.DRAINING)
        n_off = _node(HOST, max_accounts=12, status=TerminalNode.Status.OFFLINE)
        _occupy(n_active, 2)
        _occupy(n_drain, 3)
        _occupy(n_off, 1)
        self.assertEqual(host_occupant_count(HOST, _all()), 6)     # 2 + 3 + 1 — all statuses count
        self.assertEqual(host_occupant_count(HOST), 6)             # nodes=None path too

    def test_case_insensitive_host_grouping(self):
        n1 = _node("10.0.0.5")
        _occupy(n1, 2)
        self.assertEqual(host_occupant_count("10.0.0.5", _all()), 2)
        self.assertEqual(host_occupant_count("10.0.0.5".upper(), _all()), 2)


class HostCapacityCeilingTests(TestCase):
    def test_none_when_no_row_then_value_when_set(self):
        self.assertIsNone(host_capacity_ceiling(HOST))                 # no row → None (fail-closed signal)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=20, certified_safe=20)
        self.assertEqual(host_capacity_ceiling(HOST), 20)
        self.assertEqual(host_capacity_ceiling(HOST.upper()), 20)      # iexact match
        self.assertIsNone(host_capacity_ceiling(""))                   # blank → None

    def test_ceiling_zero_is_returned_as_zero_not_none(self):
        # A certified hard-stop of 0 must stay 0 (refuse everyone), NOT collapse to None ("unconfigured").
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=0)
        self.assertEqual(host_capacity_ceiling(HOST), 0)


class HostCapacityBlockTests(TestCase):
    def test_unconfigured_when_no_ceiling(self):
        n1 = _node(HOST)
        _occupy(n1, 1)
        self.assertEqual(host_capacity_block(n1, _all()), ALLOC_HOST_CAPACITY_UNCONFIGURED)

    def test_unconfigured_when_blank_rdp_host(self):
        # Defensive contract of the helper. NOTE: in allocate_workspace_node a blank-rdp_host node is caught by
        # _node_deliverable FIRST (→ not_deliverable), so the allocator never reaches this branch for a blank host;
        # this asserts the helper's own fail-closed behaviour, not an allocator-reachable path.
        n_blank = _node("")
        self.assertEqual(host_capacity_block(n_blank, _all()), ALLOC_HOST_CAPACITY_UNCONFIGURED)

    def test_ok_below_ceiling_then_exceeded_at_ceiling(self):
        n1 = _node(HOST, max_accounts=10)
        n2 = _node(HOST, max_accounts=12)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=7)
        _occupy(n1, 3)
        _occupy(n2, 3)                                             # host total 6 < 7
        self.assertIsNone(host_capacity_block(n2, _all()))         # ok, room for one more
        _occupy(n2, 1)                                             # host total now 7 == ceiling
        self.assertEqual(host_capacity_block(n2, _all()), ALLOC_HOST_CAPACITY_EXCEEDED)

    def test_ceiling_zero_refuses_everyone(self):
        n1 = _node(HOST, max_accounts=10)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=0)
        self.assertEqual(host_capacity_block(n1, _all()), ALLOC_HOST_CAPACITY_EXCEEDED)   # 0 occupants >= 0 ceiling

    def test_draining_coresident_occupants_count_toward_ceiling(self):
        n_active = _node(HOST, max_accounts=10, status=TerminalNode.Status.ACTIVE)
        n_drain = _node(HOST, max_accounts=12, status=TerminalNode.Status.DRAINING)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=4)
        _occupy(n_active, 1)
        _occupy(n_drain, 3)                                        # box physically at 4 (1 active + 3 draining)
        self.assertEqual(host_capacity_block(n_active, _all()), ALLOC_HOST_CAPACITY_EXCEEDED)

    def test_same_host_node3_cannot_bypass_ceiling(self):
        n1 = _node(HOST, max_accounts=10)
        n2 = _node(HOST, max_accounts=12)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=7)
        _occupy(n1, 4)
        _occupy(n2, 3)                                             # host total 7 == ceiling
        n3 = _node(HOST, max_accounts=50)                          # fresh same-host node with huge per-node room
        self.assertTrue(P.node_occupant_count(n3) < n3.max_accounts)   # n3 individually looks wide open
        self.assertEqual(host_capacity_block(n3, _all()), ALLOC_HOST_CAPACITY_EXCEEDED)  # but the BOX is full

    def test_customer_zero_occupancy_counts_toward_host(self):
        n_cz = _node(HOST, max_accounts=10)        # stands in for the CZ logical node
        n_beta = _node(HOST, max_accounts=12)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=2)
        _occupy(n_cz, 1)
        _occupy(n_beta, 1)                         # host total 2 == ceiling
        self.assertEqual(host_capacity_block(n_beta, _all()), ALLOC_HOST_CAPACITY_EXCEEDED)

    def test_other_host_unaffected_by_full_host(self):
        n_full = _node(HOST, max_accounts=10)
        n_free = _node(OTHER, max_accounts=10)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=1)
        HostCapacity.objects.create(physical_host_key=OTHER, max_total_occupants=5)
        _occupy(n_full, 1)
        self.assertEqual(host_capacity_block(n_full, _all()), ALLOC_HOST_CAPACITY_EXCEEDED)
        self.assertIsNone(host_capacity_block(n_free, _all()))     # the independent host still has room


class HostCapacityFlagTests(TestCase):
    @override_settings(HOSTED_HOST_CAPACITY_GUARD_ENABLED="1")
    def test_flag_on(self):
        self.assertTrue(hosted_host_capacity_guard_enabled())

    def test_flag_off_when_unset(self):
        self.assertFalse(hosted_host_capacity_guard_enabled())


@override_settings(**_FLAGS_ON, HOSTED_HOST_CAPACITY_GUARD_ENABLED="1")
class AllocatorGuardOnIntegrationTests(TestCase):
    """Drive the REAL allocate_workspace_node with the guard ON (the ON-path wiring + reason precedence)."""

    def _ws(self, login="700900"):
        return P.request_hosted_workspace(_entitled_user(), expected_login=login).workspace

    def test_refuses_when_host_at_ceiling(self):
        n = _node(HOST, max_accounts=10)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=2)
        _occupy(n, 2)                                              # host at ceiling
        ws = self._ws()
        res = P.allocate_workspace_node(ws)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, ALLOC_HOST_CAPACITY_EXCEEDED)
        ws = HostedMt5Workspace.objects.get(pk=ws.pk)
        self.assertIsNone(ws.execution_node_id)                   # not bound
        self.assertEqual(str(ws.canonical_state), S.PROVISIONING) # not advanced

    def test_refuses_when_host_unconfigured(self):
        _node(HOST, max_accounts=10)                              # ACTIVE, deliverable, but NO HostCapacity row
        ws = self._ws()
        res = P.allocate_workspace_node(ws)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, ALLOC_HOST_CAPACITY_UNCONFIGURED)
        self.assertIsNone(HostedMt5Workspace.objects.get(pk=ws.pk).execution_node_id)

    def test_draining_coresident_blocks_bind_end_to_end(self):
        # The regression test for the under-count bug, THROUGH the real allocator: an ACTIVE node has room, but a
        # DRAINING co-resident node's occupants fill the certified box ceiling → the bind is refused.
        n_active = _node(HOST, max_accounts=10, status=TerminalNode.Status.ACTIVE)
        n_drain = _node(HOST, max_accounts=12, status=TerminalNode.Status.DRAINING)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=2)
        _occupy(n_drain, 2)                                       # box full via the draining node alone
        self.assertTrue(P.node_occupant_count(n_active) < n_active.max_accounts)  # the active node looks open
        ws = self._ws()
        res = P.allocate_workspace_node(ws)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, ALLOC_HOST_CAPACITY_EXCEEDED)

    def test_binds_when_host_has_room(self):
        n = _node(HOST, max_accounts=10)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=5)
        ws = self._ws()
        res = P.allocate_workspace_node(ws)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.reason, P.ALLOC_OK)
        ws = HostedMt5Workspace.objects.get(pk=ws.pk)
        self.assertEqual(ws.execution_node_id, n.pk)
        self.assertEqual(str(ws.canonical_state), S.WAITING_FOR_LOGIN)


@override_settings(**_FLAGS_ON)   # guard flag UNSET → OFF
class AllocatorGuardOffIntegrationTests(TestCase):
    def test_guard_off_ignores_host_ceiling(self):
        # Same over-ceiling setup as the ON test, but with the guard OFF the allocator binds normally — proving the
        # DARK default changes nothing.
        n = _node(HOST, max_accounts=10)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=1)
        _occupy(n, 5)                                             # way past the (ignored) host ceiling of 1
        ws = P.request_hosted_workspace(_entitled_user(), expected_login="700901").workspace
        res = P.allocate_workspace_node(ws)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.reason, P.ALLOC_OK)
        self.assertEqual(HostedMt5Workspace.objects.get(pk=ws.pk).execution_node_id, n.pk)


class SetHostCapacityCommandTests(TestCase):
    def test_dry_run_writes_nothing_then_apply_writes_row_and_audit(self):
        call_command("set_host_capacity", "--host-key", HOST, "--max", "20", "--safe", "18", stdout=StringIO())
        self.assertFalse(HostCapacity.objects.filter(physical_host_key=HOST).exists())   # dry-run default
        self.assertEqual(HostCapacityAudit.objects.count(), 0)
        call_command("set_host_capacity", "--host-key", HOST, "--max", "20", "--safe", "18",
                     "--basis", "ADR-0069 recert", "--actor", "op", "--apply", stdout=StringIO())
        row = HostCapacity.objects.get(physical_host_key=HOST)
        self.assertEqual((row.max_total_occupants, row.certified_safe), (20, 18))
        a = HostCapacityAudit.objects.get()
        self.assertEqual((a.previous_max, a.new_max, a.new_safe), (None, 20, 18))

    def test_update_without_safe_preserves_prior_safe(self):
        call_command("set_host_capacity", "--host-key", HOST, "--max", "20", "--safe", "18", "--apply",
                     stdout=StringIO())
        call_command("set_host_capacity", "--host-key", HOST, "--max", "16", "--apply", stdout=StringIO())
        row = HostCapacity.objects.get(physical_host_key=HOST)
        self.assertEqual((row.max_total_occupants, row.certified_safe), (16, 18))   # safe preserved, not reset
        self.assertEqual(HostCapacityAudit.objects.count(), 2)

    def test_safe_greater_than_max_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("set_host_capacity", "--host-key", HOST, "--max", "20", "--safe", "30", "--apply",
                         stdout=StringIO())


class HostCeilingConcurrencyTests(TransactionTestCase):
    """Two concurrent REAL allocate_workspace_node calls (guard ON) with one free host slot: exactly one binds,
    the other is refused ALLOC_HOST_CAPACITY_EXCEEDED. select_for_update over the node rows serialises the binds,
    so the loser observes the winner's committed occupancy. (Requires PostgreSQL row locking.)"""

    def _alloc(self, ws_pk, barrier, results, idx):
        try:
            barrier.wait(timeout=15)
            ws = HostedMt5Workspace.objects.get(pk=ws_pk)
            res = P.allocate_workspace_node(ws)
            results[idx] = (res.ok, res.reason)
        except Exception as e:  # pragma: no cover - surfaces as a failed assertion below
            results[idx] = ("error", repr(e))
        finally:
            connection.close()

    @override_settings(**_FLAGS_ON, HOSTED_HOST_CAPACITY_GUARD_ENABLED="1")
    def test_two_concurrent_binds_cannot_oversubscribe(self):
        _node(HOST, max_accounts=50)                                   # per-node room ample; host ceiling binds
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=1)   # exactly ONE slot box-wide
        ws1 = P.request_hosted_workspace(_entitled_user(), expected_login="700801").workspace
        ws2 = P.request_hosted_workspace(_entitled_user(), expected_login="700802").workspace
        barrier = threading.Barrier(2)
        results = {}
        threads = [threading.Thread(target=self._alloc, args=(ws.pk, barrier, results, i))
                   for i, ws in enumerate((ws1, ws2))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        oks = sorted((ok, reason) for (ok, reason) in results.values())
        self.assertEqual(oks, [(False, ALLOC_HOST_CAPACITY_EXCEEDED), (True, P.ALLOC_OK)],
                         f"expected exactly one bind under contention, got {results}")
        self.assertEqual(host_occupant_count(HOST), 1)                 # never oversubscribed
