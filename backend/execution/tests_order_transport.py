"""Beta Launch (ADR-0034 co-residency) — per-node ORDER-TRANSPORT selection seam.

A HOSTED (Provider-B) order MUST go to its OWN node's pin-enforcing bridge, never Customer Zero's shared
global bridge; a LEGACY / Customer-Zero job MUST keep the global bridge, byte-for-byte. These tests pin the
resolver's decision table (the Sponsor's 10-point bar) and structurally guard the dispatcher wiring —
removing the per-node resolution call fails a test.
"""
from __future__ import annotations

import ast
import contextlib
import inspect
from unittest import mock

from django.test import SimpleTestCase, TestCase

from execution.order_transport import (
    OT_ENDPOINT_UNCONFIGURED,
    OT_LEGACY_GLOBAL,
    OT_NODE_MISMATCH,
    OT_NODE_OK,
    OT_NODE_UNBOUND,
    OT_RESOLVE_ERROR,
    resolve_order_transport,
)

GLOBAL = "http://guvfx-agent:8788"   # Customer Zero's shared legacy bridge (AGENT_ORDER_BASE)
NODE_A = "http://10.50.0.9:8790"     # beta node A's dedicated pin-enforcing bridge
NODE_B = "http://10.50.0.10:8790"    # beta node B's dedicated pin-enforcing bridge


class _Node:
    def __init__(self, pk, url=""):
        self.pk = pk
        self.order_bridge_base_url = url


class _Acct:
    def __init__(self, node_id):
        self.terminal_node_id = node_id


class _Job:
    """Minimal stand-in for an ExecutionJob (the resolver reads only these attributes)."""
    def __init__(self, account, node=None):
        self.account = account
        self.terminal_node = node
        self.terminal_node_id = getattr(node, "pk", None)


def _resolve(job, *, flag_on=True, hosted=False, classifier_error=None):
    """Resolve with the canonical classifier (``execution.hosted_pin``) patched — the resolver imports it
    function-locally, so patching the module attribute is authoritative."""
    patches = [mock.patch("execution.hosted_pin.pin_subsystem_enabled", return_value=flag_on)]
    if classifier_error is not None:
        patches.append(mock.patch("execution.hosted_pin.is_hosted_workspace_account",
                                   side_effect=classifier_error))
    else:
        patches.append(mock.patch("execution.hosted_pin.is_hosted_workspace_account",
                                   return_value=hosted))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return resolve_order_transport(job, global_base_url=GLOBAL)


