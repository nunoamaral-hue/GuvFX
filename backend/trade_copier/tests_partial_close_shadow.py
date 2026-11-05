"""ADR-0074 Phase-3/7/8 — DARK partial-close SHADOW admission + dispatch wiring.

``model_partial_close`` completes the partial-close path for the DARK pass: it MODELS the proportional
reduction against the durable opened follower volume using the SAME pure calc + durable constraints as the live
admission, performs NO live broker read, and creates NO ExecutionJob. The lifecycle dispatch records the
modeled reduction. Mutation-killers: the modeled reduction equals the pure calc; admission fails closed on the
same ledger faults as the live path; and the dispatch never creates a job.
"""
from decimal import Decimal

from django.test import override_settings

from execution.models import ExecutionJob
from trade_copier import lifecycle_dispatch
from trade_copier.execution_producer import _partial_reduction, model_partial_close
from trade_copier.models import (CopierLifecycleDispatch, CopierPositionLifecycle, LifecycleStateChoices,
                                 LifecyclePendingOp)
from trade_copier.replication import BrokerConstraints
from trade_copier.tests_partial_close_producer import PartialFixture

ENABLED = dict(COPIER_ENABLED=True)
DISPATCH = dict(COPIER_ENABLED=True, COPIER_LIFECYCLE_DISPATCH_ENABLED=True)


class ModelPartialCloseTests(PartialFixture):
    def test_models_reduction_against_opened_volume_no_broker_read(self):
        # Opened follower volume = 0.05 (the matched OPEN target_volume); master ratio 0.4 → raw 0.02 → 0.02.
        f, rep = self._ready_partial(before="1.00", after="0.60")
        ok, ticket, reduction, remaining, reason = model_partial_close(rep)
        self.assertTrue(ok, reason)
        self.assertEqual(ticket, 777)
        exp_ok, exp_red, exp_rem, _ = _partial_reduction(
            1.00, 0.60, 0.05, BrokerConstraints(min_volume=0.01, lot_step=0.01, max_volume=100, authoritative=True))
        self.assertTrue(exp_ok)
        self.assertAlmostEqual(reduction, exp_red, places=8)
        self.assertAlmostEqual(remaining, exp_rem, places=8)
        self.assertEqual(ExecutionJob.objects.count(), 0)   # INVARIANT: modeling creates no job

    def test_no_verified_ticket_fails_closed(self):
        f = self._follower()
        self._instrument(f.follower_account)
        rep = self._partial_rep(f, self._partial_event())    # no matched OPEN
        ok, _t, _red, _rem, reason = model_partial_close(rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_verified_follower_ticket")

    def test_non_authoritative_constraints_fail_closed(self):
        f, rep = self._ready_partial(instrument=False)
        ok, _t, _red, _rem, reason = model_partial_close(rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "constraints_not_authoritative")

    def test_rounds_to_zero_fails_closed(self):
        # modeled opened volume 0.05, but a tiny master ratio → the pure calc rounds to zero.
        f, rep = self._ready_partial(before="1.00", after="0.995")   # ratio 0.005 → raw 0.00025 → 0
        ok, _t, _red, _rem, reason = model_partial_close(rep)
        self.assertFalse(ok)
        self.assertEqual(reason, "rounds_to_zero")


@override_settings(**DISPATCH)
class PartialDispatchWiringTests(PartialFixture):
    def _lifecycle(self, ticket=1):
        return CopierPositionLifecycle.objects.create(
            master=self.master, follower=self.follower_obj, master_position_ticket=ticket,
            state=LifecycleStateChoices.CLOSE_PENDING, pending_op=LifecyclePendingOp.CLOSE,
            is_terminal=False, applied_sequence=4)

    def test_dispatch_records_modeled_partial_close_no_job(self):
        self.follower_obj, rep = self._ready_partial(before="1.00", after="0.60")
        lc = self._lifecycle(ticket=1)
        out = lifecycle_dispatch.dispatch_one(lc)
        self.assertGreaterEqual(out["recorded"], 1)
        d = CopierLifecycleDispatch.objects.get(lifecycle=lc, op="PARTIAL_CLOSE")
        self.assertTrue(d.admit_ok)
        self.assertTrue(d.would_execute)
        self.assertTrue(d.idempotency_key.startswith("copier-pclose:"))
        self.assertIn("modeled_reduction", d.detail)
        self.assertTrue(d.detail.get("requires_live_volume_at_arm"))
        self.assertEqual(ExecutionJob.objects.count(), 0)   # INVARIANT: dispatch creates no job

    def test_dispatch_is_idempotent(self):
        self.follower_obj, rep = self._ready_partial(before="1.00", after="0.60")
        lc = self._lifecycle(ticket=1)
        lifecycle_dispatch.dispatch_one(lc)
        lifecycle_dispatch.dispatch_one(lc)
        self.assertEqual(CopierLifecycleDispatch.objects.filter(lifecycle=lc, op="PARTIAL_CLOSE").count(), 1)
