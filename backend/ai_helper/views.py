from typing import Any, Dict, List, Optional

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from backtests.models import BacktestRun
from strategies.models import Strategy
from .serializers import (
    StrategyInsightsRequestSerializer,
    StrategyInsightsResponseSerializer,
)


class StrategyInsightsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        req_serializer = StrategyInsightsRequestSerializer(data=request.data)
        req_serializer.is_valid(raise_exception=True)
        strategy_id = req_serializer.validated_data["strategy_id"]
        max_runs = req_serializer.validated_data["max_runs"]

        try:
            strategy = Strategy.objects.get(id=strategy_id, owner=request.user)
        except Strategy.DoesNotExist:
            return Response(
                {"detail": "Strategy not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        runs_qs = (
            BacktestRun.objects.filter(
                config__strategy_id=strategy.id,
                config__owner=request.user,
                status=BacktestRun.STATUS_COMPLETED,
            )
            .exclude(metrics__isnull=True)
            .order_by("-started_at", "-id")[:max_runs]
        )
        runs = list(runs_qs)
        metrics_list = [run.metrics or {} for run in runs]

        total_trades = 0
        for metrics in metrics_list:
            trades = self._coerce_float(metrics.get("total_trades"))
            if trades is not None:
                total_trades += int(trades)

        win_rates: List[float] = []
        for metrics in metrics_list:
            win_rate = self._coerce_float(metrics.get("win_rate_pct"))
            if win_rate is not None:
                win_rates.append(win_rate)
        avg_win_rate = sum(win_rates) / len(win_rates) if win_rates else None

        max_dd_values: List[float] = []
        for metrics in metrics_list:
            dd = self._coerce_float(metrics.get("max_drawdown"))
            if dd is not None:
                max_dd_values.append(dd)
        worst_dd = min(max_dd_values) if max_dd_values else None

        recs: List[str] = []
        if avg_win_rate is not None:
            if avg_win_rate > 55:
                recs.append(
                    "Win rate is solid; consider cautiously increasing size on your best-performing symbol/timeframe."
                )
            elif avg_win_rate < 45:
                recs.append(
                    "Win rate is low; focus on filtering out low-quality setups or tightening stop-loss logic."
                )

        strategy_risk = (
            float(strategy.risk_per_trade_pct)
            if strategy.risk_per_trade_pct is not None
            else None
        )

        if worst_dd is not None and strategy_risk is not None:
            if worst_dd < -20 and strategy_risk > 1.0:
                recs.append(
                    "Drawdown is relatively large compared to your risk per trade. Consider reducing risk or adding stricter daily/weekly loss limits."
                )
            elif worst_dd > -10 and strategy_risk <= 1.0:
                recs.append(
                    "Drawdown has been relatively contained; you might have room to very cautiously explore higher risk, with strict safeguards."
                )

        if total_trades < 50:
            recs.append(
                "Sample size is small. Treat any conclusions as provisional until you have at least 100 trades."
            )

        summary_parts: List[str] = []
        if avg_win_rate is not None:
            summary_parts.append(f"Avg win rate: {avg_win_rate:.1f}%.")
        if worst_dd is not None:
            summary_parts.append(f"Worst drawdown: {worst_dd:.2f}.")
        if total_trades:
            summary_parts.append(
                f"Total trades across recent runs: {total_trades}."
            )

        summary = " ".join(summary_parts) or "Not enough data to form a summary yet."

        risk_assessment = ""
        if worst_dd is not None:
            risk_assessment = (
                "Risk appears elevated relative to historical drawdown."
                if worst_dd < -20
                else "Risk appears moderate relative to historical drawdown."
            )

        resp = {
            "strategy_id": strategy.id,
            "summary": summary,
            "recommendations": recs,
            "risk_assessment": risk_assessment,
            "notes": "",
        }

        out_serializer = StrategyInsightsResponseSerializer(resp)
        return Response(out_serializer.data, status=status.HTTP_200_OK)

    def _coerce_float(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
