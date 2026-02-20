"""
M5 Auto Evaluation Scheduler for ALTS (Adaptive Liquidity Trap Scalper)

This management command runs every minute via cron and triggers ALTS signal
evaluation at M5 bar closes (every 5 minutes: :00, :05, :10, ..., :55 UTC).

Key features:
- Grace window (default 30s) for cron jitter
- Idempotency: one PLACE_ORDER per (account, strategy, symbol, bar_close_time)
- Targets only ALTS strategies (template_slug = "adaptive-liquidity-trap-scalper")
- TEST stage: evaluates but does NOT create PLACE_ORDER jobs (observability only)
- LIVE stage: evaluates and creates PLACE_ORDER jobs

Usage:
    # Run via cron every minute
    * * * * * docker compose exec -T guvfx-backend python manage.py run_m5_scheduler

    # Dry run
    python manage.py run_m5_scheduler --dry-run

    # Force evaluation for specific bar close
    python manage.py run_m5_scheduler --force-bar-close-iso "2026-02-20T12:05:00Z"

    # Force-once: create ONE test PLACE_ORDER for end-to-end testing
    python manage.py run_m5_scheduler --force-once --account-id 1 --strategy-id 1
"""

import datetime as dt
import logging
from datetime import datetime, timedelta
from typing import Optional

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from execution.models import ExecutionJob
from strategies.engines.alts_engine import ALTS_TEMPLATE_SLUG, evaluate_alts
from strategies.management.commands.run_h4_scheduler import job_exists_for_bar_close
from strategies.models import Strategy, StrategyAssignment, StrategyRuntimeEvent
from strategies.risk_manager import record_signal_event, ORDER_PLACED
from strategies.signal_engine import create_place_order_job
from trading.models import TradingAccount

logger = logging.getLogger(__name__)


def get_current_m5_bar_close(now_utc: datetime) -> datetime:
    """
    Get the most recent M5 bar close time relative to now_utc.

    M5 bars close at :00, :05, :10, ..., :55 of every hour.
    """
    minute = now_utc.minute
    m5_close_minute = (minute // 5) * 5
    return now_utc.replace(minute=m5_close_minute, second=0, microsecond=0)


def is_within_m5_close_window(
    now_utc: datetime,
    grace_seconds: int,
) -> tuple[bool, Optional[datetime]]:
    """
    Check if now_utc is within grace_seconds of an M5 bar close.

    Returns (is_in_window, bar_close_time).
    """
    # Check current and previous M5 close
    current_close = get_current_m5_bar_close(now_utc)

    # We might be in the window of the just-passed close
    for close_time in [current_close, current_close - timedelta(minutes=5)]:
        window_start = close_time
        window_end = close_time + timedelta(seconds=grace_seconds)
        if window_start <= now_utc <= window_end:
            return True, close_time

    return False, None


class Command(BaseCommand):
    help = "Run M5 auto evaluation for ALTS strategies"

    def add_arguments(self, parser):
        parser.add_argument(
            "--grace-seconds",
            type=int,
            default=30,
            help="Grace window in seconds after M5 close (default: 30)",
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
            help="Force exactly ONE evaluation for end-to-end testing. "
                 "Requires --account-id and --strategy-id.",
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
            in_window, bar_close_time = is_within_m5_close_window(now_utc, grace_seconds)

            if not in_window:
                # Silent exit — M5 runs every minute, most runs are no-ops
                return

        bar_close_iso = bar_close_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.stdout.write(
            f"[FIRE] M5 ALTS evaluation triggered. "
            f"bar_close_time={bar_close_iso}, now_utc={now_utc.isoformat()}"
        )

        # Find active ALTS strategy assignments
        assignments_qs = StrategyAssignment.objects.filter(
            is_active=True,
            strategy__is_active=True,
            account__is_active=True,
        ).select_related("strategy", "account")

        # Filter to ALTS strategies only
        assignments_qs = assignments_qs.filter(
            strategy__filters__template_slug=ALTS_TEMPLATE_SLUG
        )

        if account_filter:
            assignments_qs = assignments_qs.filter(account_id=account_filter)

        if strategy_filter:
            assignments_qs = assignments_qs.filter(strategy_id=strategy_filter)

        all_assignments = list(assignments_qs)

        if not all_assignments:
            self.stdout.write("[INFO] No active ALTS assignments found")
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
                    # TEST stage: evaluate for observability but do NOT create jobs
                    self._evaluate_test_stage(a, bar_close_iso, dry_run)

        self.stdout.write(
            f"[INFO] Found {len(all_assignments)} ALTS assignments, "
            f"{len(assignments)} LIVE (will create jobs)"
        )

        # Process LIVE assignments (or all in force-once mode)
        for assignment in assignments:
            self._evaluate_live(assignment, bar_close_iso, dry_run, now_utc)

        self.stdout.write(f"[DONE] M5 ALTS evaluation complete for bar_close={bar_close_iso}")

    def _evaluate_test_stage(
        self,
        assignment,
        bar_close_iso: str,
        dry_run: bool,
    ) -> None:
        """
        Evaluate ALTS in TEST stage: run the engine for observability
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
                result = evaluate_alts(
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
                        f"[M5-TEST] strategy={strategy.id} symbol={symbol} "
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
                logger.exception(f"M5 TEST evaluation error: {e}")

    def _evaluate_live(
        self,
        assignment,
        bar_close_iso: str,
        dry_run: bool,
        now_utc: datetime,
    ) -> None:
        """
        Evaluate ALTS in LIVE stage: run engine and create PLACE_ORDER jobs.
        """
        strategy = assignment.strategy
        account = assignment.account
        filters = strategy.filters or {}
        pairs = filters.get("pairs_enabled") or ["EURUSD", "GBPUSD"]

        for symbol in pairs:
            symbol = symbol.strip().upper()

            print(
                f"[M5] account={account.id} strategy={strategy.id} "
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

                    result = evaluate_alts(
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
                            f"[M5-LIVE] job_id={job.id} account={account.id} "
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
                logger.exception(f"M5 LIVE evaluation error: {e}")
                continue
