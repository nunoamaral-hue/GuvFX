"""ADR-0082 — set_node_capacity governed primitive tests.

Positive + adversarial coverage of the per-node capacity primitive and its fail-closed, occupancy-based
physical-host certified-safe guard, plus a REAL-PATH concurrency test (PostgreSQL row locks) proving a
capacity change serialises behind a co-resident occupancy increase and re-evaluates the guard under the lock.

Requires a real DB with row locking — the backend test DB is PostgreSQL.
"""
from __future__ import annotations

import threading
import time
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase

from execution.models import HostCapacity, NodeCapacityAudit, TerminalNode
from execution.node_capacity import (
    IsolationInvariantError,
    R_BELOW_OCCUPANCY,
    R_CERT_MISSING,
    R_HOST_AMBIGUOUS,
    R_HOST_OVER_SAFE,
    R_MAX_OUT_OF_RANGE,
    R_NODE_INACTIVE,
    R_NODE_NOT_FOUND,
    R_OK,
    R_SAFE_MISSING,
    R_WOULD_EXCEED_SAFE,
    apply_node_capacity_change,
    evaluate_node_capacity_change,
)
from trading.models import BrokerServer, TradingAccount

U = get_user_model()
_n = 0

HOST = "10.7.7.7"
OTHER = "10.7.7.6"


def _uniq():
    global _n
    _n += 1
    return f"88{_n:05d}"


def _node(host=HOST, *, max_accounts=16, status=None):
    login = _uniq()
    return TerminalNode.objects.create(
        hostname=f"nc-{login}", rdp_host=host, max_accounts=max_accounts,
        status=status or TerminalNode.Status.ACTIVE)


def _srv():
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    return srv


def _occupy(node, count):
    """Bind ``count`` live accounts to ``node`` so node_occupant_count(node) == count."""
    srv = _srv()
    for _ in range(count):
        login = _uniq()
        user = U.objects.create_user(username=f"oc{login}", email=f"{login}@x.invalid", password="x")
        TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number=login,
                                      is_demo=True, is_active=True, broker_server=srv, terminal_node=node)


def _hostcap(host=HOST, *, max_total=20, safe=18):
    row, _ = HostCapacity.objects.update_or_create(
        physical_host_key=host,
        defaults=dict(max_total_occupants=max_total, certified_safe=safe))
    return row


