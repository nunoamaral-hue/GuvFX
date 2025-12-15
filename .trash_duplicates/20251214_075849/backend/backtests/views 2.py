from django.utils import timezone
from rest_framework import permissions, viewsets
from rest_framework.views import APIView
from rest_framework.response import Response

from .models import BacktestConfig, BacktestRun
from .serializers import BacktestConfigSerializer, BacktestRunSerializer


class BacktestConfigViewSet(viewsets.ModelViewSet):
    """
    CRUD for backtest configurations.

    - Non-staff users see only their own configs.
    - Staff users see all configs.
    """
    serializer_class = BacktestConfigSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = (
            BacktestConfig.objects
            .select_related("owner", "strategy", "reference_account")
            .all()
        )
        if not user.is_staff:
            qs = qs.filter(owner=user)
        return qs

    def perform_create(self, serializer):
        serializer.save()  # BacktestConfigSerializer.create() sets owner


class BacktestRunViewSet(viewsets.ModelViewSet):
    """
    Manage backtest runs.

    For now:
    - Create() creates a PENDING run snapshotting config parameters.
    - Later a worker/engine will pick up PENDING runs and update status/metrics.
    """

    queryset = (
        BacktestRun.objects
        .select_related("config", "config__owner", "config__strategy")
        .all()
        .order_by("-started_at", "-id")
    )
    serializer_class = BacktestRunSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if not user.is_staff:
            qs = qs.filter(config__owner=user)
        strategy_id = self.request.query_params.get("strategy")
        if strategy_id:
            qs = qs.filter(config__strategy_id=strategy_id)
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        config = serializer.validated_data["config"]

        # Ownership check: non-staff can only create runs on their own configs
        if (not user.is_staff) and config.owner != user:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You can only run backtests on your own configs.")

        # Snapshot current config parameters into the run
        run = BacktestRun.objects.create(
            config=config,
            symbol=config.symbol,
            timeframe=config.timeframe,
            date_from=config.date_from,
            date_to=config.date_to,
            initial_balance=config.initial_balance,
            status=BacktestRun.STATUS_PENDING,
            created_at=timezone.now(),  # likely redundant, but explicit
        )

        # If you wanted a fake/dummy "instant completion" for now, you could update here.
        # For now we leave it as PENDING to be picked up by a future worker.

        # Ensure serializer instance is set for response rendering
        serializer.instance = run


class ProcessPendingBacktestsView(APIView):
    """
    POST /api/backtests/process-pending/

    Processes all PENDING BacktestRun objects using the same dummy logic
    as the management command, and returns how many runs were updated.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        pending_runs = (
            BacktestRun.objects
            .filter(status=BacktestRun.STATUS_PENDING)
            .select_related("config", "config__owner", "config__strategy")
        )

        if not pending_runs.exists():
            return Response(
                {"processed_runs": 0, "processed_at": timezone.now()},
                status=200,
            )

        processed_count = 0

        for run in pending_runs:
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

            processed_count += 1

        return Response(
            {
                "processed_runs": processed_count,
                "processed_at": timezone.now(),
            },
            status=200,
        )

    def _generate_dummy_results(self, run: BacktestRun):
        """
        Same dummy result generator as the management command.
        """
        total_return_pct = 12.5
        max_drawdown_pct = 8.0
        win_rate_pct = 57.0
        num_trades = 120

        initial_equity = float(run.initial_balance)
        final_equity = initial_equity * (1 + total_return_pct / 100.0)

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
