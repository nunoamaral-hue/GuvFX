"""Per-master-ticket lifecycle certification (ADR-0071 hybrid change propagation).

Two layers:

* ``LifecycleEngineTests`` — the PURE state machine (no DB). Every Phase-3 scenario in the packet is an
  explicit assertion here: open with/without SL/TP, add/modify/remove SL·TP, duplicate/stale/out-of-order
  modifies, close racing a modify, partial + repeated-partial + full close, a failed initial entry with NO
  late recovery, restart/replay idempotency, follower modify/close rejection, divergence containment,
  non-contiguous (event-loss) sequences, multi-ticket isolation, and determinism/replay of an interleaved
  stream.
* ``LifecycleProjectionTests`` — the DARK, read-only DB projection: the flag gate, OBSERVED_OPEN creation,
  idempotent re-runs (no duplicate entry), closed-before-entry → ENTRY_FAILED_FINAL, and multi-tenant
  isolation. The projection creates NO ExecutionJob and contacts no broker.
"""
from datetime import datetime, timedelta, timezone as _tz
from decimal import Decimal

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from trade_copier import lifecycle
from trade_copier.lifecycle import (
    CAT_FULL_CLOSE, CAT_MODIFY, CAT_OPEN, CAT_PARTIAL_CLOSE, CAT_SCALE_IN,
    LifecyclePolicy, LifecycleState, LinkUpdate, MasterEventInput,
    OP_CLOSE, OP_MODIFY, OP_NONE, apply_all, decide_link_update, decide_master_event,
)
from trade_copier.models import (
    CopierPositionLifecycle, MasterEvent, MasterObservationPoll,
)
from trade_copier.tests_execution_producer import ProducerFixture

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=_tz.utc)
POLICY = LifecyclePolicy(entry_admissibility_seconds=120.0, max_modification_age_seconds=120.0,
                         max_event_age_seconds=300.0)
LIFECYCLE_ON = dict(COPIER_ENABLED=True, COPIER_LIFECYCLE_ENABLED=True)


def _ev(seq, cat, *, at=NOW, broker=None, weakens=False, prot=False):
    return MasterEventInput(sequence=seq, category=cat, observed_at=at, broker_event_time=broker,
                            modify_weakens=weakens, has_protection=prot)


