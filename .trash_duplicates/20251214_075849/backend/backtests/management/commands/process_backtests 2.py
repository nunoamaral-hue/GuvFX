from django.core.management.base import BaseCommand
from django.utils import timezone

from backtests.models import BacktestRun


class Command(BaseCommand):
    help = "Process PENDING backtest runs with a fake/dummy engine."

    def handle(self, *args, **options):
        pending_runs = BacktestRun.objects.filter(
            status=BacktestRun.STATUS_PENDING
        ).select_related("config", "config__strategy", "config__owner")

        if not pending_runs.exists():
            self.stdout.write(self.style.WARNING("No PENDING backtest runs found."))
            return

        self.stdout.write(
            self.style.NOTICE(f"Processing {pending_runs.count()} PENDING backtest run(s)...")
        )

        for run in pending_runs:
            self._process_single_run(run)

        self.stdout.write(self.style.SUCCESS("Done processing backtests."))

    def _process_single_run(self, run: BacktestRun):
        self.stdout.write(f"Processing BacktestRun #{run.id} ({run.config.name})...")

        # Mark as RUNNING
        run.status = BacktestRun.STATUS_RUNNING
        run.started_at = timezone.now()
        run.save(update_fields=["status", "started_at"])

        # Generate dummy metrics
        metrics, equity_curve = self._generate_dummy_results(run)

        # Mark as COMPLETED
        run.status = BacktestRun.STATUS_COMPLETED
        run.finished_at = timezone.now()
        run.metrics = metrics
        run.equity_curve = equity_curve
        run.error_message = ""

        run.save(
            update_fields=[
                "status",
                "finished_at",
                "metrics",
                "equity_curve",
                "error_message",
            ]
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"BacktestRun #{run.id} completed: "
                f"total_return={metrics.get('total_return_pct')}%, "
                f"max_dd={metrics.get('max_drawdown_pct')}%"
            )
        )

    def _generate_dummy_results(self, run: BacktestRun):
        """
        Generate placeholder metrics & equity curve.

        For now, we just create a toy PnL path and stats.
        Later, replace this with a real engine integration.
        """
        # Toy assumptions based on timeframe, date range, etc.
        # You can make this more complex later.
        total_return_pct = 12.5  # pretend 12.5% total return
        max_drawdown_pct = 8.0
        win_rate_pct = 57.0
        num_trades = 120

        initial_equity = float(run.initial_balance)
        final_equity = initial_equity * (1 + total_return_pct / 100.0)

        # Simple synthetic equity curve with 5 steps
        equity_curve = [
            {"step": 0, "equity": initial_equity},
            {"step": 1, "equity": initial_equity * 0.98},
            {"step": 2, "equity": initial_equity * 1.03},
            {"step": 3, "equity": initial_equity * 1.05},
            {"step": 4, "equity": final_equity},
        ]

        metrics = {
            "total_return_pct": total_return_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "win_rate_pct": win_rate_pct,
            "num_trades": num_trades,
            "initial_balance": float(run.initial_balance),
            "final_balance": final_equity,
        }

        return metrics, equity_curve