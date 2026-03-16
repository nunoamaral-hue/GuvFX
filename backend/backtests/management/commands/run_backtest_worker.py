"""
Packet B — B2: Platform-native backtest worker.

Polls for queued BacktestExecution rows and processes them using
the atomic DB-claim pattern established by the platform.

Usage:
    python manage.py run_backtest_worker
    python manage.py run_backtest_worker --once
    python manage.py run_backtest_worker --poll-interval 5

This command is the Linux backtest worker entrypoint.
It must never be run on the Windows MT5 execution node.
"""
import signal
import time

from django.core.management.base import BaseCommand

from backtests.services import (
    claim_next_execution,
    get_worker_hostname,
    run_backtest_execution,
)


class Command(BaseCommand):
    help = (
        "Run the backtest worker: poll for queued BacktestExecution rows "
        "and process them using the atomic DB-claim pattern."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shutdown_requested = False

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            default=False,
            help="Process at most one execution, then exit.",
        )
        parser.add_argument(
            "--poll-interval",
            type=int,
            default=10,
            help="Seconds between poll cycles (default: 10).",
        )

    def handle(self, *args, **options):
        once = options["once"]
        poll_interval = options["poll_interval"]
        worker_hostname = get_worker_hostname()

        self.stdout.write(
            self.style.NOTICE(
                f"Backtest worker starting (hostname={worker_hostname}, "
                f"poll_interval={poll_interval}s, once={once})"
            )
        )

        # Register graceful shutdown on SIGINT / SIGTERM
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        processed = 0

        while not self._shutdown_requested:
            execution = claim_next_execution(worker_hostname)

            if execution is None:
                if once:
                    self.stdout.write(
                        self.style.WARNING("No queued executions found. Exiting (--once).")
                    )
                    break
                # Poll loop — sleep then retry
                time.sleep(poll_interval)
                continue

            # Process the claimed execution
            self.stdout.write(
                f"Claimed execution {execution.run_identifier} "
                f"(job {execution.backtest_job_id})"
            )

            try:
                run_backtest_execution(execution)
                processed += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Execution {execution.run_identifier} completed."
                    )
                )
            except Exception as exc:
                # Failure is already recorded in services.py
                self.stderr.write(
                    self.style.ERROR(
                        f"Execution {execution.run_identifier} failed: {exc}"
                    )
                )

            if once:
                break

        self.stdout.write(
            self.style.SUCCESS(
                f"Backtest worker shutting down. Processed {processed} execution(s)."
            )
        )

    def _handle_signal(self, signum, frame):
        """Request graceful shutdown on signal."""
        self.stdout.write(
            self.style.WARNING(
                f"Received signal {signum}, requesting shutdown after current execution..."
            )
        )
        self._shutdown_requested = True
