"""ADR-0069 — physical-host-wide capacity guard tests.

Covers: ``host_occupant_count`` sums across co-resident logical nodes (grouped by ``rdp_host``);
``host_capacity_ceiling`` reads the certified ``HostCapacity`` LIVE and returns ``None`` (the fail-closed
signal) when absent; ``host_capacity_block`` refuses an uncertified host (blank ``rdp_host`` OR no row) and a
host already at its ceiling, and a fresh same-host "node 3" with ample per-node room CANNOT bypass the host
ceiling; Customer Zero's occupancy counts toward the host total; the flag gates the guard; and a real
two-thread race (one free slot) cannot oversubscribe the host ceiling (``select_for_update`` serialises binds).
"""
from __future__ import annotations

import threading

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from execution.models import HostCapacity, TerminalNode
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import provisioning as P
from hosted_workspace.flags import hosted_host_capacity_guard_enabled
from hosted_workspace.provisioning import (ALLOC_HOST_CAPACITY_EXCEEDED, ALLOC_HOST_CAPACITY_UNCONFIGURED,
                                           host_capacity_block, host_capacity_ceiling, host_occupant_count)

U = get_user_model()
_n = 0

HOST = "10.9.9.9"
OTHER = "10.9.9.8"


def _uniq():
    global _n
    _n += 1
    return f"79{_n:05d}"


def _node(host, *, max_accounts=12, status=None):
    login = _uniq()
    return TerminalNode.objects.create(
        hostname=f"n-{login}", rdp_host=host, max_accounts=max_accounts,
        status=status or TerminalNode.Status.ACTIVE)


def _occupy(node, count):
    """Create ``count`` live accounts bound to ``node`` via ``terminal_node`` (is_active=True) so that
    ``node_occupant_count(node) == count`` (the legacy term of the occupancy union)."""
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    for _ in range(count):
        login = _uniq()
        user = U.objects.create_user(username=f"hc{login}", email=f"{login}@x.invalid", password="x")
        TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number=login,
                                      is_demo=True, is_active=True, broker_server=srv, terminal_node=node)


def _active():
    return list(TerminalNode.objects.filter(status=TerminalNode.Status.ACTIVE))


class HostOccupancyCountTests(TestCase):
    def test_sums_across_coresident_nodes_grouped_by_rdp_host(self):
        n1 = _node(HOST, max_accounts=10)
        n2 = _node(HOST, max_accounts=12)
        n_other = _node(OTHER, max_accounts=12)
        _occupy(n1, 3)
        _occupy(n2, 4)
        _occupy(n_other, 5)
        active = _active()
        self.assertEqual(host_occupant_count(HOST, active), 7)    # 3 + 4 across the two co-resident logical nodes
        self.assertEqual(host_occupant_count(OTHER, active), 5)   # a different physical host is independent
        self.assertEqual(host_occupant_count("", active), 0)      # blank key → 0
        self.assertEqual(P.node_occupant_count(n1), 3)            # the per-node count is unchanged

    def test_case_insensitive_host_grouping(self):
        n1 = _node("10.0.0.5")
        _occupy(n1, 2)
        self.assertEqual(host_occupant_count("10.0.0.5", _active()), 2)
        self.assertEqual(host_occupant_count("10.0.0.5".upper(), _active()), 2)


class HostCapacityCeilingTests(TestCase):
    def test_none_when_no_row_then_value_when_set(self):
        self.assertIsNone(host_capacity_ceiling(HOST))                 # no row → None (fail-closed signal)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=20, certified_safe=20)
        self.assertEqual(host_capacity_ceiling(HOST), 20)
        self.assertEqual(host_capacity_ceiling(HOST.upper()), 20)      # iexact match
        self.assertIsNone(host_capacity_ceiling(""))                   # blank → None


