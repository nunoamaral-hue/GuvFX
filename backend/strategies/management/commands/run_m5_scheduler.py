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

        # Separate by stage — only LIVE gets PLACE_ORDER jobs (even force-once)
        assignments = []
        for a in all_assignments:
            stage = getattr(a, "stage", "TEST")
            if stage == "LIVE":
                assignments.append(a)
            elif not force_once:
                # TEST stage: evaluate for observability but do NOT create jobs
                self._evaluate_test_stage(a, bar_close_iso, dry_run)
            else:
                self.stdout.write(
                    f"  [SKIP-STAGE] assignment={a.id} strategy={a.strategy_id} "
                    f"account={a.account_id} stage={stage} (force-once requires LIVE)"
                )

        self.stdout.write(
            f"[INFO] Found {len(all_assignments)} ALTS assignments, "
            f"{len(assignments)} LIVE (will create jobs)"
        )

        # --force-once mode: create exactly ONE test PLACE_ORDER job
        if force_once:
            self._handle_force_once(
                assignments=assignments,
                bar_close_iso=bar_close_iso,
                dry_run=dry_run,
            )
            return

        # Process LIVE assignments
        for assignment in assignments:
            self._evaluate_live(assignment, bar_close_iso, dry_run, now_utc)

        self.stdout.write(f"[DONE] M5 ALTS evaluation complete for bar_close={bar_close_iso}")

    def _handle_force_once(
        self,
        assignments: list,
        bar_close_iso: str,
        dry_run: bool,
    ) -> None:
        """
        Create exactly ONE test PLACE_ORDER job for end-to-end testing.

        Bypasses ALTS signal logic but keeps safety rails:
        - Demo accounts only
        - LIVE stage only (caller pre-filters)
        - SL/TP placeholders (Windows bridge overrides for forced_once_test)
        - Idempotent per bar_close_time
        - Prefers EURUSD as the first eligible symbol.
        """
        PREFERRED_SYMBOLS = ["EURUSD", "GBPUSD"]

        self.stdout.write("[FORCE-ONCE] Looking for first eligible ALTS assignment...")

        for assignment in assignments:
            strategy = assignment.strategy
            account = assignment.account

            # Safety: Demo only
            if not account.is_demo:
                self.stdout.write(
                    f"  [SKIP] account={account.id} is not demo"
                )
                continue

            # Resolve symbols
            filters = strategy.filters or {}
            pairs_enabled = filters.get("pairs_enabled") or PREFERRED_SYMBOLS

            # Pick first eligible symbol (prefer EURUSD)
            selected_symbol = None
            for symbol in PREFERRED_SYMBOLS:
                if symbol in pairs_enabled:
                    selected_symbol = symbol
                    break
            if not selected_symbol:
                selected_symbol = pairs_enabled[0] if pairs_enabled else "EURUSD"

            # Idempotency check
            if job_exists_for_bar_close(account.id, strategy.id, selected_symbol, bar_close_iso):
                self.stdout.write(
                    f"  [SKIP-IDEMPOTENT] Job already exists: account={account.id} "
                    f"strategy={strategy.id} symbol={selected_symbol} bar_close={bar_close_iso}"
                )
                continue

            if dry_run:
                self.stdout.write(
                    f"  [DRY-RUN] Would create PLACE_ORDER: account={account.id} "
                    f"strategy={strategy.id} symbol={selected_symbol}"
                )
                return

            try:
                with transaction.atomic():
                    # Double-check idempotency inside transaction
                    if job_exists_for_bar_close(account.id, strategy.id, selected_symbol, bar_close_iso):
                        self.stdout.write(
                            f"  [SKIP-RACE] Job created by concurrent process"
                        )
                        return

                    # Resolve windows_username from account's mt5_instance
                    windows_username = None
                    if account.mt5_instance:
                        windows_username = getattr(account.mt5_instance, "windows_username", None)

                    # Build payload — placeholders for SL/TP (Windows bridge overrides
                    # for signal_reason=forced_once_test using live tick + configurable pips)
                    payload = {
                        "symbol": selected_symbol,
                        "side": "SELL",
                        "lots": 0.01,
                        "entry_price": 0,
                        "sl_price": 0,
                        "tp_price": 0,
                        "comment": f"GS{strategy.id:04d}",
                        "magic": strategy.magic_number or strategy.id,
                        "is_demo": account.is_demo,
                        "strategy_id": strategy.id,
                        "windows_username": windows_username,
                        "signal_reason": "forced_once_test",
                        "assignment_stage": "LIVE",
                        "bar_close_time": bar_close_iso,
                        "safety_rails": {
                            "max_lots": 0.02,
                            "allowed_symbols": ["EURUSD", "GBPUSD"],
                            "demo_only": True,
                        },
                    }

                    job = ExecutionJob.objects.create(
                        job_type=ExecutionJob.JobType.PLACE_ORDER,
                        account=account,
                        strategy=strategy,
                        assignment=assignment,
                        status=ExecutionJob.Status.PENDING,
                        created_by=None,
                        payload=payload,
                    )

                    # Update comment with actual job ID (GS tag)
                    job.payload["comment"] = f"GS{job.id:04d}"
                    job.save(update_fields=["payload"])

                    self.stdout.write(
                        f"[FORCE-ONCE] SUCCESS: created PLACE_ORDER job_id={job.id} "
                        f"account={account.id} strategy={strategy.id} symbol={selected_symbol} "
                        f"side=SELL lots=0.01"
                    )
                    print(
                        f"[FORCE-ONCE] job_id={job.id} account={account.id} strategy={strategy.id} "
                        f"symbol={selected_symbol} bar={bar_close_iso}"
                    )
                    return

            except Exception as e:
                self.stderr.write(
                    f"  [ERROR] Failed to create job: {str(e)}"
                )
                logger.exception(f"Force-once job creation error: {e}")
                return

        self.stdout.write("[FORCE-ONCE] No eligible LIVE ALTS assignment found for forced test")

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