class LifecycleEngineTests(SimpleTestCase):
    """The pure state machine — no database."""

    # ── helpers ──────────────────────────────────────────────────────────────
    def _observed_open(self, *, at=NOW, prot=True, seq=1):
        d = decide_master_event(None, _ev(seq, CAT_OPEN, at=at, prot=prot), now=at, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.OBSERVED_OPEN)
        return d.next

    def _linked(self, *, seq=1):
        ctx = self._observed_open(seq=seq)
        ctx = decide_link_update(ctx, LinkUpdate(attempted=True), now=NOW, policy=POLICY).next
        self.assertEqual(ctx.state, LifecycleState.ENTRY_PENDING)
        ctx = decide_link_update(ctx, LinkUpdate(linked=True, follower_ticket=777), now=NOW, policy=POLICY).next
        self.assertEqual(ctx.state, LifecycleState.LINKED)
        self.assertEqual(ctx.follower_ticket, 777)
        return ctx

    # ── OPEN admission ───────────────────────────────────────────────────────
    def test_open_with_sltp_is_observed(self):
        self.assertEqual(self._observed_open(prot=True).state, LifecycleState.OBSERVED_OPEN)

    def test_open_without_sltp_is_observed(self):
        """OPEN_WITHOUT_SLTP_SUPPORTED: a no-SL/TP master OPEN is admitted (not refused)."""
        ctx = self._observed_open(prot=False)
        self.assertEqual(ctx.state, LifecycleState.OBSERVED_OPEN)

    def test_open_detected_late_is_entry_failed_final(self):
        """A delayed entry is never placed: an OPEN detected past the admissibility deadline is terminal."""
        d = decide_master_event(None, _ev(1, CAT_OPEN, at=NOW - timedelta(seconds=200)), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.ENTRY_FAILED_FINAL)
        self.assertEqual(d.reason, "entry_deadline_exceeded")

    # ── entry attempt / link ───────────────────────────────────────────────────
    def test_attempt_then_link(self):
        self.assertEqual(self._linked().state, LifecycleState.LINKED)

    def test_attempt_after_deadline_fails_final(self):
        ctx = self._observed_open()
        d = decide_link_update(ctx, LinkUpdate(attempted=True), now=NOW + timedelta(seconds=200), policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.ENTRY_FAILED_FINAL)
        self.assertEqual(d.reason, "attempt_after_deadline")

    def test_link_after_deadline_fails_final(self):
        ctx = self._observed_open()
        ctx = decide_link_update(ctx, LinkUpdate(attempted=True), now=NOW, policy=POLICY).next
        d = decide_link_update(ctx, LinkUpdate(linked=True, follower_ticket=9),
                               now=NOW + timedelta(seconds=200), policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.ENTRY_FAILED_FINAL)
        self.assertEqual(d.reason, "linked_after_deadline")

    def test_entry_attempt_failed_is_final(self):
        ctx = self._observed_open()
        ctx = decide_link_update(ctx, LinkUpdate(attempted=True), now=NOW, policy=POLICY).next
        d = decide_link_update(ctx, LinkUpdate(failed=True), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.ENTRY_FAILED_FINAL)
        self.assertEqual(d.reason, "entry_attempt_failed")

    def test_no_recovery_after_failed_entry(self):
        """NO_LATE_RECOVERY_TRADES: once ENTRY_FAILED_FINAL, later snapshots/events never resurrect it."""
        ctx = self._observed_open()
        ctx = decide_link_update(ctx, LinkUpdate(attempted=True), now=NOW, policy=POLICY).next
        ctx = decide_link_update(ctx, LinkUpdate(failed=True), now=NOW, policy=POLICY).next
        self.assertEqual(ctx.state, LifecycleState.ENTRY_FAILED_FINAL)
        # Later OPEN/MODIFY/CLOSE for the same ticket are absorbed — no re-entry.
        for cat in (CAT_OPEN, CAT_MODIFY, CAT_FULL_CLOSE, CAT_PARTIAL_CLOSE):
            d = decide_master_event(ctx, _ev(ctx.applied_sequence + 1, cat), now=NOW, policy=POLICY)
            self.assertEqual(d.next.state, LifecycleState.ENTRY_FAILED_FINAL)
            self.assertEqual(d.reason, "terminal_absorbed")
            ctx = d.next
        # And a replayed link update cannot move it either.
        d = decide_link_update(ctx, LinkUpdate(linked=True, follower_ticket=1), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.ENTRY_FAILED_FINAL)

    # ── modify ─────────────────────────────────────────────────────────────────
    def test_add_sl_after_entry(self):
        d = decide_master_event(self._linked(), _ev(2, CAT_MODIFY), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.MODIFY_PENDING)
        self.assertEqual(d.next.pending_op, OP_MODIFY)

    def test_add_tp_after_entry(self):
        d = decide_master_event(self._linked(), _ev(2, CAT_MODIFY), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.MODIFY_PENDING)

    def test_modify_both_repeatedly(self):
        ctx = self._linked()
        ctx = decide_master_event(ctx, _ev(2, CAT_MODIFY), now=NOW, policy=POLICY).next           # SL set
        self.assertEqual(ctx.state, LifecycleState.MODIFY_PENDING)
        ctx = decide_link_update(ctx, LinkUpdate(modify_confirmed=True), now=NOW, policy=POLICY).next
        self.assertEqual(ctx.state, LifecycleState.LINKED)
        ctx = decide_master_event(ctx, _ev(3, CAT_MODIFY), now=NOW, policy=POLICY).next            # TP set
        self.assertEqual(ctx.state, LifecycleState.MODIFY_PENDING)
        ctx = decide_link_update(ctx, LinkUpdate(modify_confirmed=True), now=NOW, policy=POLICY).next
        self.assertEqual(ctx.state, LifecycleState.LINKED)

    def test_sl_remove_is_deferred_not_applied(self):
        d = decide_master_event(self._linked(), _ev(2, CAT_MODIFY, weakens=True), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.LINKED)             # stop never weakened silently
        self.assertFalse(d.applied)
        self.assertEqual(d.reason, "modify_weakens_stop_deferred")

    def test_tp_remove_is_applied(self):
        d = decide_master_event(self._linked(), _ev(2, CAT_MODIFY, weakens=False), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.MODIFY_PENDING)

    def test_duplicate_modification_is_idempotent(self):
        ctx = self._linked()
        ctx = decide_master_event(ctx, _ev(2, CAT_MODIFY), now=NOW, policy=POLICY).next
        d = decide_master_event(ctx, _ev(2, CAT_MODIFY), now=NOW, policy=POLICY)   # same sequence replayed
        self.assertEqual(d.rejected, "stale_sequence")
        self.assertEqual(d.next.state, LifecycleState.MODIFY_PENDING)

    def test_stale_out_of_order_modification_rejected(self):
        ctx = self._linked()
        ctx = decide_master_event(ctx, _ev(10, CAT_MODIFY), now=NOW, policy=POLICY).next
        d = decide_master_event(ctx, _ev(8, CAT_MODIFY), now=NOW, policy=POLICY)   # older seq arrives late
        self.assertEqual(d.rejected, "stale_sequence")
        self.assertEqual(d.next.applied_sequence, 10)

    def test_modify_too_old_is_dropped(self):
        d = decide_master_event(self._linked(), _ev(2, CAT_MODIFY, at=NOW - timedelta(seconds=200)),
                                now=NOW, policy=POLICY)
        self.assertEqual(d.reason, "modify_too_old")
        self.assertEqual(d.next.state, LifecycleState.LINKED)

    def test_modify_before_link_dropped(self):
        d = decide_master_event(self._observed_open(), _ev(2, CAT_MODIFY), now=NOW, policy=POLICY)
        self.assertEqual(d.reason, "modify_before_link")
        self.assertEqual(d.next.state, LifecycleState.OBSERVED_OPEN)

    # ── close + ordering contract ───────────────────────────────────────────────
    def test_close_supersedes_queued_modify(self):
        """CLOSE has priority over SL/TP modifications."""
        ctx = self._linked()
        ctx = decide_master_event(ctx, _ev(2, CAT_MODIFY), now=NOW, policy=POLICY).next
        self.assertEqual(ctx.state, LifecycleState.MODIFY_PENDING)
        ctx = decide_master_event(ctx, _ev(3, CAT_FULL_CLOSE), now=NOW, policy=POLICY).next
        self.assertEqual(ctx.state, LifecycleState.CLOSE_PENDING)
        self.assertEqual(ctx.pending_op, OP_CLOSE)
        # A modify arriving AFTER the close is dropped (close still wins).
        d = decide_master_event(ctx, _ev(4, CAT_MODIFY), now=NOW, policy=POLICY)
        self.assertEqual(d.reason, "superseded_by_close")
        self.assertEqual(d.next.state, LifecycleState.CLOSE_PENDING)

    def test_partial_close(self):
        d = decide_master_event(self._linked(), _ev(2, CAT_PARTIAL_CLOSE), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.CLOSE_PENDING)
        self.assertEqual(d.next.pending_op, OP_CLOSE)

    def test_repeated_partial_close_event(self):
        ctx = self._linked()
        ctx = decide_master_event(ctx, _ev(2, CAT_PARTIAL_CLOSE), now=NOW, policy=POLICY).next
        d = decide_master_event(ctx, _ev(3, CAT_PARTIAL_CLOSE), now=NOW, policy=POLICY)
        self.assertEqual(d.reason, "close_already_pending")
        self.assertEqual(d.next.state, LifecycleState.CLOSE_PENDING)

    def test_partial_reconciled_returns_to_linked(self):
        ctx = self._linked()
        ctx = decide_master_event(ctx, _ev(2, CAT_PARTIAL_CLOSE), now=NOW, policy=POLICY).next
        d = decide_link_update(ctx, LinkUpdate(partial_reconciled=True), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.LINKED)
        self.assertEqual(d.next.pending_op, OP_NONE)

    def test_full_early_close_to_closed(self):
        ctx = self._linked()
        ctx = decide_master_event(ctx, _ev(2, CAT_FULL_CLOSE), now=NOW, policy=POLICY).next
        self.assertEqual(ctx.state, LifecycleState.CLOSE_PENDING)
        d = decide_link_update(ctx, LinkUpdate(close_confirmed=True), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.CLOSED)

    def test_closed_before_entry(self):
        d = decide_master_event(self._observed_open(), _ev(2, CAT_FULL_CLOSE), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.ENTRY_FAILED_FINAL)
        self.assertEqual(d.reason, "closed_before_entry")

    def test_closed_before_link_is_contained(self):
        ctx = self._observed_open()
        ctx = decide_link_update(ctx, LinkUpdate(attempted=True), now=NOW, policy=POLICY).next
        d = decide_master_event(ctx, _ev(2, CAT_FULL_CLOSE), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.CONTAINED_FAILURE)
        self.assertEqual(d.reason, "closed_before_link_contained")

    # ── follower-side failures + divergence ─────────────────────────────────────
    def test_follower_modify_rejection_stays_linked(self):
        ctx = self._linked()
        ctx = decide_master_event(ctx, _ev(2, CAT_MODIFY), now=NOW, policy=POLICY).next
        d = decide_link_update(ctx, LinkUpdate(modify_failed=True), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.LINKED)
        self.assertEqual(d.reason, "modify_failed")

    def test_follower_close_rejection_is_contained(self):
        ctx = self._linked()
        ctx = decide_master_event(ctx, _ev(2, CAT_FULL_CLOSE), now=NOW, policy=POLICY).next
        d = decide_link_update(ctx, LinkUpdate(close_failed=True), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.CONTAINED_FAILURE)
        self.assertEqual(d.reason, "close_failed")

    def test_divergent_link_contained(self):
        d = decide_link_update(self._linked(), LinkUpdate(divergent=True), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.CONTAINED_FAILURE)
        self.assertEqual(d.reason, "link_divergent")

    def test_ticket_lost_while_closing_is_closed(self):
        ctx = self._linked()
        ctx = decide_master_event(ctx, _ev(2, CAT_FULL_CLOSE), now=NOW, policy=POLICY).next
        d = decide_link_update(ctx, LinkUpdate(ticket_lost=True), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.CLOSED)              # expected full-close confirmation

    def test_ticket_lost_unexpected_is_contained(self):
        d = decide_link_update(self._linked(), LinkUpdate(ticket_lost=True), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.CONTAINED_FAILURE)
        self.assertEqual(d.reason, "link_lost")

    # ── scale-in / event loss / determinism ─────────────────────────────────────
    def test_scale_in_deferred(self):
        d = decide_master_event(self._linked(), _ev(2, CAT_SCALE_IN), now=NOW, policy=POLICY)
        self.assertEqual(d.reason, "scale_in_deferred")
        self.assertEqual(d.next.state, LifecycleState.LINKED)
        self.assertEqual(d.next.applied_sequence, 2)                       # consumed (HWM advanced)

    def test_non_contiguous_sequences_apply_and_reject_backward(self):
        """Event loss (a missing sequence) does not break monotonicity; a backward seq is still rejected."""
        ctx = self._linked()
        ctx = decide_master_event(ctx, _ev(5, CAT_MODIFY), now=NOW, policy=POLICY).next   # seq 2-4 lost
        self.assertEqual(ctx.applied_sequence, 5)
        d = decide_master_event(ctx, _ev(3, CAT_FULL_CLOSE), now=NOW, policy=POLICY)       # late older event
        self.assertEqual(d.rejected, "stale_sequence")

    def test_terminal_absorbs_everything(self):
        ctx = self._linked()
        ctx = decide_master_event(ctx, _ev(2, CAT_FULL_CLOSE), now=NOW, policy=POLICY).next
        ctx = decide_link_update(ctx, LinkUpdate(close_confirmed=True), now=NOW, policy=POLICY).next
        self.assertEqual(ctx.state, LifecycleState.CLOSED)
        d = decide_master_event(ctx, _ev(99, CAT_OPEN), now=NOW, policy=POLICY)
        self.assertEqual(d.next.state, LifecycleState.CLOSED)
        self.assertEqual(d.reason, "terminal_absorbed")

    def test_apply_all_is_deterministic(self):
        """Replaying the same ordered trigger stream yields the same final state (replay safety)."""
        triggers = [
            (NOW, "event", _ev(1, CAT_OPEN)),
            (NOW, "link", LinkUpdate(attempted=True)),
            (NOW, "link", LinkUpdate(linked=True, follower_ticket=321)),
            (NOW, "event", _ev(2, CAT_MODIFY)),
            (NOW, "event", _ev(3, CAT_FULL_CLOSE)),
            (NOW, "link", LinkUpdate(close_confirmed=True)),
        ]
        a, _ = apply_all(None, triggers, policy=POLICY)
        b, _ = apply_all(None, triggers, policy=POLICY)
        self.assertEqual(a, b)
        self.assertEqual(a.state, LifecycleState.CLOSED)

    def test_orphan_change_without_open_has_no_lifecycle(self):
        d = decide_master_event(None, _ev(1, CAT_MODIFY), now=NOW, policy=POLICY)
        self.assertEqual(d.rejected, "no_lifecycle")
        self.assertIsNone(d.next)

    def test_policy_rejects_non_positive_window(self):
        with self.assertRaises(ValueError):
            LifecyclePolicy(entry_admissibility_seconds=0)


