"""
H1 Auto Evaluation Scheduler for SCE (Structural Continuation Engine)

This management command runs every minute via cron and triggers SCE signal
evaluation at H1 bar closes (every hour at :00 UTC).

Key features:
- Grace window (default 45s) for cron jitter
- Idempotency: one PLACE_ORDER per (account, strategy, symbol, bar_close_time)
- Targets only SCE strategies (template_slug = "structural-continuation-engine")
- TEST stage: evaluates for observability, no PLACE_ORDER jobs
- LIVE stage: evaluates and creates PLACE_ORDER jobs

Usage:
    # Run via cron every minute
    * * * * * docker compose exec -T guvfx-backend python manage.py run_h1_scheduler

    # Dry run
    python manage.py run_h1_scheduler --dry-run

    # Force evaluation for specific bar close
    python manage.py run_h1_scheduler --force-bar-close-iso "2026-02-20T12:00:00Z"

    # Force-once: create ONE test evaluation for end-to-end testing
    python manage.py run_h1_scheduler --force-once --account-id 1 --strategy-id 1
"""

import datetime as dt
import logging
from datetime import datetime, timedelta
from typing import Optional

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from execution.models import ExecutionJob
from strategies.engines.sce_engine import SCE_TEMPLATE_SLUG, evaluate_sce
from strategies.management.commands.run_h4_scheduler import job_exists_for_bar_close
from strategies.models import Strategy, StrategyAssignment, StrategyRuntimeEvent
from strategies.risk_manager import record_signal_event, ORDER_PLACED
from strategies.signal_engine import create_place_order_job
from trading.models import TradingAccount

logger = logging.getLogger(__name__)


def is_within_h1_close_window(
    now_utc: datetime,
    grace_seconds: int,
) -> tuple[bool, Optional[datetime]]:
    """
    Check if now_utc is within grace_seconds of an H1 bar close.

    H1 bars close at :00 of every hour.
    Returns (is_in_window, bar_close_time).
    """
    # Current hour close
    current_close = now_utc.replace(minute=0, second=0, microsecond=0)
    window_end = current_close + timedelta(seconds=grace_seconds)

    if current_close <= now_utc <= window_end:
        return True, current_close

    # Also check previous hour close (in case cron runs late)
    prev_close = current_close - timedelta(hours=1)
    prev_window_end = prev_close + timedelta(seconds=grace_seconds)
    if prev_close <= now_utc <= prev_window_end:
        return True, prev_close

    return False, None