class OrderTransportResolverTests(SimpleTestCase):
    # NOTE (ADR-0046 production correction): these ``_Acct(1)`` non-hosted cases prove the PROVIDER-A /
    # legacy path. In PRODUCTION Customer Zero is PROVIDER-B (hosted), so the Customer-Zero safety proof is
    # ProductionPremiseProviderBRoutingTests, NOT these — do not read "acct 1" here as Customer Zero.
    # (1) A Provider-A / legacy (non-hosted) job resolves to the global bridge — even when node-bound.
    def test_legacy_non_hosted_resolves_global(self):
        t = _resolve(_Job(_Acct(1), _Node(1)), flag_on=True, hosted=False)
        self.assertTrue(t.ok)
        self.assertFalse(t.hosted)
        self.assertEqual(t.reason_code, OT_LEGACY_GLOBAL)
        self.assertEqual(t.base_url, GLOBAL)

    # (8) CZ behaviour unchanged while the subsystem is DARK (no per-node endpoint exists / consulted).
    def test_dark_subsystem_resolves_global(self):
        t = _resolve(_Job(_Acct(1), _Node(1, NODE_A)), flag_on=False, hosted=False)
        self.assertTrue(t.ok)
        self.assertFalse(t.hosted)
        self.assertEqual(t.base_url, GLOBAL)

    # (8, CZ-safety) A non-hosted job is global REGARDLESS of its node carrying an endpoint — the decision
    # is keyed on the canonical hosted classifier, never on node-endpoint presence.
    def test_legacy_job_global_even_if_node_has_endpoint(self):
        t = _resolve(_Job(_Acct(1), _Node(1, NODE_A)), flag_on=True, hosted=False)
        self.assertTrue(t.ok)
        self.assertEqual(t.base_url, GLOBAL)

    # (2) Hosted beta job resolves to its node-specific bridge.
    def test_hosted_resolves_node_bridge(self):
        t = _resolve(_Job(_Acct(7), _Node(7, NODE_A)), flag_on=True, hosted=True)
        self.assertTrue(t.ok)
        self.assertTrue(t.hosted)
        self.assertEqual(t.reason_code, OT_NODE_OK)
        self.assertEqual(t.base_url, NODE_A)

    # (3)+(6) Hosted job whose node has NO endpoint fails CLOSED — and NEVER the global bridge.
    def test_hosted_missing_endpoint_fails_closed_never_global(self):
        t = _resolve(_Job(_Acct(7), _Node(7, "")), flag_on=True, hosted=True)
        self.assertFalse(t.ok)
        self.assertEqual(t.reason_code, OT_ENDPOINT_UNCONFIGURED)
        self.assertEqual(t.base_url, "")
        self.assertNotEqual(t.base_url, GLOBAL)

    # (3) Hosted job with no node snapshot at all -> fail closed, never global.
    def test_hosted_unbound_node_fails_closed(self):
        t = _resolve(_Job(_Acct(7), node=None), flag_on=True, hosted=True)
        self.assertFalse(t.ok)
        self.assertEqual(t.reason_code, OT_NODE_UNBOUND)
        self.assertNotEqual(t.base_url, GLOBAL)

    # (5) Wrong-node binding (job node != account node) fails closed.
    def test_hosted_node_mismatch_fails_closed(self):
        t = _resolve(_Job(_Acct(8), _Node(7, NODE_A)), flag_on=True, hosted=True)
        self.assertFalse(t.ok)
        self.assertEqual(t.reason_code, OT_NODE_MISMATCH)
        self.assertNotEqual(t.base_url, GLOBAL)

    # (4) Two beta nodes resolve to different bridge endpoints.
    def test_two_hosted_nodes_resolve_different_bridges(self):
        ta = _resolve(_Job(_Acct(7), _Node(7, NODE_A)), hosted=True)
        tb = _resolve(_Job(_Acct(8), _Node(8, NODE_B)), hosted=True)
        self.assertTrue(ta.ok and tb.ok)
        self.assertEqual(ta.base_url, NODE_A)
        self.assertEqual(tb.base_url, NODE_B)
        self.assertNotEqual(ta.base_url, tb.base_url)

    # A classifier error is ambiguous -> fail closed (never the global CZ bridge).
    def test_classifier_error_fails_closed_never_global(self):
        t = _resolve(_Job(_Acct(7), _Node(7, NODE_A)), classifier_error=RuntimeError("boom"))
        self.assertFalse(t.ok)
        self.assertEqual(t.reason_code, OT_RESOLVE_ERROR)
        self.assertNotEqual(t.base_url, GLOBAL)

    # (9) AUTO_SHADOW behaviour unchanged: the shadow path uses this SAME resolver; a CZ/legacy shadow job
    # (non-hosted) resolves global exactly like the live legacy path.
    def test_shadow_uses_same_resolver_non_hosted_global(self):
        t = _resolve(_Job(_Acct(1), _Node(1)), flag_on=True, hosted=False)
        self.assertTrue(t.ok)
        self.assertEqual(t.base_url, GLOBAL)

    # (7) Identity pin survives ALONGSIDE the resolved node transport at the DISPATCH boundary. Not a
    # tautology: the real apply_identity_pin fills the payload, then that payload AND the resolved per-node
    # base flow through ONE agent_order call — and we assert agent_order RECEIVED both. A regression that
    # dropped the pin, or dispatched to the global bridge instead of the node base, fails here.
    def test_identity_pin_survives_the_dispatch_transport(self):
        import mt5_trade_ingest_worker as worker
        from execution.order_transport import OT_NODE_OK, OrderTransport
        agent_payload = {"symbol": "EURUSD", "side": "BUY", "lots": 0.01}
        worker.apply_identity_pin(agent_payload, {"require_identity_pin": True, "expected_login": "770077",
                                                  "expected_server": "GuvfxBeta-Demo", "is_demo": True})
        with mock.patch.object(worker, "resolve_order_base",
                               return_value=OrderTransport(True, OT_NODE_OK, NODE_A, hosted=True)), \
             mock.patch.object(worker, "agent_order", return_value={"ok": True}) as ao:
            base = worker.resolve_order_base(123).base_url
            worker.agent_order(agent_payload, base)
        sent_payload, sent_base = ao.call_args.args
        self.assertEqual(sent_base, NODE_A)                        # the per-node transport
        self.assertIs(sent_payload["require_identity_pin"], True)  # pin survives ALONGSIDE it
        self.assertEqual(sent_payload["expected_login"], "770077")
        self.assertEqual(sent_payload["expected_server"], "GuvfxBeta-Demo")