class LifecycleProjectionTests(ProducerFixture):
    """The DARK, read-only DB projection. ProducerFixture gives a master + an OPEN event (seq1, ticket1)."""

    def _close_event(self, *, seq=2, ticket=1):
        return MasterEvent.objects.create(
            master=self.master, sequence=seq, from_poll=self.poll,
            event_type=MasterEvent.EventType.FULL_CLOSE, position_ticket=ticket, symbol="XAUUSD",
            side="BUY", volume_before=Decimal("0.10"), volume_after=Decimal("0"),
            volume_delta=Decimal("-0.10"), observed_at=timezone.now())

    def test_pass_gated_off_is_inert(self):
        self._follower()
        out = lifecycle.run_lifecycle_pass(now=timezone.now())
        self.assertFalse(out["enabled"])
        self.assertEqual(CopierPositionLifecycle.objects.count(), 0)

    @override_settings(**LIFECYCLE_ON)
    def test_projection_creates_observed_open(self):
        f = self._follower()
        out = lifecycle.run_lifecycle_pass(now=timezone.now())
        self.assertTrue(out["enabled"])
        row = CopierPositionLifecycle.objects.get(follower=f, master=self.master, master_position_ticket=1)
        self.assertEqual(row.state, LifecycleState.OBSERVED_OPEN.value)
        self.assertEqual(row.applied_sequence, 1)
        self.assertIsNotNone(row.entry_deadline_at)
        self.assertEqual(row.open_event_id, self.event.id)
        # No ExecutionJob is ever created by the projection.
        from execution.models import ExecutionJob
        self.assertEqual(ExecutionJob.objects.count(), 0)
        self.assertTrue(row.transitions.filter(to_state=LifecycleState.OBSERVED_OPEN.value).exists())

    @override_settings(**LIFECYCLE_ON)
    def test_projection_is_idempotent(self):
        """RESTART/REPLAY: re-running projects nothing new — no duplicate entry, no duplicate transition."""
        f = self._follower()
        lifecycle.run_lifecycle_pass(now=timezone.now())
        row = CopierPositionLifecycle.objects.get(follower=f, master_position_ticket=1)
        t1 = row.transitions.count()
        lifecycle.run_lifecycle_pass(now=timezone.now())
        self.assertEqual(CopierPositionLifecycle.objects.count(), 1)
        row.refresh_from_db()
        self.assertEqual(row.transitions.count(), t1)                      # no new transitions on replay
        self.assertEqual(row.applied_sequence, 1)

    @override_settings(**LIFECYCLE_ON)
    def test_projection_closed_before_entry_is_final(self):
        f = self._follower()
        self._close_event()                                                # master OPEN(1) then FULL_CLOSE(2)
        lifecycle.run_lifecycle_pass(now=timezone.now())
        row = CopierPositionLifecycle.objects.get(follower=f, master_position_ticket=1)
        self.assertEqual(row.state, LifecycleState.ENTRY_FAILED_FINAL.value)
        self.assertTrue(row.is_terminal)
        self.assertEqual(row.failure_reason, "closed_before_entry")

    @override_settings(**LIFECYCLE_ON)
    def test_multi_tenant_isolation_same_ticket_number(self):
        """Two followers under two different owners/masters with the SAME broker ticket number never collide."""
        from django.contrib.auth import get_user_model
        from trade_copier import services
        from trade_copier.models import CopierMaster
        from trading.models import BrokerServer, TradingAccount
        f1 = self._follower()
        # A second, fully-independent tenant with its own master that ALSO has an OPEN on ticket #1.
        User = get_user_model()
        u2 = User.objects.create_user(username="owner2", email="o2@x.invalid", password="x")
        bs2 = BrokerServer.objects.create(broker_display_name="IS6", server_name="IS6Technologies-Demo2",
                                          environment="demo")
        m2acct = TradingAccount.objects.create(user=u2, name="m2", broker_name="IS6", account_number="3000001",
                                               is_demo=True, is_active=True, broker_server=bs2)
        master2 = CopierMaster.objects.create(trading_account=m2acct, observe_enabled=True)
        poll2 = MasterObservationPoll.objects.create(master=master2, observed_at=timezone.now(),
                                                     read_at=timezone.now(), position_count=1)
        MasterEvent.objects.create(master=master2, sequence=1, from_poll=poll2,
                                   event_type=MasterEvent.EventType.OPEN, position_ticket=1, symbol="XAUUSD",
                                   side="BUY", volume_before=Decimal("0"), volume_after=Decimal("0.10"),
                                   volume_delta=Decimal("0.10"), observed_at=timezone.now())
        f2acct = TradingAccount.objects.create(user=u2, name="f2", broker_name="IS6", account_number="3000002",
                                               is_demo=True, is_active=True, broker_server=bs2)
        f2 = services.add_follower(master2, f2acct, multiplier=Decimal("0.5"))
        services.set_follower_enabled(f2, True)

        lifecycle.run_lifecycle_pass(now=timezone.now())
        self.assertEqual(CopierPositionLifecycle.objects.filter(master_position_ticket=1).count(), 2)
        self.assertTrue(CopierPositionLifecycle.objects.filter(follower=f1, master=self.master).exists())
        self.assertTrue(CopierPositionLifecycle.objects.filter(follower=f2, master=master2).exists())
