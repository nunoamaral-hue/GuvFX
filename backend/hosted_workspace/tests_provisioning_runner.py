"""Beta Readiness Stream 2 — G2 (autonomous node-allocation driver) + G12 (rdp_host deliverability).

Proves the provisioning driver allocates every PROVISIONING workspace idempotently + fail-closed, and that
allocation now refuses a node without a durable rdp_host (so workspace→node→rdp_host holds before delivery).
Nothing here arms execution or places an order.
"""
from unittest import mock

from django.test import TestCase, override_settings

from hosted_workspace import provisioning as P
from hosted_workspace import provisioning_runner as R
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from hosted_workspace.tests_provisioning import _FLAGS_ON, _node, _user


def _requested(login="700900"):
    res = P.request_hosted_workspace(_user(login), expected_login=login)
    assert res.ok, res.reason
    return res.workspace


class ProvisioningDriverTests(TestCase):
    def test_dark_master_flag_is_a_no_op(self):
        # No flags → master OFF → dormant, touches nothing.
        out = R.run_workspace_provisioning()
        self.assertFalse(out["enabled"])
        self.assertEqual(out["candidates"], 0)
        self.assertEqual(out["allocated"], 0)

    @override_settings(**_FLAGS_ON)
    def test_allocates_provisioning_workspace_and_advances(self):
        _node(max_accounts=5)
        ws = _requested()
        self.assertEqual(str(ws.canonical_state), S.PROVISIONING)
        out = R.run_workspace_provisioning()
        self.assertTrue(out["enabled"])
        self.assertEqual((out["candidates"], out["allocated"]), (1, 1))
        ws.refresh_from_db()
        self.assertIsNotNone(ws.execution_node_id)                       # G2: bound
        self.assertEqual(str(ws.canonical_state), S.WAITING_FOR_LOGIN)   # advanced

    @override_settings(**_FLAGS_ON)
    def test_repeat_is_idempotent_changes_nothing(self):
        _node(max_accounts=5)
        _requested()
        R.run_workspace_provisioning()                    # allocates + advances out of PROVISIONING
        out2 = R.run_workspace_provisioning()             # no PROVISIONING candidate remains
        self.assertEqual((out2["candidates"], out2["allocated"], out2["already"]), (0, 0, 0))

    @override_settings(**_FLAGS_ON)
    def test_no_node_capacity_fails_closed_and_leaves_workspace_untouched(self):
        _node(max_accounts=0)                             # active but zero capacity
        ws = _requested()
        out = R.run_workspace_provisioning()
        self.assertEqual((out["candidates"], out["allocated"], out["no_capacity"]), (1, 0, 1))
        ws.refresh_from_db()
        self.assertIsNone(ws.execution_node_id)
        self.assertEqual(str(ws.canonical_state), S.PROVISIONING)        # retry next cycle

    @override_settings(**_FLAGS_ON)
    def test_missing_node_fails_closed(self):
        ws = _requested()                                # NO node created
        out = R.run_workspace_provisioning()
        self.assertEqual((out["allocated"], out["no_capacity"]), (0, 1))
        ws.refresh_from_db()
        self.assertIsNone(ws.execution_node_id)

    @override_settings(**_FLAGS_ON)
    def test_node_without_rdp_host_is_not_deliverable_G12(self):
        _node(rdp_host="", max_accounts=5)               # capacity but no durable rdp_host
        ws = _requested()
        out = R.run_workspace_provisioning()
        self.assertEqual((out["allocated"], out["not_deliverable"]), (0, 1))
        ws.refresh_from_db()
        self.assertIsNone(ws.execution_node_id)          # never bound to an undeliverable node

    @override_settings(**_FLAGS_ON)
    def test_one_failure_does_not_stop_the_cycle(self):
        _node(max_accounts=5)
        _requested("700901")
        _requested("700902")
        real = P.allocate_workspace_node
        calls = {"n": 0}

        def flaky(ws, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")               # first workspace errors
            return real(ws, **kw)

        # allocate_workspace_node is imported locally inside the driver, so patch it on its source module.
        with mock.patch.object(P, "allocate_workspace_node", side_effect=flaky):
            out = R.run_workspace_provisioning()
        self.assertEqual(out["candidates"], 2)
        self.assertEqual(out["errors"], 1)               # error isolated
        self.assertEqual(out["allocated"], 1)            # the other still allocated


class AllocateDeliverabilityTests(TestCase):
    @override_settings(**_FLAGS_ON)
    def test_allocate_refuses_node_without_rdp_host(self):
        _node(rdp_host="", max_accounts=5)
        res = P.allocate_workspace_node(_requested())
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.ALLOC_NODE_NOT_DELIVERABLE)

    @override_settings(**_FLAGS_ON)
    def test_allocate_binds_deliverable_node(self):
        _node(rdp_host="10.0.0.9", max_accounts=5)
        res = P.allocate_workspace_node(_requested())
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.reason, P.ALLOC_OK)

    @override_settings(**_FLAGS_ON)
    def test_prefers_deliverable_node_over_undeliverable_one(self):
        _node(hostname="no-rdp", rdp_host="", max_accounts=5)            # id lower, undeliverable
        good = _node(hostname="has-rdp", rdp_host="10.0.0.9", max_accounts=5)
        res = P.allocate_workspace_node(_requested())
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.node_hostname, good.hostname)              # skipped the undeliverable one
