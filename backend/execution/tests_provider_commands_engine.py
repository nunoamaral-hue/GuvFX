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
            user=self.user, name="Demo", account_number="PE1", is_demo=True, broker_name="DemoBroker")
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

    def _cmd(self, provider, command_type, reply_to, *, args=None, chat_id="c1"):
        # chat_id defaults to the _plan default ("c1") so correlation is chat-scoped and
        # same-chat (T1 R2 HIGH-B: a blank-chat command fails closed, never cross-channel).
        self._mid += 1
        return ProviderCommand.objects.create(
            provider=provider, chat_id=chat_id, message_id=f"cmd{self._mid}",
            reply_to_message_id=reply_to, command_type=command_type, args=args or {},
            raw_text="x", status=ProviderCommand.Status.PENDING)


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
        # Genuine ambiguity is now SAME-chat/multi-target: correlation is chat-scoped, so
        # cross-CHAT candidates can no longer be ambiguous (R2 HIGH-B). Two active plans in
        # the SAME chat 'a' share the reply message_id (two accounts) → ambiguous, no action.
        acct2 = TradingAccount.objects.create(
            user=self.user, name="D2", account_number="PE2", is_demo=True, broker_name="DemoBroker")
        p1 = self._plan(chat_id="a")
        appr2 = PendingSignalApproval.objects.create(
            source="ti_signals", chat_id="a", message_id="shared2", symbol="XAUUSD",
            direction="BUY", stop_loss="4000", take_profits=["4010"],
            status=PendingSignalApproval.Status.APPROVED)
        SignalExecutionPlan.objects.create(
            approval=appr2, account=acct2, source="ti_signals", chat_id="a", message_id="shared",
            symbol="XAUUSD", direction="BUY", stop_loss="4000", is_demo=True,
            signal_timestamp=timezone.now(), status=SignalExecutionPlan.Status.PROMOTED)
        SignalExecutionPlan.objects.filter(id=p1.id).update(message_id="shared")
        cmd = self._cmd(self.ti, "CLOSE_ALL", "shared", chat_id="a")
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

    def test_close_all_defers_on_raced_worker_claim(self):
        # All fills resolved at the top check, but a PENDING order is claimed (→RUNNING) DURING
        # processing → the post-enqueue re-check must DEFER so the raced position isn't left open.
        p = self._plan()
        leg2 = p.legs.get(leg_index=2)

        def race(_plan):
            leg2.execution_job.status = "RUNNING"
            leg2.execution_job.save(update_fields=["status"])
            Trade.objects.filter(account=self.acct, comment=f"WAY{p.id}L2").delete()  # not yet ingested
            return []
        self._cmd(self.ti, "CLOSE_ALL", p.message_id)
        with _enabled(), mock.patch.object(engine, "_cancel_pending_order_jobs", side_effect=race):
            res = engine.apply_provider_commands()
        self.assertEqual(res["deferred"], 1)

    def test_close_dedup_is_account_scoped(self):
        # A CLOSE for the SAME ticket on a DIFFERENT account must not suppress this account's close.
        p = self._plan()
        other = TradingAccount.objects.create(
            user=self.user, name="Other", account_number="PE2", is_demo=True, broker_name="DemoBroker")
        t1 = Trade.objects.get(account=self.acct, comment=f"WAY{p.id}L1")
        ExecutionJob.objects.create(job_type="CLOSE_TRADE", account=other, status="PENDING",
                                    payload={"ticket": t1.ticket})  # same ticket, other account
        self._cmd(self.ti, "CLOSE_LEG", p.message_id, args={"leg_index": 1})
        with _enabled():
            engine.apply_provider_commands()
        self.assertTrue(ExecutionJob.objects.filter(
            job_type="CLOSE_TRADE", account=self.acct, payload__ticket=t1.ticket).exists())

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