class EvaluateGuardTests(TestCase):
    """Pure evaluator — the fail-closed decision table."""

    def test_occupancy_equal_to_proposed_is_allowed(self):
        n = _node(max_accounts=16)
        _occupy(n, 5)
        _hostcap(max_total=20, safe=18)
        plan = evaluate_node_capacity_change(node_id=n.pk, proposed_max=5)   # == occupancy
        self.assertTrue(plan.ok)
        self.assertEqual(plan.reason, R_OK)

    def test_proposed_below_occupancy_rejected(self):
        n = _node(max_accounts=16)
        _occupy(n, 5)
        _hostcap()
        plan = evaluate_node_capacity_change(node_id=n.pk, proposed_max=4)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.reason, R_BELOW_OCCUPANCY)

    def test_missing_certification_rejected(self):
        n = _node(max_accounts=16)
        _occupy(n, 2)
        # no HostCapacity row for this host
        plan = evaluate_node_capacity_change(node_id=n.pk, proposed_max=17)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.reason, R_CERT_MISSING)

    def test_certified_safe_missing_rejected(self):
        n = _node(max_accounts=16)
        _occupy(n, 2)
        HostCapacity.objects.update_or_create(
            physical_host_key=HOST, defaults=dict(max_total_occupants=20, certified_safe=None))
        plan = evaluate_node_capacity_change(node_id=n.pk, proposed_max=17)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.reason, R_SAFE_MISSING)

    def test_host_safe_limit_enforced_taurex_shape(self):
        """safe=18, target occ=16, one co-resident occupant=1 → 17 OK (17+1=18), 18 REFUSED (18+1=19)."""
        target = _node(max_accounts=16)
        coresident = _node(max_accounts=10)        # same HOST
        _occupy(target, 16)
        _occupy(coresident, 1)
        _hostcap(max_total=20, safe=18)
        ok = evaluate_node_capacity_change(node_id=target.pk, proposed_max=17)
        self.assertTrue(ok.ok, ok.reason)
        self.assertEqual(ok.other_node_occupancy, 1)
        over = evaluate_node_capacity_change(node_id=target.pk, proposed_max=18)
        self.assertFalse(over.ok)
        self.assertEqual(over.reason, R_WOULD_EXCEED_SAFE)

    def test_draining_and_offline_coresident_occupancy_counted(self):
        """A RAISE that would pass if co-resident DRAINING/OFFLINE occupants were ignored must be REFUSED."""
        target = _node(max_accounts=3)             # current ceiling 3 so proposed 4 is a RAISE
        draining = _node(max_accounts=10, status=TerminalNode.Status.DRAINING)
        offline = _node(max_accounts=10, status=TerminalNode.Status.OFFLINE)
        _occupy(target, 2)
        _occupy(draining, 4)
        _occupy(offline, 3)                        # other_occ = 4 + 3 = 7
        _hostcap(max_total=20, safe=10)
        # raise 3 → 4 : 4 + 7 = 11 > 10 → refused ONLY because draining+offline are counted (else 4+0 ok).
        plan = evaluate_node_capacity_change(node_id=target.pk, proposed_max=4)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.reason, R_WOULD_EXCEED_SAFE)
        self.assertEqual(plan.other_node_occupancy, 7)

    def test_whitespace_variant_coresident_is_counted(self):
        """A co-resident node whose rdp_host differs only by trailing whitespace is still counted, so a RAISE
        that would wrongly pass under a whitespace-sensitive match is REFUSED."""
        target = _node(host="10.7.7.7", max_accounts=3)
        ws = _node(host="10.7.7.7 ", max_accounts=10)   # trailing space — same physical host
        _occupy(target, 2)
        _occupy(ws, 5)                                  # host_occ = 7 (only if the whitespace variant counts)
        _hostcap(host="10.7.7.7", max_total=20, safe=7)  # host_occ(7) <= safe(7) so rule 4 does NOT pre-empt rule 5
        plan = evaluate_node_capacity_change(node_id=target.pk, proposed_max=4)   # raise 3->4; 4+5=9 > 7
        self.assertFalse(plan.ok)
        self.assertEqual(plan.reason, R_WOULD_EXCEED_SAFE)
        self.assertEqual(plan.other_node_occupancy, 5)

    def test_blank_rdp_host_rejected(self):
        """Blank rdp_host → physical host unresolvable → fail closed in BOTH evaluate and apply (no write)."""
        n = _node(host="", max_accounts=16)
        _hostcap()                                  # a HostCapacity exists for HOST, but node has no rdp_host
        self.assertEqual(evaluate_node_capacity_change(node_id=n.pk, proposed_max=17).reason, R_HOST_AMBIGUOUS)
        plan = apply_node_capacity_change(node_id=n.pk, proposed_max=17, reason="x", actor="t")
        self.assertFalse(plan.applied)
        self.assertEqual(plan.reason, R_HOST_AMBIGUOUS)
        n.refresh_from_db()
        self.assertEqual(n.max_accounts, 16)
        self.assertEqual(NodeCapacityAudit.objects.count(), 0)

    def test_host_already_over_safe_rejects_a_raise(self):
        target = _node(max_accounts=10)
        coresident = _node(max_accounts=16)
        _occupy(target, 4)
        _occupy(coresident, 4)                     # host occ = 8
        _hostcap(max_total=20, safe=5)             # already over safe
        plan = evaluate_node_capacity_change(node_id=target.pk, proposed_max=11)   # RAISE 10->11
        self.assertFalse(plan.ok)
        self.assertEqual(plan.reason, R_HOST_OVER_SAFE)

    def test_lowering_allowed_even_when_host_over_safe(self):
        """The rollback/tighten path: a lowering (>= node occupancy) is permitted even when the host legitimately
        sits above certified_safe — a ceiling reduction cannot worsen occupancy."""
        target = _node(max_accounts=18)
        coresident = _node(max_accounts=16)
        _occupy(target, 16)
        _occupy(coresident, 4)                     # host occ = 20, safe = 18 → over safe
        _hostcap(max_total=20, safe=18)
        # dry-run: a lowering 18 -> 16 (still >= occupancy 16) is allowed despite host over safe
        plan = evaluate_node_capacity_change(node_id=target.pk, proposed_max=16)
        self.assertTrue(plan.ok, plan.reason)
        # apply it: writes through
        applied = apply_node_capacity_change(node_id=target.pk, proposed_max=16, reason="rollback/tighten", actor="t")
        self.assertTrue(applied.applied)
        target.refresh_from_db()
        self.assertEqual(target.max_accounts, 16)
        self.assertEqual(NodeCapacityAudit.objects.count(), 1)

    def test_unknown_node_fails_closed(self):
        self.assertEqual(evaluate_node_capacity_change(node_hostname="does-not-exist", proposed_max=5).reason,
                         R_NODE_NOT_FOUND)
        self.assertEqual(evaluate_node_capacity_change(node_id=999999, proposed_max=5).reason, R_NODE_NOT_FOUND)

    def test_inactive_node_rejected(self):
        for st in (TerminalNode.Status.DRAINING, TerminalNode.Status.OFFLINE, TerminalNode.Status.DISABLED):
            n = _node(status=st)
            _hostcap()
            plan = evaluate_node_capacity_change(node_id=n.pk, proposed_max=5)
            self.assertEqual(plan.reason, R_NODE_INACTIVE)

    def test_invalid_values_fail_closed(self):
        n = _node(max_accounts=16)
        _occupy(n, 1)
        _hostcap(max_total=20, safe=18)
        for bad in (0, -1, 21):                    # below MIN, negative, above ceiling(20)
            plan = evaluate_node_capacity_change(node_id=n.pk, proposed_max=bad)
            self.assertFalse(plan.ok)
            self.assertEqual(plan.reason, R_MAX_OUT_OF_RANGE, f"value {bad}")