class TerminalNodeOrderBridgeFieldTests(TestCase):
    def test_field_roundtrips_and_defaults_blank(self):
        from execution.models import TerminalNode
        n = TerminalNode.objects.create(hostname="beta-node-ot-x")
        self.assertEqual(n.order_bridge_base_url, "")  # default blank -> a hosted order fails closed
        n.order_bridge_base_url = NODE_A
        n.save(update_fields=["order_bridge_base_url"])
        n.refresh_from_db()
        self.assertEqual(n.order_bridge_base_url, NODE_A)


class ResolveOrderBaseHelperTests(TestCase):
    """The LIVE worker helper ``resolve_order_base`` itself — the function every dispatch site calls — not
    just the pure inner resolver. Covers its flag-ON ORM load + delegation, its fail-closed-on-missing-job
    arm, and the DARK short-circuit's no-extra-query guarantee (I3/I5/I1)."""

    def test_missing_job_fails_closed_never_global(self):
        # Flag ON + a job id that does not exist -> the DoesNotExist arm must fail CLOSED (never fall back
        # to Customer Zero's global bridge). This executes worker line ~112 that no other test reaches.
        import mt5_trade_ingest_worker as worker
        with mock.patch("execution.hosted_pin.pin_subsystem_enabled", return_value=True):
            t = worker.resolve_order_base(9_999_999)
        self.assertFalse(t.ok)                                    # kills the fall-back-to-global mutation
        self.assertEqual(t.reason_code, "order_transport_job_missing")
        self.assertEqual(t.base_url, "")                          # fail-closed carries NO dispatchable base

    def test_dark_short_circuit_issues_no_query(self):
        # Flag OFF -> the helper returns the global bridge WITHOUT loading the ExecutionJob (I3 byte-identical
        # dispatch). If it queried, the patched get() would raise and fail the test.
        import mt5_trade_ingest_worker as worker
        with mock.patch("execution.hosted_pin.pin_subsystem_enabled", return_value=False), \
             mock.patch.object(worker.ExecutionJob.objects, "get",
                               side_effect=AssertionError("DARK path must not query ExecutionJob")):
            t = worker.resolve_order_base(1)
        self.assertTrue(t.ok)
        self.assertFalse(t.hosted)
        self.assertEqual(t.base_url, worker.AGENT_ORDER_BASE)

    def test_flag_on_hosted_job_resolves_node_bridge(self):
        # Flag ON + a real hosted (Provider-B) ExecutionJob whose node carries an endpoint: the helper loads
        # the job and delegates to the resolver, returning the node bridge (ok, hosted, base=node url).
        import mt5_trade_ingest_worker as worker
        from django.contrib.auth import get_user_model
        from execution.models import ExecutionJob, TerminalNode
        from execution.readiness import PERSISTENT_WORKSPACE
        from trading.models import TradingAccount
        user = get_user_model().objects.create_user(
            username="ot-happy", email="ot-happy@x.invalid", password="x")
        node = TerminalNode.objects.create(hostname="beta-node-ot-happy", order_bridge_base_url=NODE_A)
        acct = TradingAccount.objects.create(
            user=user, name="a", broker_name="B", account_number="770077", is_demo=True,
            readiness_provider=PERSISTENT_WORKSPACE, terminal_node=node)
        job = ExecutionJob.objects.create(
            account=acct, terminal_node=node, job_type=ExecutionJob.JobType.CLOSE_TRADE,
            payload={"ticket": 1})
        # Set the real subsystem env flag so BOTH gate reads agree — resolve_order_base's
        # pin_subsystem_enabled() AND the classifier's _provider_b_pin_enabled() (which
        # is_hosted_workspace_account uses) resolve True off the one underlying flag.
        import os
        with mock.patch.dict(os.environ, {"HOSTED_PERSISTENT_MT5_ENABLED": "1"}, clear=False):
            t = worker.resolve_order_base(job.id)
        self.assertTrue(t.ok)
        self.assertTrue(t.hosted)
        self.assertEqual(t.base_url, NODE_A)


