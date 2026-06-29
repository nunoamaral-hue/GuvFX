"""
Wipe trades for an account and optionally set cutover to now.

Usage:
    python manage.py reset_account_trades --account-id 13 --set-cutover-now
    python manage.py reset_account_trades --account-id 13 --set-cutover-now --delete-jobs
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from trading.models import TradingAccount, Trade


class Command(BaseCommand):
    help = "Delete Trade rows for an account, optionally delete ExecutionJobs, and set cutover to now"

    def add_arguments(self, parser):
        parser.add_argument(
            "--account-id",
            type=int,
            required=True,
            help="Trading account ID",
        )
        parser.add_argument(
            "--set-cutover-now",
            action="store_true",
            help="Set ingest_cutover_time to now() after deletion so old MT5 history won't re-import.",
        )
        parser.add_argument(
            "--delete-jobs",
            action="store_true",
            help="Also delete ExecutionJob rows for this account.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Skip confirmation prompt (for scripting).",
        )

    def handle(self, *args, **options):
        from execution.models import ExecutionJob

        account_id = options["account_id"]
        set_cutover = options.get("set_cutover_now", False)
        delete_jobs = options.get("delete_jobs", False)
        confirm = options.get("confirm", False)

        try:
            account = TradingAccount.objects.get(id=account_id)
        except TradingAccount.DoesNotExist:
            self.stderr.write(f"[ERROR] Account {account_id} not found")
            return

        trade_count = Trade.objects.filter(account=account).count()
        job_count = ExecutionJob.objects.filter(account=account).count() if delete_jobs else 0

        self.stdout.write(
            f"\n  Account:    {account_id} ({account})\n"
            f"  Trades:     {trade_count} to delete\n"
            f"  Jobs:       {job_count} to delete\n"
            f"  Cutover:    {'will set to now()' if set_cutover else 'unchanged'}\n"
        )

        if not confirm:
            self.stdout.write(
                "  Add --confirm to execute. This is DESTRUCTIVE and cannot be undone."
            )
            return

        # Delete trades
        deleted_trades, _ = Trade.objects.filter(account=account).delete()
        self.stdout.write(f"  [OK] Deleted {deleted_trades} trades")

        # Delete jobs (optional)
        if delete_jobs:
            deleted_jobs, _ = ExecutionJob.objects.filter(account=account).delete()
            self.stdout.write(f"  [OK] Deleted {deleted_jobs} execution jobs")

        # Set cutover
        if set_cutover:
            cutover_dt = timezone.now()
            account.ingest_cutover_time = cutover_dt
            account.save(update_fields=["ingest_cutover_time", "updated_at"])
            self.stdout.write(
                f"  [OK] Set ingest_cutover_time = {cutover_dt.isoformat()}"
            )

        self.stdout.write("\n  Reset complete.")
