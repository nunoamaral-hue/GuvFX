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

    # Force create exactly ONE test PLACE_ORDER job (end-to-end testing)
    python manage.py run_h4_scheduler --force-once --account-id 1 --strategy-id 1
"""

import datetime as dt
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

    def _handle_force_once(
        self,
        assignments: list,
        bar_close_iso: str,
        dry_run: bool,
    ) -> None:
        """
        Create exactly ONE test PLACE_ORDER job for end-to-end testing.

        This bypasses signal logic but keeps safety rails:
        - Demo accounts only
        - Zones must exist for symbol
        - SL/TP required (20 pip SL, 40 pip TP = 2R)
        - Idempotent per bar_close_time

        Prefers EURUSD as the first eligible symbol.
        """
        from decimal import Decimal

        # Priority order for symbols
        PREFERRED_SYMBOLS = ["EURUSD", "GBPUSD"]
        PIP_SIZE = 0.0001  # For EUR/GBP pairs
        SL_PIPS = 20
        TP_PIPS = 40  # 2R

        self.stdout.write("[FORCE-ONCE] Looking for first eligible assignment...")

        for assignment in assignments:
            strategy = assignment.strategy
            account = assignment.account

            # Safety: Demo only
            if not account.is_demo:
                self.stdout.write(
                    f"  [SKIP] account={account.id} is not demo"
                )
                continue

            # Get filters and zones
            filters = strategy.filters or {}
            zones = filters.get("zones") or {}
            pairs_enabled = filters.get("pairs_enabled") or PREFERRED_SYMBOLS

            # Find first eligible symbol with zones
            selected_symbol = None
            selected_zones = None

            # Prefer EURUSD, then GBPUSD, then others
            for symbol in PREFERRED_SYMBOLS:
                if symbol in pairs_enabled and symbol in zones:
                    symbol_zones = zones.get(symbol, [])
                    if symbol_zones:
                        selected_symbol = symbol
                        selected_zones = symbol_zones
                        break

            if not selected_symbol:
                # Fallback to any enabled symbol with zones
                for symbol in pairs_enabled:
                    symbol_zones = zones.get(symbol, [])
                    if symbol_zones:
                        selected_symbol = symbol
                        selected_zones = symbol_zones
                        break

            if not selected_symbol:
                self.stdout.write(
                    f"  [SKIP] strategy={strategy.id} has no symbols with zones"
                )
                continue

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

            # Get first zone for entry reference
            zone = selected_zones[0]
            zone_low = float(zone.get("low", 1.0))
            zone_high = float(zone.get("high", 1.1))
            zone_type = zone.get("zone_type", "demand")

            # Determine side and calculate entry/SL/TP
            if zone_type == "demand":
                side = "BUY"
                # Entry near zone high (where price would break out)
                entry_price = zone_high + (5 * PIP_SIZE)
                sl_price = entry_price - (SL_PIPS * PIP_SIZE)
                tp_price = entry_price + (TP_PIPS * PIP_SIZE)
            else:
                side = "SELL"
                # Entry near zone low (where price would break down)
                entry_price = zone_low - (5 * PIP_SIZE)
                sl_price = entry_price + (SL_PIPS * PIP_SIZE)
                tp_price = entry_price - (TP_PIPS * PIP_SIZE)

            # Round prices
            entry_price = round(entry_price, 5)
            sl_price = round(sl_price, 5)
            tp_price = round(tp_price, 5)

            # Create job directly (bypassing signal engine)
            try:
                with transaction.atomic():
                    # Double-check idempotency inside transaction
                    if job_exists_for_bar_close(account.id, strategy.id, selected_symbol, bar_close_iso):
                        self.stdout.write(
                            f"  [SKIP-RACE] Job created by concurrent process"
                        )
                        return

                    # Get windows_username from account's mt5_instance
                    windows_username = None
                    if account.mt5_instance:
                        windows_username = getattr(account.mt5_instance, "windows_username", None)

                    # Build payload
                    payload = {
                        "symbol": selected_symbol,
                        "side": side,
                        "lots": 0.01,  # Minimum lot for testing
                        "entry_price": entry_price,
                        "sl_price": sl_price,
                        "tp_price": tp_price,
                        "comment": f"GS{strategy.id:04d}",  # Will update with job ID
                        "magic": strategy.magic_number or strategy.id,
                        "is_demo": account.is_demo,
                        "strategy_id": strategy.id,
                        "windows_username": windows_username,
                        "zone_name": zone.get("name", f"{zone_type}_{selected_symbol}"),
                        "signal_reason": "forced_once_test",
                        "assignment_stage": getattr(assignment, "stage", "TEST"),
                        "bar_close_time": bar_close_iso,
                        "safety_rails": {
                            "max_lots": 0.02,
                            "allowed_symbols": ["EURUSD", "GBPUSD"],
                            "demo_only": True,
                        },
                    }

                    # Create the job
                    job = ExecutionJob.objects.create(
                        job_type=ExecutionJob.JobType.PLACE_ORDER,
                        account=account,
                        strategy=strategy,
                        assignment=assignment,
                        status=ExecutionJob.Status.PENDING,
                        created_by=None,  # System-triggered
                        payload=payload,
                    )

                    # Update comment with actual job ID (GS tag)
                    job.payload["comment"] = f"GS{job.id:04d}"
                    job.save(update_fields=["payload"])

                    self.stdout.write(
                        f"[FORCE-ONCE] SUCCESS: created PLACE_ORDER job_id={job.id} "
                        f"account={account.id} strategy={strategy.id} symbol={selected_symbol} "
                        f"side={side} lots=0.01 entry={entry_price} sl={sl_price} tp={tp_price}"
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

        self.stdout.write("[FORCE-ONCE] No eligible assignment found for forced test")

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
        parser.add_argument(
            "--force-once",
            action="store_true",
            help="Create exactly ONE test PLACE_ORDER job for first eligible symbol (EURUSD preferred). "
                 "Bypasses signal logic but keeps safety rails (demo, zones, SL/TP required). "
                 "Uses 20 pip SL, 40 pip TP (2R). Idempotent per bar_close_time.",
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
            # Parse forced bar close time
            try:
                bar_close_time = datetime.fromisoformat(force_bar_close.replace("Z", "+00:00"))
                bar_close_time = bar_close_time.replace(tzinfo=dt.timezone.utc)
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

        all_assignments = list(assignments_qs)

        # Separate LIVE from TEST — only LIVE assignments get auto-evaluated
        # (force-once bypasses this gate to allow testing)
        if force_once:
            assignments = all_assignments
        else:
            assignments = []
            for a in all_assignments:
                if getattr(a, "stage", "TEST") == "LIVE":
                    assignments.append(a)
                else:
                    self.stdout.write(
                        f"[SKIP-STAGE] assignment={a.id} strategy={a.strategy_id} "
                        f"account={a.account_id} stage={a.stage} (not LIVE, skipping auto-eval)"
                    )

        self.stdout.write(
            f"[INFO] Found {len(all_assignments)} active TBP assignments, "
            f"{len(assignments)} with stage=LIVE (evaluating)"
        )

        if not assignments:
            self.stdout.write("[INFO] No active TBP assignments to evaluate")
            return

        # --force-once mode: Create exactly ONE test PLACE_ORDER job
        if force_once:
            self._handle_force_once(
                assignments=assignments,
                bar_close_iso=bar_close_iso,
                dry_run=dry_run,
            )
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

                # Safe logging for cron readability
                print(f"[H4] account={account.id} strategy={strategy.id} symbol={symbol} bar={bar_close_iso}")

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
