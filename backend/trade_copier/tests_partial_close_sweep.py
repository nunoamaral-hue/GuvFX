"""ADR-0056 F4 — background reconciliation sweep + crash-boundary matrix. The sweep is READ-ONLY: it
re-reads the authoritative remaining volume and finalizes a stranded partial-close job; it NEVER calls
order_send, re-dispatches, or requeues to PENDING. Crash boundaries proven: after claim-before-authorize,
after authorize-before-bridge, after bridge-before-complete, after completion-before-reconcile. DARK;
nothing is armed/executed."""
from django.test import override_settings
from django.utils import timezone

from execution.models import ExecutionJob
from trade_copier import consumer, reconcile
from trade_copier.tests_consumer import ARMED, NODE, ConsumerFixture, _fetch


def _raises_if_read(_acct):
    raise AssertionError("the sweep must not perform an authoritative read on this boundary")


class SweepFixture(ConsumerFixture):
    def _expire_lease(self):
        self.job.refresh_from_db()
        self.job.lease_expires_at = timezone.now() - timezone.timedelta(hours=1)   # well past the grace window
        self.job.save(update_fields=["lease_expires_at"])

    def _sweep(self, *, fetch=None):
        return consumer.sweep_partial_close_reconcile(fetch=fetch)


# ── crash-boundary matrix (none may cause a second broker send) ───────────────────────────────────────
class CrashBoundaryTests(SweepFixture):
    @override_settings(**ARMED)
    def test_boundary_1_claim_before_authorize_no_read_dispatch_refused(self):
        # Claimed, but authorize-send never happened (worker died) -> NOTHING was sent -> terminal
        # dispatch-refused with NO authoritative read (fetch would raise if called).
        self._claim(self._worker())
        self._expire_lease()
        out = self._sweep(fetch=_raises_if_read)
        self.assertEqual(out["finalized"], 1)
        self.job.refresh_from_db(); self.rep.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.FAILED)
        self.assertEqual(self.rep.reconciliation_status, reconcile.PARTIAL_DISPATCH_REFUSED)

    @override_settings(**ARMED)
    def test_boundary_2_authorize_before_bridge_reads_and_finalizes(self):
        # Authorized but the bridge call never completed. The sweep CANNOT assume the single send did not
        # land, so it reads authoritatively. Position unchanged (1.00) vs intended 0.60 -> divergence -> FAILED.
        w = self._worker()
        self._authorize(w, self._claim(w))
        self._expire_lease()
        out = self._sweep(fetch=_fetch(777, 1.00))
        self.assertEqual(out["finalized"], 1)
        self.job.refresh_from_db(); self.rep.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.FAILED)
        self.assertEqual(self.rep.reconciliation_status, reconcile.PARTIAL_DIVERGENT_VOLUME)

    @override_settings(**ARMED)
    def test_boundary_3_bridge_before_complete_reconciles_success(self):
        # The single send landed (broker left the intended 0.60) but the worker died before /complete. The
        # sweep's authoritative read reconciles it to SUCCESS — with no second send.
        w = self._worker()
        self._authorize(w, self._claim(w))
        self._expire_lease()
        out = self._sweep(fetch=_fetch(777, 0.60))
        self.assertEqual(out["finalized"], 1)
        self.job.refresh_from_db(); self.rep.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.SUCCESS)
        self.assertEqual(self.rep.reconciliation_status, reconcile.PARTIAL_RECONCILED)

    @override_settings(**ARMED)
    def test_boundary_4_completion_before_reconcile_finishes(self):
        # A completion marked the job COMPLETING + recon SENT, then crashed before the read. The sweep
        # re-reads and finalizes; never a second send.
        w = self._worker()
        token, _ = self._dispatch_authorized(w)
        self.job.refresh_from_db()
        res = dict(self.job.result or {}); res["phase"] = consumer.PHASE_COMPLETING
        self.job.result = res
        self.job.save(update_fields=["result"])
        reconcile._set_recon(self.rep, reconcile.PARTIAL_SENT)
        self._expire_lease()
        out = self._sweep(fetch=_fetch(777, 0.60))
        self.assertEqual(out["finalized"], 1)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.SUCCESS)


# ── sweep behaviour ───────────────────────────────────────────────────────────────────────────────────
class SweepBehaviourTests(SweepFixture):
    def test_sweep_inert_while_dark(self):
        # No flags -> copier disabled -> sweep is a no-op (never touches a job).
        out = consumer.sweep_partial_close_reconcile(fetch=_fetch(777, 0.60))
        self.assertFalse(out["enabled"])
        self.assertEqual(out["finalized"], 0)

    @override_settings(**ARMED)
    def test_sweep_ignores_fresh_lease(self):
        # A job whose lease is still within the grace window is NOT swept (it may still have a live worker).
        w = self._worker()
        self._authorize(w, self._claim(w))
        out = self._sweep(fetch=_fetch(777, 0.60))      # lease not expired
        self.assertEqual(out["scanned"], 0)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.RUNNING)

    @override_settings(**ARMED)
    def test_read_unavailable_keeps_running_and_counts_attempt(self):
        w = self._worker()
        self._authorize(w, self._claim(w))
        self._expire_lease()
        out = self._sweep(fetch=_fetch(None, 0, ok=False, reason="snapshot_identity_login_mismatch"))
        self.assertEqual(out["finalized"], 0)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.RUNNING)      # non-terminal; retried next sweep
        self.assertEqual((self.job.result or {}).get("recon_attempts"), 1)

    @override_settings(**ARMED)
    def test_escalates_to_operator_review_after_max_attempts(self):
        w = self._worker()
        self._authorize(w, self._claim(w))
        self._expire_lease()
        self.job.refresh_from_db()
        res = dict(self.job.result or {}); res["recon_attempts"] = consumer._SWEEP_MAX_RECON_ATTEMPTS
        self.job.result = res
        self.job.save(update_fields=["result"])
        out = self._sweep(fetch=_fetch(None, 0, ok=False, reason="snapshot_identity_login_mismatch"))
        self.assertEqual(out["finalized"], 1)
        self.job.refresh_from_db(); self.rep.refresh_from_db()
        self.assertEqual(self.job.status, ExecutionJob.Status.FAILED)
        self.assertEqual(self.rep.reconciliation_status, reconcile.PARTIAL_OPERATOR_REVIEW)

    @override_settings(**ARMED)
    def test_second_sweep_is_noop_after_finalize(self):
        w = self._worker()
        self._authorize(w, self._claim(w))
        self._expire_lease()
        self.assertEqual(self._sweep(fetch=_fetch(777, 0.60))["finalized"], 1)
        again = self._sweep(fetch=_fetch(777, 0.60))        # job now terminal -> nothing to do
        self.assertEqual(again["finalized"], 0)
        self.assertEqual(again["scanned"], 0)