class HostCapacityBlockTests(TestCase):
    def test_unconfigured_when_no_ceiling(self):
        n1 = _node(HOST)
        _occupy(n1, 1)
        self.assertEqual(host_capacity_block(n1, _active()), ALLOC_HOST_CAPACITY_UNCONFIGURED)

    def test_unconfigured_when_blank_rdp_host(self):
        n_blank = _node("")            # no physical-host identity at all
        self.assertEqual(host_capacity_block(n_blank, _active()), ALLOC_HOST_CAPACITY_UNCONFIGURED)

    def test_ok_below_ceiling_then_exceeded_at_ceiling(self):
        n1 = _node(HOST, max_accounts=10)
        n2 = _node(HOST, max_accounts=12)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=7)
        _occupy(n1, 3)
        _occupy(n2, 3)                                             # host total 6 < 7
        self.assertIsNone(host_capacity_block(n2, _active()))      # ok, room for one more
        _occupy(n2, 1)                                             # host total now 7 == ceiling
        self.assertEqual(host_capacity_block(n2, _active()), ALLOC_HOST_CAPACITY_EXCEEDED)

    def test_same_host_node3_cannot_bypass_ceiling(self):
        n1 = _node(HOST, max_accounts=10)
        n2 = _node(HOST, max_accounts=12)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=7)
        _occupy(n1, 4)
        _occupy(n2, 3)                                             # host total 7 == ceiling
        n3 = _node(HOST, max_accounts=50)                          # fresh same-host node with huge per-node room
        self.assertTrue(P.node_occupant_count(n3) < n3.max_accounts)   # n3 individually looks wide open
        self.assertEqual(host_capacity_block(n3, _active()), ALLOC_HOST_CAPACITY_EXCEEDED)  # but the BOX is full

    def test_customer_zero_occupancy_counts_toward_host(self):
        n_cz = _node(HOST, max_accounts=10)        # stands in for the CZ logical node
        n_beta = _node(HOST, max_accounts=12)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=2)
        _occupy(n_cz, 1)                           # CZ consumes 1 of the host total
        _occupy(n_beta, 1)                         # beta consumes 1 → host total 2 == ceiling
        self.assertEqual(host_capacity_block(n_beta, _active()), ALLOC_HOST_CAPACITY_EXCEEDED)

    def test_other_host_unaffected_by_full_host(self):
        n_full = _node(HOST, max_accounts=10)
        n_free = _node(OTHER, max_accounts=10)
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=1)
        HostCapacity.objects.create(physical_host_key=OTHER, max_total_occupants=5)
        _occupy(n_full, 1)                         # HOST full
        self.assertEqual(host_capacity_block(n_full, _active()), ALLOC_HOST_CAPACITY_EXCEEDED)
        self.assertIsNone(host_capacity_block(n_free, _active()))   # the independent host still has room


class HostCapacityFlagTests(TestCase):
    @override_settings(HOSTED_HOST_CAPACITY_GUARD_ENABLED="1")
    def test_flag_on(self):
        self.assertTrue(hosted_host_capacity_guard_enabled())

    @override_settings(HOSTED_HOST_CAPACITY_GUARD_ENABLED="0")
    def test_flag_off_by_default_token(self):
        self.assertFalse(hosted_host_capacity_guard_enabled())

    def test_flag_off_when_unset(self):
        self.assertFalse(hosted_host_capacity_guard_enabled())


class HostCeilingConcurrencyTests(TransactionTestCase):
    """The guard's race-safety rests on ``select_for_update`` over the ACTIVE node rows: two concurrent binds
    serialise on those locks, so the second sees the first's COMMITTED occupancy. With a ceiling leaving exactly
    ONE free slot, exactly one of two concurrent binds may succeed. (Requires a real DB with row locking —
    the backend test DB is PostgreSQL.)"""

    def _critical_section(self, host, ceiling, srv_id, barrier, results, idx):
        try:
            barrier.wait(timeout=15)
            with transaction.atomic():
                nodes = list(TerminalNode.objects.select_for_update()
                             .filter(status=TerminalNode.Status.ACTIVE, rdp_host=host).order_by("id"))
                if host_occupant_count(host, nodes) >= ceiling:
                    results[idx] = "refused"
                    return
                node = nodes[0]
                login = f"rc{idx}{threading.get_ident()}"[:30]
                user = U.objects.create_user(username=f"u{login}", email=f"{login}@x.invalid", password="x")
                TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number=login,
                                              is_demo=True, is_active=True, broker_server_id=srv_id,
                                              terminal_node=node)
                results[idx] = "bound"
        except Exception as e:  # pragma: no cover - surfaces as a failed assertion below
            results[idx] = f"error:{e!r}"
        finally:
            connection.close()

    def test_two_concurrent_binds_cannot_oversubscribe(self):
        n1 = _node(HOST, max_accounts=50)        # per-node room is ample; the HOST ceiling is the only limit
        srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
        HostCapacity.objects.create(physical_host_key=HOST, max_total_occupants=1)   # exactly ONE slot box-wide
        barrier = threading.Barrier(2)
        results = {}
        threads = [threading.Thread(target=self._critical_section,
                                    args=(HOST, 1, srv.id, barrier, results, i)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(sorted(results.values()), ["bound", "refused"],
                         f"expected exactly one bind under contention, got {results}")
        self.assertEqual(host_occupant_count(HOST), 1)              # never oversubscribed
