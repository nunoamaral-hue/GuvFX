"""P5: cold-boot recovery ORCHESTRATION — staggering (per-pass establish cap), per-tenant failure isolation, and
the read-only operational-visibility block. Builds on P4-b's reconciler; DARK; injected seams, no host.
"""
from __future__ import annotations

from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from hosted_workspace import session_reconciler as SR
# Reuse the P4-b fixtures (armed/matched workspace, forced non-reserved ids, resolver/probe helpers).
from hosted_workspace.tests_p4b_session_reconciler import ARMED, RECON, _account, _armed_ws, _probe, _resolver


def _refresh(ws):
    ws.refresh_from_db()
    return ws


@override_settings(**ARMED)
class StaggeringTests(TestCase):
    def test_cold_boot_surge_is_capped_per_pass(self):
        # More ABSENT candidates than the cap => only MAX_ESTABLISH_PER_CYCLE self-connects this pass; the rest are
        # DEFERRED (rate_limited), not dropped, and stay ABSENT for the next cron tick (idempotent staggering).
        n = SR.MAX_ESTABLISH_PER_CYCLE + 2
        for _ in range(n):
            acct, node = _account()
            _armed_ws(acct, node)
        calls = []

        def establish(ws, account, node, status):
            calls.append(account.id)
            return {"ok": True}
        with mock.patch.object(SR, "workspace_delivery_ready", return_value=True):
            out = SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=_probe("ABSENT"),
                                                   establish_fn=establish)
        self.assertEqual(out["candidates"], n)
        self.assertEqual(out["established"], SR.MAX_ESTABLISH_PER_CYCLE)
        self.assertEqual(out["skipped_rate_limited"], 2)
        self.assertEqual(len(calls), SR.MAX_ESTABLISH_PER_CYCLE)     # only the capped few actually self-connect

    def test_rate_limited_candidate_burns_no_claim(self):
        # A deferred (rate-limited) candidate must not consume its attempt budget — it retries fresh next cycle.
        wss = []
        for _ in range(SR.MAX_ESTABLISH_PER_CYCLE + 1):
            acct, node = _account()
            wss.append(_armed_ws(acct, node))
        with mock.patch.object(SR, "workspace_delivery_ready", return_value=True):
            SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=_probe("ABSENT"),
                                             establish_fn=lambda *a: {"ok": True})
        # at least one workspace was rate-limited; every rate-limited one still has count 0 (no claim burned)
        counts = sorted(_refresh(w).session_recovery_count for w in wss)
        self.assertEqual(counts.count(0), 1)                        # exactly the one deferred candidate


@override_settings(**RECON)
class ProbeOnlyNotCappedTests(TestCase):
    def test_probe_only_classification_is_never_rate_limited(self):
        # Darkness guard (MED-1): with the arm sub-gate OFF, MORE than the cap of ABSENT candidates must ALL be
        # classified/audited (skipped_not_armed) and NONE rate-limited — the cap must never throttle probe-only.
        n = SR.MAX_ESTABLISH_PER_CYCLE + 2
        for _ in range(n):
            acct, node = _account()
            _armed_ws(acct, node)
        calls = []
        out = SR.run_hosted_session_reconciler(
            executor_resolver=_resolver(), probe_fn=_probe("ABSENT"),
            establish_fn=lambda *a: calls.append(1) or {"ok": True})   # passed, but arm gate OFF => never called
        self.assertEqual(out["absent"], n)
        self.assertEqual(out["skipped_not_armed"], n)
        self.assertEqual(out["skipped_rate_limited"], 0)
        self.assertEqual(calls, [])                                    # zero self-connect / zero decryption