class ApplyAndCommandTests(TestCase):
    def test_dry_run_causes_zero_mutation(self):
        n = _node(max_accounts=16)
        _occupy(n, 16)
        _node(max_accounts=10)  # co-resident; occupy 0
        _hostcap(max_total=20, safe=18)
        out = StringIO()
        call_command("set_node_capacity", "--node-hostname", n.hostname, "--max", "17", stdout=out)
        n.refresh_from_db()
        self.assertEqual(n.max_accounts, 16)               # unchanged
        self.assertEqual(NodeCapacityAudit.objects.count(), 0)
        self.assertIn("DRY-RUN", out.getvalue())

    def test_apply_changes_exactly_one_node_and_writes_one_audit(self):
        target = _node(max_accounts=16)
        coresident = _node(max_accounts=10)
        other_host = _node(OTHER, max_accounts=16)
        _occupy(target, 16)
        _occupy(coresident, 1)
        _occupy(other_host, 5)
        _hostcap(max_total=20, safe=18)
        out = StringIO()
        call_command("set_node_capacity", "--node-hostname", target.hostname, "--max", "17",
                     "--apply", "--reason", "GFX-TEST recover one onboarding", "--actor", "ops@test",
                     "--correlation-id", "corr-1", stdout=out)
        target.refresh_from_db(); coresident.refresh_from_db(); other_host.refresh_from_db()
        self.assertEqual(target.max_accounts, 17)
        self.assertEqual(coresident.max_accounts, 10)      # unrelated node unchanged
        self.assertEqual(other_host.max_accounts, 16)      # different-host node unchanged
        audits = list(NodeCapacityAudit.objects.all())
        self.assertEqual(len(audits), 1)
        a = audits[0]
        self.assertEqual((a.previous_max_accounts, a.new_max_accounts), (16, 17))
        self.assertEqual(a.node_occupancy_at_decision, 16)
        self.assertEqual(a.host_occupancy_at_decision, 17)
        self.assertEqual(a.certified_safe, 18)
        self.assertEqual(a.ceiling, 20)
        self.assertEqual((a.actor, a.reason, a.correlation_id),
                         ("ops@test", "GFX-TEST recover one onboarding", "corr-1"))

    def test_apply_requires_reason(self):
        n = _node(max_accounts=16)
        _occupy(n, 1)
        _hostcap()
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("set_node_capacity", "--node-hostname", n.hostname, "--max", "17", "--apply")
        self.assertEqual(NodeCapacityAudit.objects.count(), 0)

    def test_refused_apply_writes_nothing(self):
        n = _node(max_accounts=16)
        _occupy(n, 16)
        _occupy(_node(max_accounts=10), 2)                 # other occ = 2
        _hostcap(max_total=20, safe=18)
        out = StringIO()
        # raise 16 -> 17 : 17 + 2 = 19 > 18 → refused → non-zero exit (CommandError), no write
        with self.assertRaises(CommandError) as ctx:
            call_command("set_node_capacity", "--node-hostname", n.hostname, "--max", "17",
                         "--apply", "--reason", "x", stdout=out)
        self.assertIn(R_WOULD_EXCEED_SAFE, str(ctx.exception))
        n.refresh_from_db()
        self.assertEqual(n.max_accounts, 16)
        self.assertEqual(NodeCapacityAudit.objects.count(), 0)

    def test_service_level_requires_reason(self):
        n = _node(max_accounts=16)
        _occupy(n, 1)
        _hostcap()
        for bad in ("", "   "):
            with self.assertRaises(ValueError):
                apply_node_capacity_change(node_id=n.pk, proposed_max=17, reason=bad, actor="t")
        self.assertEqual(NodeCapacityAudit.objects.count(), 0)

    def test_isolation_change_during_apply_rolls_back(self):
        """Rule 8: if the Customer-Zero forbidden-node set differs across the write, the whole transaction rolls
        back (no max_accounts change, no audit). Forced here by patching the isolation source to differ."""
        target = _node(max_accounts=16)
        _occupy(target, 16)
        _occupy(_node(max_accounts=10), 1)
        _hostcap(max_total=20, safe=18)
        with mock.patch("hosted_workspace.tenant_isolation.forbidden_execution_node_ids",
                        side_effect=[set(), {999999}]):      # before != after
            with self.assertRaises(IsolationInvariantError):
                apply_node_capacity_change(node_id=target.pk, proposed_max=17, reason="x", actor="t")
        target.refresh_from_db()
        self.assertEqual(target.max_accounts, 16)            # rolled back
        self.assertEqual(NodeCapacityAudit.objects.count(), 0)

    def test_audit_failure_rolls_back_capacity_change(self):
        n = _node(max_accounts=16)
        _occupy(n, 16)
        _occupy(_node(max_accounts=10), 1)
        _hostcap(max_total=20, safe=18)
        with mock.patch("execution.node_capacity.NodeCapacityAudit.objects.create",
                        side_effect=RuntimeError("audit sink down")):
            with self.assertRaises(RuntimeError):
                apply_node_capacity_change(node_id=n.pk, proposed_max=17, reason="x", actor="t")
        n.refresh_from_db()
        self.assertEqual(n.max_accounts, 16)               # rolled back
        self.assertEqual(NodeCapacityAudit.objects.count(), 0)

    def test_unrelated_isolation_and_controls_unchanged(self):
        from hosted_workspace.tenant_isolation import forbidden_execution_node_ids
        target = _node(max_accounts=16)
        _occupy(target, 16)
        _occupy(_node(max_accounts=10), 1)
        cap = _hostcap(max_total=20, safe=18)
        before_forbidden = set(forbidden_execution_node_ids())
        apply_node_capacity_change(node_id=target.pk, proposed_max=17, reason="x", actor="t")
        cap.refresh_from_db()
        self.assertEqual(set(forbidden_execution_node_ids()), before_forbidden)   # isolation unchanged
        self.assertEqual((cap.max_total_occupants, cap.certified_safe), (20, 18))  # host ceiling/safe untouched

    def test_output_never_exposes_credentials_or_logins(self):
        target = _node(max_accounts=16)
        srv = _srv()
        secret_login = "SECRET-LOGIN-9999"
        user = U.objects.create_user(username="sl", email="sl@x.invalid", password="pw-not-printed")
        TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number=secret_login,
                                      broker_password="pw-not-printed", is_demo=True, is_active=True,
                                      broker_server=srv, terminal_node=target)
        _hostcap(max_total=20, safe=18)
        out = StringIO()
        call_command("set_node_capacity", "--node-hostname", target.hostname, "--max", "17",
                     "--apply", "--reason", "x", stdout=out)
        text = out.getvalue()
        self.assertNotIn(secret_login, text)
        self.assertNotIn("pw-not-printed", text)
        self.assertNotIn("password", text.lower())


