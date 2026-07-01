"""
EXEC-E2a — operator command: promote a PLANNED plan to suppressed shadow jobs.

Given a PLANNED ``SignalExecutionPlan``, create one ``PLACE_ORDER_SHADOW``
``ExecutionJob`` per leg via the promotion service. The jobs are SUPPRESSED
(``execution_mode=SHADOW``) and un-claimable (distinct job_type + next_job
endpoint guard). Creates NO executable PLACE_ORDER job, places NO order, contacts
NO MT5/agent, uses NO credentials. There is no automatic promotion trigger.

Usage:
    python manage.py promote_plan_to_shadow --plan <id> [--actor <username|email>]
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from execution.models import ExecutionJob, SignalExecutionPlan
from execution.signal_promotion import PromotionRejected, promote_plan_to_shadow_jobs

User = get_user_model()


class Command(BaseCommand):
    help = (
        "EXEC-E2a: promote a PLANNED plan into per-leg PLACE_ORDER_SHADOW jobs "
        "(suppressed, un-claimable; no order placed, no executable job created)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--plan", type=int, required=True, help="SignalExecutionPlan id.")
        parser.add_argument("--actor", help="Existing username/email to attribute to.")

    def _actor(self, identifier):
        if not identifier:
            return None
        return (
            User.objects.filter(username=identifier).first()
            or User.objects.filter(email=identifier).first()
        )

    def handle(self, *args, **opts):
        executable_before = ExecutionJob.objects.exclude(
            job_type=ExecutionJob.JobType.PLACE_ORDER_SHADOW
        ).count()
        try:
            plan = SignalExecutionPlan.objects.get(id=opts["plan"])
        except SignalExecutionPlan.DoesNotExist:
            raise CommandError(f"SignalExecutionPlan #{opts['plan']} not found")

        try:
            jobs = promote_plan_to_shadow_jobs(plan, actor=self._actor(opts.get("actor")))
        except PromotionRejected as exc:
            self.stdout.write(self.style.WARNING(
                f"REJECTED [{exc.code}]: {exc.message} (a PROMOTION_REJECTED audit row was written)"
            ))
            raise CommandError("promotion rejected") from exc

        executable_after = ExecutionJob.objects.exclude(
            job_type=ExecutionJob.JobType.PLACE_ORDER_SHADOW
        ).count()

        self.stdout.write(self.style.SUCCESS(
            f"\nPlan #{plan.id} {plan.status}: {len(jobs)} PLACE_ORDER_SHADOW job(s) (NO ORDER):"
        ))
        for job in jobs:
            p = job.payload
            self.stdout.write(
                f"  + SHADOW job #{job.id} {p.get('side')} {p.get('symbol')} "
                f"lot={p.get('lots')} SL={p.get('sl_price')} TP={p.get('tp_price')} "
                f"mode={p.get('execution_mode')} ({job.job_type})"
            )
        self.stdout.write("=" * 60)
        self.stdout.write(self.style.SUCCESS(
            f"Done — {len(jobs)} shadow job(s); "
            f"{executable_after - executable_before} executable jobs created; 0 orders placed."
        ))
