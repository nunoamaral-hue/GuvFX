from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from trading.models import TradingAccount
from strategies.models import StrategyAssignment
from execution.models import ExecutionJob


class Command(BaseCommand):
    help = "Poll active MT5 instances and enqueue deterministic ExecutionJob tasks for the MT5 worker."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50, help="Max jobs to enqueue per run")

    def handle(self, *args, **opts):
        limit = int(opts["limit"])
        created = 0
        now = timezone.now()

        # Active accounts are the source of truth for which account is running on each instance.
        active_accounts = (
            TradingAccount.objects
            .select_related("mt5_instance", "user")
            .filter(is_active=True, mt5_instance__isnull=False)
            .order_by("mt5_instance_id", "id")
        )

        for acc in active_accounts:
            if created >= limit:
                break

            # Find the single active strategy assignment for this account (if any)
            assignment = (
                StrategyAssignment.objects
                .select_related("strategy", "account")
                .filter(account=acc, is_active=True)
                .first()
            )
            if not assignment:
                continue

            # Deterministic: only enqueue if there isn't already a pending job of same type for this account+assignment
            # MVP: enqueue SYNC_POSITIONS heartbeat job (worker can later translate into EA snapshot pulls)
            already = ExecutionJob.objects.filter(
                account=acc,
                assignment=assignment,
                job_type=ExecutionJob.JobType.SYNC_POSITIONS,
                status=ExecutionJob.Status.PENDING,
            ).exists()
            if already:
                continue

            with transaction.atomic():
                job = ExecutionJob.objects.create(
                    job_type=ExecutionJob.JobType.SYNC_POSITIONS,
                    account=acc,
                    strategy=assignment.strategy,
                    assignment=assignment,
                    payload={
                        "ts": now.isoformat(),
                        "mt5_instance_id": acc.mt5_instance_id,
                        "mode": "PULL_SNAPSHOTS",
                    },
                    status=ExecutionJob.Status.PENDING,
                    created_by=None,
                )
                created += 1
                self.stdout.write(self.style.SUCCESS(f"Enqueued job {job.id} for account={acc.id} assignment={assignment.id}"))

        self.stdout.write(self.style.NOTICE(f"Done. Created {created} job(s)."))