class R2CommandChatIsolationTests(EngineBase):
    """T1 R2 HIGH-B: command→plan correlation is ALWAYS chat-scoped; a blank/unresolved
    command chat fails closed and can never target another channel's plan."""

    def test_blank_chat_command_one_other_chat_candidate_fails_closed(self):
        p = self._plan(chat_id="A")
        cmd = self._cmd(self.ti, "CLOSE_ALL", p.message_id, chat_id="")  # blank chat
        plan, reason = engine.resolve_target_plan(cmd)
        self.assertIsNone(plan)
        self.assertEqual(reason, "unresolved_chat_identity")

    def test_blank_chat_command_two_chat_candidates_fails_closed(self):
        self._plan(chat_id="A")
        self._plan(chat_id="B")
        SignalExecutionPlan.objects.filter(source="ti_signals").update(message_id="shared")
        cmd = self._cmd(self.ti, "CLOSE_ALL", "shared", chat_id="")
        plan, reason = engine.resolve_target_plan(cmd)
        self.assertIsNone(plan)
        self.assertEqual(reason, "unresolved_chat_identity")

    def test_command_and_plan_same_chat_matches(self):
        p = self._plan(chat_id="A")
        cmd = self._cmd(self.ti, "CLOSE_ALL", p.message_id, chat_id="A")
        plan, reason = engine.resolve_target_plan(cmd)
        self.assertEqual(plan.id, p.id)
        self.assertEqual(reason, "reply")

    def test_command_and_plan_different_chat_no_match(self):
        p = self._plan(chat_id="A")
        cmd = self._cmd(self.ti, "CLOSE_ALL", p.message_id, chat_id="B")
        plan, reason = engine.resolve_target_plan(cmd)
        self.assertIsNone(plan)
        self.assertEqual(reason, "no_reply_match")

    def test_identical_reply_ids_across_two_chats_are_isolated(self):
        self._plan(chat_id="A")
        self._plan(chat_id="B")
        SignalExecutionPlan.objects.filter(source="ti_signals").update(message_id="shared")
        cmd_a = self._cmd(self.ti, "CLOSE_ALL", "shared", chat_id="A")
        plan, reason = engine.resolve_target_plan(cmd_a)
        self.assertEqual(reason, "reply")
        self.assertEqual(plan.chat_id, "A")  # chat A's plan only — never chat B's

    def test_rejected_correlation_enqueues_no_job(self):
        self._plan(chat_id="A")
        self._plan(chat_id="B")
        SignalExecutionPlan.objects.filter(source="ti_signals").update(message_id="shared")
        cmd = self._cmd(self.ti, "CLOSE_ALL", "shared", chat_id="")  # blank → fail closed
        with _enabled():
            res = engine.apply_provider_commands()
        self.assertEqual(res["rejected"], 1)
        cmd.refresh_from_db()
        self.assertEqual(cmd.status, ProviderCommand.Status.REJECTED)
        self.assertFalse(ExecutionJob.objects.filter(
            job_type__in=("CLOSE_TRADE", "MODIFY_POSITION")).exists())

    def test_downstream_flag_enabled_end_to_end_chat_exact(self):
        """With the downstream identity flag ON, plan_demo_execution carries approval.chat_id
        into the plan, so a same-chat command correlates and a cross-chat one does not."""
        import os
        from execution import signal_planning
        SignalSourceConfig.objects.filter(source="ti_signals").update(
            auto_demo_execution_enabled=True, total_lot_target=Decimal("0.03"))
        appr = PendingSignalApproval.objects.create(
            source="ti_signals", chat_id="A", message_id="e2e", symbol="EURUSD",
            direction="BUY", entry="1.0850", stop_loss="1.0800", take_profit="1.0900",
            take_profits=["1.0900", "1.0950", "1.1000"],
            status=PendingSignalApproval.Status.APPROVED)
        with mock.patch.dict(os.environ, {"SIGNAL_INTAKE_CHAT_AWARE_DOWNSTREAM_ENABLED": "1"}):
            plan = signal_planning.plan_demo_execution(appr, account=self.acct)
        self.assertEqual(plan.chat_id, "A")
        self.assertEqual(plan.status, SignalExecutionPlan.Status.PLANNED)
        match, r1 = engine.resolve_target_plan(self._cmd(self.ti, "CLOSE_ALL", "e2e", chat_id="A"))
        self.assertEqual((match.id, r1), (plan.id, "reply"))
        cross, r2 = engine.resolve_target_plan(self._cmd(self.ti, "CLOSE_ALL", "e2e", chat_id="B"))
        self.assertIsNone(cross)
        self.assertEqual(r2, "no_reply_match")