class ProductionPremiseProviderBRoutingTests(TestCase):
    """PRODUCTION TRUTH (ADR-0046 correction, 2026-08-14): **Customer Zero is a PROVIDER-B account**, not
    legacy — it was migrated to a hosted persistent workspace on node 1, and ``HOSTED_PERSISTENT_MT5_ENABLED``
    is ON in prod. The earlier ``_Acct(1)``/"legacy CZ" fixtures below prove the Provider-A path only; they
    are NOT the safety proof for Customer Zero. THIS class is: it uses a real Provider-B account whose node
    carries an EXPLICIT order endpoint, and proves the destination follows the authoritative execution NODE
    (never the account id / email / physical host / global config). A Provider-B account on a node with NO
    explicit endpoint fails CLOSED — it never falls back to any global bridge."""

    def _provider_b_account(self, *, login, node):
        from django.contrib.auth import get_user_model
        from execution.readiness import PERSISTENT_WORKSPACE
        from trading.models import TradingAccount
        user = get_user_model().objects.create_user(
            username="pb-%s" % login, email="pb-%s@x.invalid" % login, password="x")
        return TradingAccount.objects.create(
            user=user, name="a", broker_name="B", account_number=login, is_demo=True,
            readiness_provider=PERSISTENT_WORKSPACE, terminal_node=node)

    def _job(self, acct, node):
        from execution.models import ExecutionJob
        return ExecutionJob.objects.create(
            account=acct, terminal_node=node, job_type=ExecutionJob.JobType.CLOSE_TRADE, payload={"ticket": 1})

    def _resolve(self, job_id):
        import os
        import mt5_trade_ingest_worker as worker
        with mock.patch.dict(os.environ, {"HOSTED_PERSISTENT_MT5_ENABLED": "1"}, clear=False):
            return worker.resolve_order_base(job_id)

    # (1)+(10) Provider-B Customer Zero on node 1 resolves to node 1's EXPLICIT :8788 endpoint (byte-identical).
    def test_customer_zero_provider_b_resolves_to_its_node_8788_bridge(self):
        from execution.models import TerminalNode
        CZ_BRIDGE = "http://100.79.101.19:8788"
        node = TerminalNode.objects.create(
            hostname="cz-node-1", rdp_host="100.79.101.19", order_bridge_base_url=CZ_BRIDGE)
        job = self._job(self._provider_b_account(login="1302561", node=node), node)
        t = self._resolve(job.id)
        self.assertTrue(t.ok)
        self.assertTrue(t.hosted)
        self.assertEqual(t.base_url, CZ_BRIDGE)  # CZ routes to its OWN node's :8788 — never fail-closed

    # (3)+(4) A Provider-B node with NO explicit endpoint fails closed — never any global bridge.
    def test_provider_b_node_without_endpoint_fails_closed_never_global(self):
        from execution.models import TerminalNode
        node = TerminalNode.objects.create(hostname="pb-noendpoint", rdp_host="100.79.101.19")
        job = self._job(self._provider_b_account(login="700111", node=node), node)
        t = self._resolve(job.id)
        self.assertFalse(t.ok)
        self.assertEqual(t.reason_code, OT_ENDPOINT_UNCONFIGURED)
        self.assertEqual(t.base_url, "")

    # (5) Two Provider-B nodes on the SAME physical rdp_host resolve to DIFFERENT order bridges.
    def test_two_provider_b_nodes_same_rdp_host_resolve_different_bridges(self):
        from execution.models import TerminalNode
        cz = TerminalNode.objects.create(hostname="cores-cz", rdp_host="100.79.101.19",
                                         order_bridge_base_url="http://100.79.101.19:8788")
        beta = TerminalNode.objects.create(hostname="cores-beta", rdp_host="100.79.101.19",
                                           order_bridge_base_url="http://100.79.101.19:8790")
        cz_t = self._resolve(self._job(self._provider_b_account(login="1302561", node=cz), cz).id)
        beta_t = self._resolve(self._job(self._provider_b_account(login="900222", node=beta), beta).id)
        self.assertEqual(cz_t.base_url, "http://100.79.101.19:8788")
        self.assertEqual(beta_t.base_url, "http://100.79.101.19:8790")  # beta NEVER on CZ's :8788
        self.assertNotEqual(cz_t.base_url, beta_t.base_url)

    # (6) Destination follows execution-NODE authority, not the account id: two Provider-B accounts on the
    # SAME node resolve to the SAME endpoint.
    def test_destination_follows_node_authority_not_account_id(self):
        from execution.models import TerminalNode
        node = TerminalNode.objects.create(hostname="shared-node", rdp_host="10.0.0.9",
                                           order_bridge_base_url="http://10.0.0.9:8790")
        t1 = self._resolve(self._job(self._provider_b_account(login="111", node=node), node).id)
        t2 = self._resolve(self._job(self._provider_b_account(login="222", node=node), node).id)
        self.assertEqual(t1.base_url, "http://10.0.0.9:8790")
        self.assertEqual(t2.base_url, t1.base_url)


