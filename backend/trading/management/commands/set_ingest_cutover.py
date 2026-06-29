"""
Set the ingest cutover timestamp for a trading account.

After setting, trade ingest will skip any MT5 deals with deal.time < cutover.
Use this after wiping trades so old history doesn't re-import.

Usage:
    python manage.py set_ingest_cutover --account-id 13
    python manage.py set_ingest_cutover --account-id 13 --cutover-iso "2026-02-19T12:00:00Z"
    python manage.py set_ingest_cutover --account-id 13 --clear
"""

import datetime as dt
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from trading.models import TradingAccount


class Command(BaseCommand):
    help = "Set or clear the ingest cutover timestamp for a trading account"

    def add_arguments(self, parser):
        parser.add_argument(
            "--account-id",
            type=int,
            required=True,
            help="Trading account ID",
        )
        parser.add_argument(
            "--cutover-iso",
            type=str,
            default=None,
            help="Cutover timestamp in ISO format (e.g., 2026-02-19T12:00:00Z). Defaults to now UTC.",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Clear the cutover (set to NULL), allowing all deals to be ingested.",
        )

    def handle(self, *args, **options):
        account_id = options["account_id"]
        cutover_iso = options.get("cutover_iso")
        clear = options.get("clear", False)

        try:
            account = TradingAccount.objects.get(id=account_id)
        except TradingAccount.DoesNotExist:
            self.stderr.write(f"[ERROR] Account {account_id} not found")
            return

        old_cutover = account.ingest_cutover_time

        if clear:
            account.ingest_cutover_time = None
            account.save(update_fields=["ingest_cutover_time", "updated_at"])
            self.stdout.write(
                f"[OK] Cleared ingest_cutover_time for account={account_id} "
                f"(was: {old_cutover.isoformat() if old_cutover else 'NULL'})"
            )
            return

        if cutover_iso:
            cutover_dt = parse_datetime(cutover_iso)
            if not cutover_dt:
                self.stderr.write(f"[ERROR] Cannot parse: {cutover_iso}")
                return
        else:
            cutover_dt = timezone.now()

        account.ingest_cutover_time = cutover_dt
        account.save(update_fields=["ingest_cutover_time", "updated_at"])

        self.stdout.write(
            f"[OK] Set ingest_cutover_time for account={account_id}\n"
            f"  old: {old_cutover.isoformat() if old_cutover else 'NULL'}\n"
            f"  new: {cutover_dt.isoformat()}\n"
            f"  Deals with time < {cutover_dt.isoformat()} will be skipped during ingest."
        )