class ConcurrencyTests(TransactionTestCase):
    """Real-path: a capacity change serialises BEHIND a co-resident occupancy increase (both lock the same
    co-resident node rows) and re-evaluates the guard under the lock → fails closed rather than oversubscribing."""

    def test_capacity_change_racing_occupancy_increase_fails_closed(self):
        CHOST = "10.7.7.3"                      # distinct host → no cross-test fixture collision
        target = _node(CHOST, max_accounts=1)   # low ceiling so proposed 2 is a genuine RAISE
        coresident = _node(CHOST, max_accounts=10)
        _occupy(target, 0)
        _occupy(coresident, 1)                 # host occ = 1; safe = 3. Raise target 1->2 is safe PRE-bump
                                               # (2 + other 1 = 3 <= 3) but UNsafe once the bump lands first.
        _hostcap(CHOST, max_total=20, safe=3)

        barrier = threading.Barrier(2)
        results: dict = {}

        def bumper():
            try:
                with transaction.atomic():
                    # Lock the co-resident node rows FIRST (same rows the capacity apply will lock).
                    list(TerminalNode.objects.select_for_update()
                         .filter(rdp_host__iexact="10.7.7.3").order_by("pk"))
                    barrier.wait()             # release changer; its apply now blocks on these locks
                    _occupy(coresident, 1)     # occupancy 2 -> 3 within the held lock
                    time.sleep(0.5)            # ensure changer is blocked on the lock
                # commit releases the locks
            except Exception as e:             # pragma: no cover
                results["bumper_err"] = repr(e)
            finally:
                connection.close()

        def changer():
            try:
                barrier.wait()
                results["plan"] = apply_node_capacity_change(
                    node_id=target.pk, proposed_max=2, reason="race", actor="t")
            except Exception as e:             # pragma: no cover
                results["changer_err"] = repr(e)
            finally:
                connection.close()

        tb = threading.Thread(target=bumper)
        tc = threading.Thread(target=changer)
        tb.start(); tc.start()
        tb.join(timeout=10); tc.join(timeout=10)

        self.assertNotIn("bumper_err", results, results.get("bumper_err"))
        self.assertNotIn("changer_err", results, results.get("changer_err"))
        plan = results["plan"]
        # The occupancy increase committed first; the capacity change re-read occupancy UNDER THE LOCK and
        # fails closed instead of certifying a ceiling that would permit occupancy past certified-safe.
        self.assertFalse(plan.applied)
        self.assertEqual(plan.reason, R_WOULD_EXCEED_SAFE)
        target.refresh_from_db()
        self.assertEqual(target.max_accounts, 1)           # unchanged
        self.assertEqual(NodeCapacityAudit.objects.count(), 0)
        from hosted_workspace.provisioning import node_occupant_count
        coresident.refresh_from_db()
        self.assertEqual(node_occupant_count(coresident), 2)   # the occupancy increase (1 -> 2) did land
