"""Phase-7 lifecycle→producer SHADOW dispatch tests (ADR-0074).

The load-bearing invariant: the dispatch pass records synthetic ``CopierLifecycleDispatch`` rows and creates
**zero** ``ExecutionJob`` — it never calls ``produce_*``. Also: gated off, idempotent, LINKED not dispatched,
PARTIAL admission MODELED (ledger-only, no broker read; ADR-0074 Phase-3 completion)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution.models import ExecutionJob
from trade_copier import services
from trade_copier.lifecycle_dispatch import run_lifecycle_dispatch_pass
from trade_copier.models import (
    CopierLifecycleDispatch, CopierMaster, CopierPositionLifecycle, CopierReplication,
    MasterEvent, MasterObservationPoll, ReplicationStatus,
)
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
DISPATCH_ON = dict(COPIER_ENABLED=True, COPIER_LIFECYCLE_DISPATCH_ENABLED=True)


class DispatchBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.bs = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6Technologies-Demo",
                                              environment="demo")
        self.macct = TradingAccount.objects.create(user=self.user, name="m", broker_name="IS6",
                                                   account_number="1000001", is_demo=True, is_active=True,
                                                   broker_server=self.bs)
        self.master = CopierMaster.objects.create(trading_account=self.macct, observe_enabled=True)
        self.poll = MasterObservationPoll.objects.create(master=self.master, observed_at=timezone.now(),
                                                         read_at=timezone.now(), position_count=1)
        facct = TradingAccount.objects.create(user=self.user, name="f", broker_name="IS6",
                                              account_number="2000001", is_demo=True, is_active=True,
                                              broker_server=self.bs)
        self.follower = services.add_follower(self.master, facct, multiplier=Decimal("0.5"))
        services.set_follower_enabled(self.follower, True)
        self._seq = 0

    def _event(self, etype, ticket=1):
        self._seq += 1
        return MasterEvent.objects.create(
            master=self.master, sequence=self._seq, from_poll=self.poll, event_type=etype,
            position_ticket=ticket, symbol="XAUUSD", side="BUY", volume_before=Decimal("0"),
            volume_after=Decimal("0.10"), volume_delta=Decimal("0.10"), observed_at=timezone.now())

    def _rep(self, event, op):
        return CopierReplication.objects.create(
            master_event=event, follower=self.follower, requested_op=op,
            status=ReplicationStatus.SHADOW_RECORDED, target_volume=Decimal("0.05"), follower_symbol="XAUUSD")

    def _lifecycle(self, state, *, ticket=1, pending="NONE", follower_ticket=None, terminal=False):
        return CopierPositionLifecycle.objects.create(
            master=self.master, follower=self.follower, master_position_ticket=ticket, state=state,
            pending_op=pending, is_terminal=terminal, entry_reference_at=timezone.now(),
            follower_ticket=follower_ticket)


class DispatchTests(DispatchBase):
    def test_gate_off_records_nothing(self):
        self._lifecycle("OBSERVED_OPEN")
        self._rep(self._event(MasterEvent.EventType.OPEN), "OPEN")
        with override_settings(COPIER_ENABLED=True, COPIER_LIFECYCLE_DISPATCH_ENABLED=False):
            out = run_lifecycle_dispatch_pass()
        self.assertFalse(out["enabled"])
        self.assertEqual(CopierLifecycleDispatch.objects.count(), 0)

    @override_settings(**DISPATCH_ON)
    def test_observed_open_records_shadow_no_job(self):
        self._lifecycle("OBSERVED_OPEN")
        rep = self._rep(self._event(MasterEvent.EventType.OPEN), "OPEN")
        out = run_lifecycle_dispatch_pass()
        self.assertEqual(out["recorded"], 1)
        d = CopierLifecycleDispatch.objects.get()
        self.assertEqual(d.op, "OPEN")
        self.assertEqual(d.idempotency_key, f"copier:{rep.pk}")
        self.assertEqual(ExecutionJob.objects.count(), 0)   # INVARIANT: no job, ever

    @override_settings(**DISPATCH_ON)
    def test_dispatch_is_idempotent(self):
        self._lifecycle("OBSERVED_OPEN")
        self._rep(self._event(MasterEvent.EventType.OPEN), "OPEN")
        self.assertEqual(run_lifecycle_dispatch_pass()["recorded"], 1)
        self.assertEqual(run_lifecycle_dispatch_pass()["recorded"], 0)   # re-run records nothing new
        self.assertEqual(CopierLifecycleDispatch.objects.count(), 1)
        self.assertEqual(ExecutionJob.objects.count(), 0)

    @override_settings(**DISPATCH_ON)
    def test_linked_not_dispatched(self):
        self._lifecycle("LINKED", follower_ticket=555)
        out = run_lifecycle_dispatch_pass()
        self.assertEqual(out["recorded"], 0)
        self.assertEqual(CopierLifecycleDispatch.objects.count(), 0)

    @override_settings(**DISPATCH_ON)
    def test_modify_pending_records_modify(self):
        self._lifecycle("MODIFY_PENDING", pending="MODIFY", follower_ticket=555)
        rep = self._rep(self._event(MasterEvent.EventType.SL_CHANGE), "MODIFY_SLTP")
        run_lifecycle_dispatch_pass()
        d = CopierLifecycleDispatch.objects.get()
        self.assertEqual(d.op, "MODIFY")
        self.assertEqual(d.idempotency_key, f"copier-modify:{rep.pk}")
        self.assertEqual(ExecutionJob.objects.count(), 0)

    @override_settings(**DISPATCH_ON)
    def test_close_pending_full_records_close(self):
        self._lifecycle("CLOSE_PENDING", pending="CLOSE", follower_ticket=555)
        rep = self._rep(self._event(MasterEvent.EventType.FULL_CLOSE), "CLOSE")
        run_lifecycle_dispatch_pass()
        d = CopierLifecycleDispatch.objects.get(op="CLOSE")
        self.assertEqual(d.idempotency_key, f"copier-close:{rep.pk}")
        self.assertEqual(ExecutionJob.objects.count(), 0)

    @override_settings(**DISPATCH_ON)
    def test_close_pending_partial_is_modeled_no_broker_read(self):
        # ADR-0074 Phase-3: the partial branch now MODELS admission (ledger-only, no live read). With no matched
        # OPEN here it fails closed with a REAL ledger reason (not a 'live-read deferred' placeholder) and still
        # records a row marked as requiring the live volume at arm time — and NEVER creates a job.
        self._lifecycle("CLOSE_PENDING", pending="CLOSE", follower_ticket=555)
        self._rep(self._event(MasterEvent.EventType.PARTIAL_CLOSE), "PARTIAL_CLOSE")
        run_lifecycle_dispatch_pass()
        d = CopierLifecycleDispatch.objects.get(op="PARTIAL_CLOSE")
        self.assertFalse(d.admit_ok)
        self.assertFalse(d.would_execute)
        self.assertNotIn("deferred", d.admit_reason)
        self.assertEqual(d.admit_reason, "no_verified_follower_ticket")
        self.assertTrue(d.detail.get("requires_live_volume_at_arm"))
        self.assertEqual(ExecutionJob.objects.count(), 0)
