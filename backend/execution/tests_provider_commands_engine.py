"""WS-E — provider command ENGINE tests: gating, reply correlation, source isolation (TI never
touches Wayond), the executors (move-SL/close/cancel), and idempotency."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution import provider_commands_engine as engine
from execution.models import ExecutionJob, ProposedOrderLeg, SignalExecutionPlan, SignalSourceConfig
from signal_intake.models import (
    ParserProfile, PendingSignalApproval, ProviderCommand, SignalProvider,
)
from trading.models import Trade, TradingAccount

User = get_user_model()


def _enabled():
    return mock.patch.object(engine, "provider_commands_enabled", return_value=True)


class EngineBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pe", email="pe@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="PE1", is_demo=True)
        pp = ParserProfile.objects.create(slug="ti_signals_v1")
        self.ti = SignalProvider.objects.create(slug="ti_signals", parser_profile=pp)
        ppw = ParserProfile.objects.create(slug="wayond_v1")
        self.way = SignalProvider.objects.create(slug="wayond", parser_profile=ppw)
        SignalSourceConfig.objects.create(source="ti_signals", command_engine_enabled=True)
        SignalSourceConfig.objects.create(source="wayond", command_engine_enabled=False)
        self._uname = mock.patch.object(engine, "_windows_username", return_value="mt5user")
        self._uname.start()
        self.addCleanup(self._uname.stop)
        self._mid = 0

    def _plan(self, *, source="ti_signals", direction="BUY", entry="4005", sl="4000",
              status=SignalExecutionPlan.Status.PROMOTED, chat_id="c1",
              tps=("4010", "4020", "4030"), open_flags=(True, True, True)):
        self._mid += 1
        mid = f"m{self._mid}"
        appr = PendingSignalApproval.objects.create(
            source=source, message_id=mid, symbol="XAUUSD", direction=direction, stop_loss=sl,
            take_profits=list(tps), status=PendingSignalApproval.Status.APPROVED)
        plan = SignalExecutionPlan.objects.create(
            approval=appr, account=self.acct, source=source, message_id=mid, chat_id=chat_id,
            symbol="XAUUSD", direction=direction, stop_loss=sl, is_demo=True,
            signal_timestamp=timezone.now(), status=status)
        for i, (tp, is_open) in enumerate(zip(tps, open_flags), start=1):
            job = ExecutionJob.objects.create(job_type="PLACE_ORDER", account=self.acct,
                                              status="SUCCESS", payload={})
            leg = ProposedOrderLeg.objects.create(
                plan=plan, leg_index=i, take_profit=tp, stop_loss=sl,
                lot_size=Decimal("0.40"), status=ProposedOrderLeg.Status.PROMOTED, execution_job=job)
            Trade.objects.create(
                account=self.acct, symbol="XAUUSD", side=direction, volume=Decimal("0.40"),
                ticket=f"pos{plan.id}{i}", open_time=timezone.now(), open_price=Decimal(entry),
                close_time=(None if is_open else timezone.now()), comment=f"WAY{plan.id}L{i}")
        return plan

    def _cmd(self, provider, command_type, reply_to, *, args=None):
        self._mid += 1
        return ProviderCommand.objects.create(
            provider=provider, message_id=f"cmd{self._mid}", reply_to_message_id=reply_to,
            command_type=command_type, args=args or {}, raw_text="x",
            status=ProviderCommand.Status.PENDING)


class GatingTests(EngineBase):
    def test_disabled_is_noop(self):
        p = self._plan()
        self._cmd(self.ti, "MOVE_SL_BE", p.message_id)
        self.assertEqual(engine.apply_provider_commands(), {"enabled": False})
        self.assertFalse(ExecutionJob.objects.filter(job_type="MODIFY_POSITION").exists())

    def test_source_not_opted_in_left_pending(self):
        # wayond has command_engine_enabled=False → its command is never processed.
        p = self._plan(source="wayond")
        cmd = self._cmd(self.way, "CLOSE_ALL", p.message_id)
        with _enabled():
            res = engine.apply_provider_commands()
        self.assertEqual(res["skipped_source"], 1)
        cmd.refresh_from_db()
        self.assertEqual(cmd.status, ProviderCommand.Status.PENDING)  # untouched
        self.assertFalse(ExecutionJob.objects.filter(job_type="CLOSE_TRADE").exists())


class CorrelationTests(EngineBase):
    def test_reply_correlation_applies_move_sl_be(self):
        p = self._plan()  # BUY entry 4005 > sl 4000 → BE move is risk-reducing
        cmd = self._cmd(self.ti, "MOVE_SL_BE", p.message_id)
        with _enabled():
            res = engine.apply_provider_commands()
        self.assertEqual(res["applied"], 1)
        cmd.refresh_from_db()
        self.assertEqual(cmd.status, ProviderCommand.Status.APPLIED)
        jobs = ExecutionJob.objects.filter(job_type="MODIFY_POSITION")
        self.assertEqual(jobs.count(), 3)          # 3 open legs
        self.assertEqual(jobs.first().payload["sl"], 4005.0)

    def test_ambiguous_reply_fails_closed(self):
        # two active plans share the reply message_id (different chat) → ambiguous, apply nothing
        self._plan(chat_id="a")
        p2 = self._plan(chat_id="b")
        # force both to the same message_id the command replies to
        SignalExecutionPlan.objects.filter(source="ti_signals").update(message_id="shared")
        cmd = self._cmd(self.ti, "CLOSE_ALL", "shared")
        with _enabled():
            res = engine.apply_provider_commands()
        self.assertEqual(res["ambiguous"], 1)
        cmd.refresh_from_db()
        self.assertEqual(cmd.status, ProviderCommand.Status.AMBIGUOUS)
        self.assertFalse(ExecutionJob.objects.filter(job_type="CLOSE_TRADE").exists())

    def test_no_reply_match_rejected(self):
        cmd = self._cmd(self.ti, "MOVE_SL_BE", "does-not-exist")
        with _enabled():
            res = engine.apply_provider_commands()
        self.assertEqual(res["rejected"], 1)
        cmd.refresh_from_db()
        self.assertEqual(cmd.status, ProviderCommand.Status.REJECTED)

    def test_source_isolation_ti_cannot_touch_wayond_plan(self):
        # A wayond plan; a TI command replying to its message_id must NOT resolve to it.
        wp = self._plan(source="wayond")
        cmd = self._cmd(self.ti, "CLOSE_ALL", wp.message_id)
        with _enabled():
            res = engine.apply_provider_commands()
        self.assertEqual(res["rejected"], 1)   # no ti_signals plan with that message_id
        self.assertFalse(ExecutionJob.objects.filter(job_type="CLOSE_TRADE").exists())


class ExecutorTests(EngineBase):
    def test_move_sl_price_widen_is_skipped(self):
        # BUY sl=4000; a "move SL to 3990" would LOWER the stop → widen → skip, enqueue nothing.
        p = self._plan()
        self._cmd(self.ti, "MOVE_SL_PRICE", p.message_id, args={"price": "3990"})
        with _enabled():
            engine.apply_provider_commands()
        self.assertFalse(ExecutionJob.objects.filter(job_type="MODIFY_POSITION").exists())

    def test_move_sl_price_tighten_enqueues(self):
        p = self._plan()  # sl 4000; move to 4003 (still below entry 4005) → risk-reducing
        self._cmd(self.ti, "MOVE_SL_PRICE", p.message_id, args={"price": "4003"})
        with _enabled():
            engine.apply_provider_commands()
        jobs = ExecutionJob.objects.filter(job_type="MODIFY_POSITION")
        self.assertEqual(jobs.count(), 3)
        self.assertEqual(jobs.first().payload["sl"], 4003.0)

    def test_close_all_closes_open_legs(self):
        p = self._plan(open_flags=(True, True, False))  # leg3 already closed
        self._cmd(self.ti, "CLOSE_ALL", p.message_id)
        with _enabled():
            engine.apply_provider_commands()
        self.assertEqual(ExecutionJob.objects.filter(job_type="CLOSE_TRADE").count(), 2)

    def test_close_leg_targets_one(self):
        p = self._plan()
        self._cmd(self.ti, "CLOSE_LEG", p.message_id, args={"leg_index": 2})
        with _enabled():
            engine.apply_provider_commands()
        jobs = ExecutionJob.objects.filter(job_type="CLOSE_TRADE")
        self.assertEqual(jobs.count(), 1)
        self.assertEqual(jobs.first().payload["leg_index"], 2)

    def test_cancel_planned_voids_plan(self):
        p = self._plan(status=SignalExecutionPlan.Status.PLANNED)
        self._cmd(self.ti, "CANCEL", p.message_id)
        with _enabled():
            engine.apply_provider_commands()
        p.refresh_from_db()
        self.assertEqual(p.status, SignalExecutionPlan.Status.VOIDED)

    def test_idempotent_processed_not_reapplied(self):
        p = self._plan()
        self._cmd(self.ti, "MOVE_SL_BE", p.message_id)
        with _enabled():
            engine.apply_provider_commands()
            n1 = ExecutionJob.objects.filter(job_type="MODIFY_POSITION").count()
            engine.apply_provider_commands()   # second pass: command is processed → no new jobs
            n2 = ExecutionJob.objects.filter(job_type="MODIFY_POSITION").count()
        self.assertEqual(n1, n2)

    def test_close_all_defers_on_unresolved_fill(self):
        # A leg whose order is placed but the fill isn't yet ingested → DEFER (enqueue nothing,
        # leave the command re-processable) so the in-flight position is not silently missed.
        p = self._plan()
        leg2 = p.legs.get(leg_index=2)
        leg2.execution_job.status = "RUNNING"
        leg2.execution_job.save(update_fields=["status"])
        Trade.objects.filter(account=self.acct, comment=f"WAY{p.id}L2").delete()  # not yet ingested
        cmd = self._cmd(self.ti, "CLOSE_ALL", p.message_id)
        with _enabled():
            res = engine.apply_provider_commands()
        self.assertEqual(res["deferred"], 1)
        cmd.refresh_from_db()
        self.assertEqual(cmd.status, ProviderCommand.Status.PENDING)   # re-processable
        self.assertFalse(ExecutionJob.objects.filter(job_type="CLOSE_TRADE").exists())

    def test_stale_command_expired_not_acted(self):
        p = self._plan()
        cmd = self._cmd(self.ti, "CLOSE_ALL", p.message_id)
        old = timezone.now() - timedelta(seconds=engine.PROVIDER_COMMAND_MAX_AGE_SECONDS + 60)
        ProviderCommand.objects.filter(id=cmd.id).update(created_at=old)
        with _enabled():
            res = engine.apply_provider_commands()
        self.assertEqual(res["expired"], 1)
        cmd.refresh_from_db()
        self.assertEqual(cmd.status, ProviderCommand.Status.SKIPPED)
        self.assertFalse(ExecutionJob.objects.filter(job_type="CLOSE_TRADE").exists())
