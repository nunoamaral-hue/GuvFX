"""
H4 Auto Evaluation Scheduler for Trendline Break Pocket (TBP) Strategy

This management command runs every minute via cron and triggers signal evaluation
at H4 bar closes (00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC).

Key features:
- Grace window (default 70s) to handle cron jitter
- Idempotency: only one PLACE_ORDER per (account, strategy, symbol, bar_close_time)
- Targets only TBP strategies (template_slug = "trendline-break-pocket-ali")
- Safe: uses existing signal engine with all safety rails

Usage:
    # Run via cron every minute
    * * * * * docker compose exec -T guvfx-backend python manage.py run_h4_scheduler

    # Dry run (no jobs created)
    python manage.py run_h4_scheduler --dry-run

    # Force evaluation for specific bar close (testing)
    python manage.py run_h4_scheduler --force-bar-close-iso "2026-02-16T08:00:00Z"
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from execution.models import ExecutionJob
from strategies.models import Strategy, StrategyAssignment
from strategies.signal_engine import run_signal_evaluation
from trading.models import TradingAccount

logger = logging.getLogger(__name__)

# H4 close hours in UTC
H4_CLOSE_HOURS = [0, 4, 8, 12, 16, 20]

# TBP template slug
TBP_TEMPLATE_SLUG = "trendline-break-pocket-ali"


def get_current_h4_bar_close(now_utc: datetime) -> Optional[datetime]:
    """
    Get the most recent H4 bar close time relative to now_utc.

    Returns the bar close time if now_utc is within the current H4 bar,
    or the previous H4 close if we're past the close.
    """
    hour = now_utc.hour
    # Find the most recent H4 close hour
    for h in reversed(H4_CLOSE_HOURS):
        if hour >= h:
            return now_utc.replace(hour=h, minute=0, second=0, microsecond=0)

    # If before 04:00, the previous close was at 20:00 yesterday
    return (now_utc - timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)


def is_within_close_window(now_utc: datetime, grace_seconds: int) -> tuple[bool, Optional[datetime]]:
    """
    Check if now_utc is within grace_seconds of an H4 bar close.

    Returns (is_in_window, bar_close_time).
    """
    # Check each H4 close hour for today
    for h in H4_CLOSE_HOURS:
        bar_close = now_utc.replace(hour=h, minute=0, second=0, microsecond=0)

        # Check if we're within the grace window after the close
        window_start = bar_close
        window_end = bar_close + timedelta(seconds=grace_seconds)

        if window_start <= now_utc <= window_end:
            return True, bar_close

    # Also check yesterday's 20:00 close (for early morning runs)
    yesterday_close = (now_utc - timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    window_end = yesterday_close + timedelta(seconds=grace_seconds)
    if yesterday_close <= now_utc <= window_end:
        return True, yesterday_close

    return False, None


def job_exists_for_bar_close(
    account_id: int,
    strategy_id: int,
    symbol: str,
    bar_close_iso: str,
) -> bool:
    """
    Check if a PLACE_ORDER job already exists for this bar close.

    Idempotency key: account_id + strategy_id + symbol + bar_close_time
    """
    return ExecutionJob.objects.filter(
        job_type=ExecutionJob.JobType.PLACE_ORDER,
        account_id=account_id,
        strategy_id=strategy_id,
        payload__symbol=symbol,
        payload__bar_close_time=bar_close_iso,
    ).exists()


class Command(BaseCommand):
    help = "Run H4 auto evaluation for Trendline Break Pocket strategies"

    def add_arguments(self, parser):
        parser.add_argument(
            "--grace-seconds",
            type=int,
            default=70,
            help="Grace window in seconds after H4 close (default: 70)",
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
            help="Force evaluation for specific bar close time (ISO format, e.g., 2026-02-16T08:00:00Z)",
        )

    def handle(self, *args, **options):
        grace_seconds = options["grace_seconds"]
        account_filter = options.get("account_id")
        strategy_filter = options.get("strategy_id")
        dry_run = options["dry_run"]
        force_bar_close = options.get("force_bar_close_iso")

        now_utc = timezone.now()

        # Determine bar close time
        if force_bar_close:
            # Parse forced bar close time
            try:
                bar_close_time = datetime.fromisoformat(force_bar_close.replace("Z", "+00:00"))
                bar_close_time = bar_close_time.replace(tzinfo=timezone.utc)
            except ValueError as e:
                self.stderr.write(f"[ERROR] Invalid ISO format: {force_bar_close} - {e}")
                return

            self.stdout.write(f"[FORCE] Using forced bar_close_time={bar_close_time.isoformat()}")
        else:
            # Check if we're within the close window
            in_window, bar_close_time = is_within_close_window(now_utc, grace_seconds)

            if not in_window:
                self.stdout.write(
                    f"[SKIP] Not within H4 close window. "
                    f"now_utc={now_utc.isoformat()}, grace_seconds={grace_seconds}"
                )
                return

        bar_close_iso = bar_close_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.stdout.write(
            f"[FIRE] H4 evaluation triggered. "
            f"bar_close_time={bar_close_iso}, now_utc={now_utc.isoformat()}"
        )

        # Find active TBP strategy assignments
        assignments_qs = StrategyAssignment.objects.filter(
            is_active=True,
            strategy__is_active=True,
            account__is_active=True,
        ).select_related("strategy", "account")

        # Filter to TBP strategies only
        assignments_qs = assignments_qs.filter(
            strategy__filters__template_slug=TBP_TEMPLATE_SLUG
        )

        if account_filter:
            assignments_qs = assignments_qs.filter(account_id=account_filter)

        if strategy_filter:
            assignments_qs = assignments_qs.filter(strategy_id=strategy_filter)

        assignments = list(assignments_qs)
        self.stdout.write(f"[INFO] Found {len(assignments)} active TBP assignments")

        if not assignments:
            self.stdout.write("[INFO] No active TBP assignments to evaluate")
            return

        # Process each assignment
        for assignment in assignments:
            strategy = assignment.strategy
            account = assignment.account

            # Determine symbols to evaluate
            filters = strategy.filters or {}
            pairs_enabled = filters.get("pairs_enabled") or []

            if not pairs_enabled:
                # Fallback to symbol_universe (comma-separated)
                symbol_universe = (strategy.symbol_universe or "").strip()
                if symbol_universe:
                    pairs_enabled = [s.strip().upper() for s in symbol_universe.split(",") if s.strip()]
                else:
                    pairs_enabled = ["EURUSD", "GBPUSD"]  # Default

            self.stdout.write(
                f"[INFO] Evaluating strategy={strategy.id} account={account.id} "
                f"symbols={pairs_enabled}"
            )

            for symbol in pairs_enabled:
                symbol = symbol.strip().upper()

                # Idempotency check (outside transaction for early skip)
                if job_exists_for_bar_close(account.id, strategy.id, symbol, bar_close_iso):
                    self.stdout.write(
                        f"  [SKIP] Job already exists: account={account.id} "
                        f"strategy={strategy.id} symbol={symbol} bar_close={bar_close_iso}"
                    )
                    continue

                if dry_run:
                    self.stdout.write(
                        f"  [DRY-RUN] Would evaluate: account={account.id} "
                        f"strategy={strategy.id} symbol={symbol}"
                    )
                    continue

                # Run signal evaluation in AUTO mode
                try:
                    with transaction.atomic():
                        # Double-check idempotency inside transaction (race prevention)
                        if job_exists_for_bar_close(account.id, strategy.id, symbol, bar_close_iso):
                            self.stdout.write(
                                f"  [SKIP-RACE] Job created by concurrent process: "
                                f"account={account.id} strategy={strategy.id} symbol={symbol}"
                            )
                            continue

                        # Call signal engine in AUTO mode (no manual_params)
                        # Pass bar_close_time via a special mechanism
                        result = run_signal_evaluation(
                            request=None,
                            strategy=strategy,
                            account=account,
                            symbol=symbol,
                            user=None,  # System-triggered
                            manual_params=None,  # AUTO mode
                            bar_close_time=bar_close_iso,  # For idempotency in job payload
                        )

                        if result.ok and result.job_id:
                            self.stdout.write(
                                f"  [EVAL] SUCCESS: account={account.id} strategy={strategy.id} "
                                f"symbol={symbol} signal={result.signal_type} job_id={result.job_id}"
                            )
                        elif result.ok and not result.signal_type:
                            self.stdout.write(
                                f"  [EVAL] NO_SIGNAL: account={account.id} strategy={strategy.id} "
                                f"symbol={symbol} reason={result.reason}"
                            )
                        else:
                            self.stdout.write(
                                f"  [EVAL] REJECTED: account={account.id} strategy={strategy.id} "
                                f"symbol={symbol} reason={result.reason}"
                            )

                except Exception as e:
                    self.stderr.write(
                        f"  [ERROR] Exception during evaluation: account={account.id} "
                        f"strategy={strategy.id} symbol={symbol} error={str(e)}"
                    )
                    logger.exception(f"H4 scheduler evaluation error: {e}")
                    continue

        self.stdout.write(f"[DONE] H4 evaluation complete for bar_close={bar_close_iso}")
