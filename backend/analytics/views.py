from django.db.models import Sum, Count, F, Value, DecimalField
from django.db.models.functions import Coalesce
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from trading.models import TradingAccount, Trade
from strategies.models import Strategy
from backtests.models import BacktestConfig, BacktestRun
from .serializers import (
    AccountPerformanceSerializer,
    StrategyBacktestSummarySerializer,
)


class AccountPerformanceView(APIView):
    """
    GET /api/analytics/account-performance/

    Returns PnL summary per trading account for the authenticated user.
    Staff users see all accounts.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user

        qs = (
            TradingAccount.objects
            .select_related("user")
            .all()
        )
        if not user.is_staff:
            qs = qs.filter(user=user)

        # Join trades and aggregate
        qs = (
            qs
            .annotate(
                num_trades=Count("trades"),
                total_profit=Coalesce(
                    Sum("trades__profit"),
                    Value(0, output_field=DecimalField(max_digits=20, decimal_places=2)),
                ),
                total_commission=Coalesce(
                    Sum("trades__commission"),
                    Value(0, output_field=DecimalField(max_digits=20, decimal_places=2)),
                ),
                total_swap=Coalesce(
                    Sum("trades__swap"),
                    Value(0, output_field=DecimalField(max_digits=20, decimal_places=2)),
                ),
            )
            .annotate(
                # all three are now DecimalFields, so this is safe
                net_pnl=F("total_profit") + F("total_commission") + F("total_swap")
            )
        )

        data = [
            {
                "account_id": acc.id,
                "account_name": acc.name,
                "broker_name": acc.broker_name,
                "num_trades": acc.num_trades or 0,
                "total_profit": acc.total_profit,
                "total_commission": acc.total_commission,
                "total_swap": acc.total_swap,
                "net_pnl": acc.net_pnl,
            }
            for acc in qs
        ]

        serializer = AccountPerformanceSerializer(data, many=True)
        return Response(serializer.data)


class StrategyBacktestSummaryView(APIView):
    """
    GET /api/analytics/strategy-backtests/

    Returns summary per backtest config (per strategy) for the authenticated user:
    - num_runs
    - last run status, metrics, created_at
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user

        configs = (
            BacktestConfig.objects
            .select_related("owner", "strategy")
            .all()
        )
        if not user.is_staff:
            configs = configs.filter(owner=user)

        summaries = []

        for config in configs:
            runs = (
                config.runs
                .order_by("-created_at")
            )
            num_runs = runs.count()
            last_run = runs.first()

            summaries.append(
                {
                    "config_id": config.id,
                    "config_name": config.name,
                    "strategy_id": config.strategy.id,
                    "strategy_name": config.strategy.name,
                    "num_runs": num_runs,
                    "last_status": last_run.status if last_run else None,
                    "last_run_created_at": last_run.created_at if last_run else None,
                    "last_metrics": last_run.metrics if last_run else None,
                }
            )

        serializer = StrategyBacktestSummarySerializer(summaries, many=True)
        return Response(serializer.data)