"""Phase-6 / Phase-8 / Phase-11 completion tests (ADR-0074).

Phase 6  — no-late-recovery EVIDENCE: a later material event on a terminal lifecycle is recorded as a
           non-executable ``post_terminal_*_ignored`` transition (no state change, no entry).
Phase 8  — mutation-killing engine assertions: close-supersedes-modify, close terminality, no-late-recovery.
Phase 11 — the three new flags default OFF, are ANDed with the master gate, and fail closed on a bad value.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from trade_copier import lifecycle as lc
from trade_copier.flags import (
    copier_alerts_enabled, copier_event_ingestion_enabled, copier_lifecycle_dispatch_enabled,
)
from trade_copier.lifecycle import (
    LifecyclePolicy, LifecycleState, MasterEventInput, TicketContext, decide_link_update, decide_master_event,
)
from trade_copier.models import (
    CopierLifecycleTransition, CopierMaster, CopierPositionLifecycle, MasterEvent, MasterObservationPoll,
)
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
POLICY = LifecyclePolicy()


class NoLateRecoveryEngineTests(SimpleTestCase):
    """Pure mutation-killers — these must fail if the no-late-recovery / ordering guarantees regress."""

    def test_open_detected_after_deadline_is_terminal(self):
        now = timezone.now()
        ev = MasterEventInput(sequence=1, category=lc.CAT_OPEN, observed_at=now - timedelta(seconds=10_000))
        d = decide_master_event(None, ev, now=now, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.ENTRY_FAILED_FINAL)

    def test_link_after_deadline_never_links(self):
        now = timezone.now()
        ctx = TicketContext(state=LifecycleState.ENTRY_PENDING, applied_sequence=1,
                            entry_reference_at=now - timedelta(seconds=10_000), attempted=True)
        d = decide_link_update(ctx, lc.LinkUpdate(linked=True, follower_ticket=9), now=now, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.ENTRY_FAILED_FINAL)   # NOT LINKED

    def test_close_supersedes_queued_modify(self):
        now = timezone.now()
        ctx = TicketContext(state=LifecycleState.MODIFY_PENDING, applied_sequence=5,
                            entry_reference_at=now, follower_ticket=9, pending_op=lc.OP_MODIFY)
        d = decide_master_event(ctx, MasterEventInput(sequence=6, category=lc.CAT_FULL_CLOSE, observed_at=now),
                                now=now, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.CLOSE_PENDING)
        self.assertEqual(d.next.pending_op, lc.OP_CLOSE)

    def test_modify_after_close_is_dropped(self):
        now = timezone.now()
        ctx = TicketContext(state=LifecycleState.CLOSE_PENDING, applied_sequence=6,
                            entry_reference_at=now, follower_ticket=9, pending_op=lc.OP_CLOSE)
        d = decide_master_event(ctx, MasterEventInput(sequence=7, category=lc.CAT_MODIFY, observed_at=now),
                                now=now, policy=POLICY)
        self.assertEqual(d.reason, "superseded_by_close")
        self.assertEqual(d.next.state, LifecycleState.CLOSE_PENDING)

    def test_closed_is_terminal_absorbs_later_events(self):
        now = timezone.now()
        ctx = TicketContext(state=LifecycleState.CLOSED, applied_sequence=9, entry_reference_at=now)
        d = decide_master_event(ctx, MasterEventInput(sequence=10, category=lc.CAT_MODIFY, observed_at=now),
                                now=now, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.CLOSED)
        self.assertEqual(d.reason, "terminal_absorbed")


@override_settings(COPIER_ENABLED=True, COPIER_LIFECYCLE_ENABLED=True)
class PostTerminalEvidenceTests(TestCase):
    """Phase 6: a later SL/TP/close change on a FINALLY-failed entry is recorded as non-executable evidence."""

    def setUp(self):
        self.user = User.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.bs = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6Technologies-Demo",
                                              environment="demo")
        self.macct = TradingAccount.objects.create(user=self.user, name="m", broker_name="IS6",
                                                   account_number="1000001", is_demo=True, is_active=True,
                                                   broker_server=self.bs)
        self.master = CopierMaster.objects.create(trading_account=self.macct, observe_enabled=True)
        from trade_copier import services
        facct = TradingAccount.objects.create(user=self.user, name="f", broker_name="IS6",
                                              account_number="2000001", is_demo=True, is_active=True,
                                              broker_server=self.bs)
        services.set_follower_enabled(services.add_follower(self.master, facct), True)
        self.poll = MasterObservationPoll.objects.create(master=self.master, observed_at=timezone.now(),
                                                         read_at=timezone.now(), position_count=1)

    def _event(self, etype, seq, *, age_s=0):
        return MasterEvent.objects.create(
            master=self.master, sequence=seq, from_poll=self.poll, event_type=etype, position_ticket=1,
            symbol="XAUUSD", side="BUY", volume_before=0, volume_after=0, volume_delta=0,
            new_sl=(1990 if "SL" in etype else None),
            observed_at=timezone.now() - timedelta(seconds=age_s))

    def test_post_terminal_event_recorded_as_evidence(self):
        # OPEN observed long ago → ENTRY_FAILED_FINAL; a later SL_CHANGE must be recorded as ignored evidence.
        self._event(MasterEvent.EventType.OPEN, 1, age_s=10_000)
        self._event(MasterEvent.EventType.SL_CHANGE, 2, age_s=0)
        lc.run_lifecycle_pass(policy=POLICY)
        row = CopierPositionLifecycle.objects.get(master=self.master, master_position_ticket=1)
        self.assertEqual(row.state, LifecycleState.ENTRY_FAILED_FINAL.value)
        self.assertTrue(row.is_terminal)
        triggers = list(CopierLifecycleTransition.objects.filter(lifecycle=row).values_list("trigger", flat=True))
        self.assertIn("entry_deadline_exceeded", triggers)
        self.assertTrue(any(t.startswith("post_terminal_") for t in triggers),
                        f"expected a post_terminal_* evidence transition, got {triggers}")

    def test_evidence_is_idempotent_on_rerun(self):
        self._event(MasterEvent.EventType.OPEN, 1, age_s=10_000)
        self._event(MasterEvent.EventType.SL_CHANGE, 2, age_s=0)
        lc.run_lifecycle_pass(policy=POLICY)
        lc.run_lifecycle_pass(policy=POLICY)   # re-run must not duplicate evidence (HWM)
        row = CopierPositionLifecycle.objects.get(master=self.master, master_position_ticket=1)
        post = CopierLifecycleTransition.objects.filter(lifecycle=row, trigger__startswith="post_terminal_").count()
        self.assertEqual(post, 1)


class NewFlagNegativeTests(SimpleTestCase):
    """Phase 11: new flags default OFF, ANDed with the master gate, fail closed on a bad value."""

    @override_settings(COPIER_ENABLED=True)
    def test_default_off(self):
        self.assertFalse(copier_event_ingestion_enabled())
        self.assertFalse(copier_lifecycle_dispatch_enabled())
        self.assertFalse(copier_alerts_enabled())

    @override_settings(COPIER_ENABLED=False, COPIER_EVENT_INGESTION_ENABLED=True,
                       COPIER_LIFECYCLE_DISPATCH_ENABLED=True, COPIER_ALERTS_ENABLED=True)
    def test_master_gate_required(self):
        self.assertFalse(copier_event_ingestion_enabled())
        self.assertFalse(copier_lifecycle_dispatch_enabled())
        self.assertFalse(copier_alerts_enabled())

    @override_settings(COPIER_ENABLED=True, COPIER_ALERTS_ENABLED="maybe",
                       COPIER_EVENT_INGESTION_ENABLED="0", COPIER_LIFECYCLE_DISPATCH_ENABLED="off")
    def test_malformed_value_fails_closed(self):
        self.assertFalse(copier_alerts_enabled())
        self.assertFalse(copier_event_ingestion_enabled())
        self.assertFalse(copier_lifecycle_dispatch_enabled())

    @override_settings(COPIER_ENABLED=True, COPIER_EVENT_INGESTION_ENABLED=True,
                       COPIER_LIFECYCLE_DISPATCH_ENABLED=True, COPIER_ALERTS_ENABLED=True)
    def test_enabled_when_both_on(self):
        self.assertTrue(copier_event_ingestion_enabled())
        self.assertTrue(copier_lifecycle_dispatch_enabled())
        self.assertTrue(copier_alerts_enabled())