@override_settings(**ARMED)
class CrossPassConvergenceTests(TestCase):
    def test_deferred_candidate_establishes_on_the_next_pass(self):
        # MED-2: the core idempotent-staggering claim — a candidate deferred by the cap in pass 1 converges in pass 2.
        n = SR.MAX_ESTABLISH_PER_CYCLE + 1
        wss = []
        for _ in range(n):
            acct, node = _account()
            wss.append(_armed_ws(acct, node))
        established = []

        def establish(ws, account, node, status):
            established.append(account.id)
            return {"ok": True}
        with mock.patch.object(SR, "workspace_delivery_ready", return_value=True):
            out1 = SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=_probe("ABSENT"),
                                                    establish_fn=establish)
        self.assertEqual(out1["established"], SR.MAX_ESTABLISH_PER_CYCLE)
        self.assertEqual(out1["skipped_rate_limited"], 1)
        deferred = [w.trading_account_id for w in wss if _refresh(w).session_recovery_count == 0]
        self.assertEqual(len(deferred), 1)                            # exactly one deferred (no claim burned)

        with mock.patch.object(SR, "workspace_delivery_ready", return_value=True):
            out2 = SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=_probe("ABSENT"),
                                                    establish_fn=establish)
        self.assertEqual(out2["established"], 1)                      # only the deferred one establishes now
        self.assertEqual(out2["skipped_cooldown"], SR.MAX_ESTABLISH_PER_CYCLE)  # the pass-1 four are cooling down
        self.assertIn(deferred[0], established)                       # the previously-deferred candidate converged
        # the count-0 backlog is fully drained (idempotent convergence)
        self.assertEqual([w.trading_account_id for w in wss if _refresh(w).session_recovery_count == 0], [])


@override_settings(**ARMED)
class PerTenantIsolationTests(TestCase):
    def test_one_tenant_failure_does_not_block_siblings(self):
        accts = []
        for _ in range(3):
            acct, node = _account()
            _armed_ws(acct, node)
            accts.append(acct.id)
        target = accts[1]
        calls = []

        def establish(ws, account, node, status):
            calls.append(account.id)
            if account.id == target:
                raise RuntimeError("one tenant's self-connect explodes")
            return {"ok": True}
        with mock.patch.object(SR, "workspace_delivery_ready", return_value=True):
            out = SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=_probe("ABSENT"),
                                                   establish_fn=establish)
        self.assertEqual(sorted(calls), sorted(accts))              # ALL tenants attempted (no early abort)
        self.assertEqual(out["established"], 2)                     # the two healthy siblings still recovered
        self.assertEqual(out["errors"], 1)                         # the exploding tenant is isolated, fail-closed


class VisibilityBlockTests(TestCase):
    def test_block_reports_candidates_and_open_alerts(self):
        from reliability.constants import Component
        from reliability.models import AlertEvent
        from reliability.services.operations_summary import _session_recovery_block
        # two armed STALE workspaces (recovery candidates); raise a session-down alert for one.
        a1, n1 = _account()
        w1 = _armed_ws(a1, n1)
        a2, n2 = _account()
        _armed_ws(a2, n2, session_recovery_count=1)
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.CRITICAL, component=Component.MT5_TERMINAL, trading_account_id=a1.id,
            title="x", body="y", dedup_key=SR._alert_dedup_key(a1.id), status=AlertEvent.Status.OPEN,
            detail={"account_id": a1.id})
        with override_settings(**ARMED):
            block = _session_recovery_block(timezone.now())
        self.assertEqual(block["armed_workspaces"], 2)
        self.assertEqual(block["stale_recovery_candidates"], 2)     # both stale (default fixture) => candidates
        self.assertEqual(block["in_flight_recovery"], 1)           # w2 carries a recovery count
        self.assertEqual(block["open_session_down_alerts"], 1)
        self.assertEqual(block["status"], "WARNING")
        self.assertTrue(block["reconciler_enabled"])
        self.assertTrue(block["self_connect_armed"])

    def test_block_healthy_when_no_alerts(self):
        from reliability.services.operations_summary import _session_recovery_block
        with override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_SESSION_RECONCILER_ENABLED="1"):
            block = _session_recovery_block(timezone.now())
        self.assertEqual(block["status"], "HEALTHY")
        self.assertFalse(block["self_connect_armed"])              # arm sub-gate off by default