class Command(BaseCommand):
    help = "Run H1 auto evaluation for SCE strategies"

    def add_arguments(self, parser):
        parser.add_argument(
            "--grace-seconds",
            type=int,
            default=45,
            help="Grace window in seconds after H1 close (default: 45)",
        )
        parser.add_argument(
            "--account-id",
            type=int,
            help="Filter to specific account ID",
        )
        parser.add_argument(
            "--strategy-id",
            type=int,
            help="Filter to specific strategy ID",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be evaluated without creating jobs",
        )
        parser.add_argument(
            "--force-bar-close-iso",
            type=str,
            help="Force evaluation for specific bar close time (ISO format)",
        )
        parser.add_argument(
            "--force-once",
            action="store_true",
            help="Force exactly ONE evaluation for end-to-end testing.",
        )

    def handle(self, *args, **options):
        grace_seconds = options["grace_seconds"]
        account_filter = options.get("account_id")
        strategy_filter = options.get("strategy_id")
        dry_run = options["dry_run"]
        force_bar_close = options.get("force_bar_close_iso")
        force_once = options.get("force_once", False)

        now_utc = timezone.now()

        # Determine bar close time
        if force_bar_close:
            try:
                bar_close_time = datetime.fromisoformat(
                    force_bar_close.replace("Z", "+00:00")
                )
                bar_close_time = bar_close_time.replace(tzinfo=dt.timezone.utc)
            except ValueError as e:
                self.stderr.write(f"[ERROR] Invalid ISO format: {force_bar_close} - {e}")
                return

            self.stdout.write(
                f"[FORCE] Using forced bar_close_time={bar_close_time.isoformat()}"
            )
        else:
            in_window, bar_close_time = is_within_h1_close_window(now_utc, grace_seconds)

            if not in_window:
                # Silent exit — H1 runs every minute, most runs are no-ops
                return

        bar_close_iso = bar_close_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.stdout.write(
            f"[FIRE] H1 SCE evaluation triggered. "
            f"bar_close_time={bar_close_iso}, now_utc={now_utc.isoformat()}"
        )

        # Find active SCE strategy assignments
        assignments_qs = StrategyAssignment.objects.filter(
            is_active=True,
            strategy__is_active=True,
            account__is_active=True,
        ).select_related("strategy", "account")

        # Filter to SCE strategies only
        assignments_qs = assignments_qs.filter(
            strategy__filters__template_slug=SCE_TEMPLATE_SLUG
        )

        if account_filter:
            assignments_qs = assignments_qs.filter(account_id=account_filter)

        if strategy_filter:
            assignments_qs = assignments_qs.filter(strategy_id=strategy_filter)

        all_assignments = list(assignments_qs)

        if not all_assignments:
            self.stdout.write("[INFO] No active SCE assignments found")
            return

        # Separate by stage
        if force_once:
            assignments = all_assignments
        else:
            assignments = []
            for a in all_assignments:
                stage = getattr(a, "stage", "TEST")
                if stage == "LIVE":
                    assignments.append(a)
                else:
                    self._evaluate_test_stage(a, bar_close_iso, dry_run)

        self.stdout.write(
            f"[INFO] Found {len(all_assignments)} SCE assignments, "
            f"{len(assignments)} LIVE (will create jobs)"
        )

        # Process LIVE assignments (or all in force-once mode)
        for assignment in assignments:
            self._evaluate_live(assignment, bar_close_iso, dry_run, now_utc)

        self.stdout.write(f"[DONE] H1 SCE evaluation complete for bar_close={bar_close_iso}")

    def _evaluate_test_stage(
        self,
        assignment,
        bar_close_iso: str,
        dry_run: bool,
    ) -> None:
        """
        Evaluate SCE in TEST stage: run the engine for observability
        but do NOT create PLACE_ORDER jobs.
        """
        strategy = assignment.strategy
        account = assignment.account
        filters = strategy.filters or {}
        pairs = filters.get("pairs_enabled") or ["EURUSD", "GBPUSD"]

        for symbol in pairs:
            symbol = symbol.strip().upper()

            if dry_run:
                self.stdout.write(
                    f"  [TEST-DRY] Would evaluate: strategy={strategy.id} "
                    f"account={account.id} symbol={symbol}"
                )
                continue

            try:
                result = evaluate_sce(
                    assignment=assignment,
                    symbol=symbol,
                    now_ts=timezone.now(),
                    bar_close_time=bar_close_iso,
                )

                if result.signal_type:
                    self.stdout.write(
                        f"  [TEST-SIGNAL] strategy={strategy.id} symbol={symbol} "
                        f"signal={result.signal_type} (no job — TEST stage)"
                    )
                    print(
                        f"[H1-TEST] strategy={strategy.id} symbol={symbol} "
                        f"signal={result.signal_type} bar={bar_close_iso}"
                    )
                else:
                    self.stdout.write(
                        f"  [TEST-SKIP] strategy={strategy.id} symbol={symbol} "
                        f"reason={result.reason}"
                    )

            except Exception as e:
                self.stderr.write(
                    f"  [TEST-ERROR] strategy={strategy.id} symbol={symbol} "
                    f"error={e}"
                )
                logger.exception(f"H1 TEST evaluation error: {e}")

    def _evaluate_live(
        self,
        assignment,
        bar_close_iso: str,
        dry_run: bool,
        now_utc: datetime,
    ) -> None:
        """
        Evaluate SCE in LIVE stage: run engine and create PLACE_ORDER jobs.
        """
        strategy = assignment.strategy
        account = assignment.account
        filters = strategy.filters or {}
        pairs = filters.get("pairs_enabled") or ["EURUSD", "GBPUSD"]

        for symbol in pairs:
            symbol = symbol.strip().upper()

            print(
                f"[H1] account={account.id} strategy={strategy.id} "
                f"symbol={symbol} bar={bar_close_iso}"
            )

            # Idempotency check
            if job_exists_for_bar_close(account.id, strategy.id, symbol, bar_close_iso):
                self.stdout.write(
                    f"  [SKIP-IDEMPOTENT] Job exists: account={account.id} "
                    f"strategy={strategy.id} symbol={symbol} bar={bar_close_iso}"
                )
                continue

            if dry_run:
                self.stdout.write(
                    f"  [DRY-RUN] Would evaluate: account={account.id} "
                    f"strategy={strategy.id} symbol={symbol}"
                )
                continue

            try:
                with transaction.atomic():
                    # Double-check idempotency inside transaction
                    if job_exists_for_bar_close(
                        account.id, strategy.id, symbol, bar_close_iso
                    ):
                        self.stdout.write(
                            f"  [SKIP-RACE] Job created by concurrent process"
                        )
                        continue

                    result = evaluate_sce(
                        assignment=assignment,
                        symbol=symbol,
                        now_ts=now_utc,
                        bar_close_time=bar_close_iso,
                    )

                    if result.ok and result.signal_type:
                        # Create PLACE_ORDER job
                        job = create_place_order_job(
                            request=None,
                            strategy=strategy,
                            account=account,
                            assignment=assignment,
                            signal=result,
                            user=None,
                            bar_close_time=bar_close_iso,
                        )
                        result.job_id = job.id

                        self.stdout.write(
                            f"  [EVAL] SUCCESS: account={account.id} "
                            f"strategy={strategy.id} symbol={symbol} "
                            f"signal={result.signal_type} job_id={job.id}"
                        )
                        print(
                            f"[H1-LIVE] job_id={job.id} account={account.id} "
                            f"strategy={strategy.id} symbol={symbol} "
                            f"signal={result.signal_type} bar={bar_close_iso}"
                        )
                    elif result.ok and not result.signal_type:
                        self.stdout.write(
                            f"  [EVAL] NO_SIGNAL: account={account.id} "
                            f"strategy={strategy.id} symbol={symbol} "
                            f"reason={result.reason}"
                        )
                    else:
                        self.stdout.write(
                            f"  [EVAL] REJECTED: account={account.id} "
                            f"strategy={strategy.id} symbol={symbol} "
                            f"reason={result.reason}"
                        )

            except Exception as e:
                self.stderr.write(
                    f"  [ERROR] Exception: account={account.id} "
                    f"strategy={strategy.id} symbol={symbol} error={e}"
                )
                logger.exception(f"H1 LIVE evaluation error: {e}")
                continue