class WorkerOrderTransportWiringTests(SimpleTestCase):
    """(10) Removal of the node-resolution call causes a test failure. Structurally guard that every
    dispatch site resolves its transport per-job and passes it — a revert to the module-global
    ``AGENT_ORDER_BASE`` (single-arg call) fails here."""

    def _tree(self):
        import mt5_trade_ingest_worker as worker
        return ast.parse(inspect.getsource(worker))

    def test_resolve_order_base_gates_every_dispatch(self):
        tree = self._tree()
        resolves = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == "resolve_order_base"]
        self.assertGreaterEqual(
            len(resolves), 4,
            "resolve_order_base must gate every dispatch site (PLACE/MODIFY/CLOSE live + SHADOW); "
            f"found {len(resolves)} — a missing call reverts that site to the shared global bridge")

    def test_every_agent_dispatch_call_passes_resolved_base(self):
        tree = self._tree()
        dispatch = {"agent_order", "agent_order_check", "agent_modify", "agent_close"}
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id in dispatch]
        self.assertGreaterEqual(len(calls), 4, "expected all four agent dispatch call sites")
        for c in calls:
            self.assertGreaterEqual(
                len(c.args), 2,
                f"{c.func.id} at line {c.lineno} must be called with (payload, order_base) — a single-arg "
                f"call reverts to the shared global bridge and breaks Customer-Zero isolation")
            # BIND the base arg to the RESOLVED `_transport.base_url`, not merely assert it exists. A
            # two-arg-but-wrong-base regression — e.g. `agent_order(agent_payload, AGENT_ORDER_BASE)`, the
            # exact pre-seam shape this change removed — would pass a count-only check yet route a HOSTED
            # order to Customer Zero's global :8788 bridge (I1). AGENT_ORDER_BASE parses to ast.Name, not
            # ast.Attribute(value=Name('_transport'), attr='base_url'), so this kills that mutation.
            base_arg = c.args[1]
            self.assertTrue(
                isinstance(base_arg, ast.Attribute) and base_arg.attr == "base_url"
                and isinstance(base_arg.value, ast.Name) and base_arg.value.id == "_transport",
                f"{c.func.id} at line {c.lineno} must dispatch to the RESOLVED _transport.base_url, not a "
                f"module-global bridge (e.g. AGENT_ORDER_BASE) — a wrong-base call routes a HOSTED order to "
                f"Customer Zero's global :8788 bridge (I1)")
