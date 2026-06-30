"""
EXEC-E1b — operator command: build a non-executable demo execution PLAN.

Given an APPROVED ``signal_intake.PendingSignalApproval`` and a DEMO
``TradingAccount`` (whose source is armed via ``SignalSourceConfig``), create one
``SignalExecutionPlan`` + up to three ``ProposedOrderLeg`` rows via the planner.
Creates NO ``ExecutionJob``, places NO order, contacts NO broker/agent, uses NO
credentials, and activates NO Telegram listener. This is the deliberate, human-
driven entry point for E1b — there is no automatic approval→plan trigger.

Usage:
    python manage.py plan_demo_execution --approval <id> --account <demo_id> \\
        [--total-lot 0.03] [--actor <username|email>]
    python manage.py plan_demo_execution --enable-source WAYOND_TELEGRAM [--total-lot 0.03]
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from execution.models import ExecutionJob, ProposedOrderLeg, SignalExecutionPlan
from execution.signal_planning import (
    PlanRejected,
    plan_demo_execution,
    set_source_enabled,
)
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount

User = get_user_model()


class Command(BaseCommand):
    help = (
        "EXEC-E1b: build a non-executable demo execution plan (no order placed, "
        "no ExecutionJob created) or arm a signal source for auto-demo planning."
    )

    def add_arguments(self, parser):
        parser.add_argument("--approval", type=int, help="PendingSignalApproval id.")
        parser.add_argument("--account", type=int, help="Demo TradingAccount id.")
        parser.add_argument("--total-lot", type=str, default=None, help="Override per-signal total lot.")
        parser.add_argument("--actor", help="Existing username/email to attribute to.")
        parser.add_argument("--enable-source", help="Arm a source for auto-demo planning (then exit).")

    def _actor(self, identifier):
        if not identifier:
            return None
        return (
            User.objects.filter(username=identifier).first()
            or User.objects.filter(email=identifier).first()
        )

    def handle(self, *args, **opts):
        actor = self._actor(opts.get("actor"))

        if opts.get("enable_source"):
            cfg = set_source_enabled(
                opts["enable_source"], True, actor=actor,
                total_lot_target=opts.get("total_lot"),
            )
            self.stdout.write(self.style.SUCCESS(
                f"Source '{cfg.source}' armed (total_lot_target={cfg.total_lot_target}). "
                f"No order placed."
            ))
            return

        if not opts.get("approval") or not opts.get("account"):
            raise CommandError("--approval and --account are required (or use --enable-source)")

        jobs_before = ExecutionJob.objects.count()
        try:
            approval = PendingSignalApproval.objects.get(id=opts["approval"])
        except PendingSignalApproval.DoesNotExist:
            raise CommandError(f"PendingSignalApproval #{opts['approval']} not found")
        try:
            account = TradingAccount.objects.get(id=opts["account"])
        except TradingAccount.DoesNotExist:
            raise CommandError(f"TradingAccount #{opts['account']} not found")

        try:
            plan = plan_demo_execution(
                approval, account=account, actor=actor, total_lot=opts.get("total_lot"),
            )
        except PlanRejected as exc:
            self.stdout.write(self.style.WARNING(
                f"REJECTED [{exc.code}]: {exc.message} (a PLAN_HELD audit row was written)"
            ))
            jobs_after = ExecutionJob.objects.count()
            self.stdout.write(self.style.SUCCESS(
                f"0 orders placed; {jobs_after - jobs_before} ExecutionJobs created."
            ))
            raise CommandError("plan rejected") from exc

        jobs_after = ExecutionJob.objects.count()
        legs = list(plan.legs.order_by("leg_index"))
        self.stdout.write(self.style.SUCCESS(
            f"\nSignalExecutionPlan #{plan.id} {plan.status} (NO ORDER PLACED):"
        ))
        self.stdout.write(
            f"  {plan.direction} {plan.symbol} SL={plan.stop_loss} "
            f"total_lot={plan.total_lot} from approval #{approval.id} onto account #{account.id}"
        )
        for leg in legs:
            self.stdout.write(
                f"    leg {leg.leg_index}: TP={leg.take_profit} lot={leg.lot_size} ({leg.order_type})"
            )
        if not legs:
            self.stdout.write(f"  (no legs — {plan.status} {plan.hold_reason})")
        self.stdout.write("=" * 60)
        self.stdout.write(self.style.SUCCESS(
            f"Done — 1 plan, {len(legs)} leg(s); {jobs_after - jobs_before} "
            f"ExecutionJobs created; 0 orders placed."
        ))
