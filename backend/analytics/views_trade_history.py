from django.db.models import Sum, Count, F, Value, DecimalField
from django.db.models.functions import Coalesce
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from trading.models import TradingAccount, Trade


def _strategy_name_from_comment(comment: str) -> str:
    """
    Extract a strategy label from trade comment.
    Convention (recommended):
      guvfx:strategy_id=<id>;name=<strategy_name>
    If not present, return "Unattributed".
    """
    if not comment:
        return "Unattributed"
    c = comment.strip()
    if "guvfx:" not in c:
        return "Unattributed"
    # Very lightweight parse (Phase A2.1)
    # Look for "name="
    idx = c.find("name=")
    if idx >= 0:
        tail = c[idx + len("name="):]
        # stop at ; if exists
        end = tail.find(";")
        name = tail[:end] if end >= 0 else tail
        name = name.strip()
        return name if name else "Unattributed"
    return "Unattributed"


class TradeHistoryView(APIView):
    """
    GET /api/analytics/trade-history/?account=<id>&from=<iso>&to=<iso>&strategy=<name>&symbol=<sym>

    Returns closed trades from DB (trading.Trade).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        account_id = request.query_params.get("account")
        if not account_id:
            return Response({"detail": "account is required"}, status=400)

        qs = Trade.objects.select_related("account").filter(account_id=account_id)

        # Ownership gate
        if not user.is_staff:
            qs = qs.filter(account__user=user)

        # Optional filters
        symbol = request.query_params.get("symbol")
        if symbol:
            qs = qs.filter(symbol=symbol)

        dt_from = request.query_params.get("from")
        if dt_from:
            qs = qs.filter(close_time__gte=dt_from)

        dt_to = request.query_params.get("to")
        if dt_to:
            qs = qs.filter(close_time__lte=dt_to)

        # Strategy filter (derived from comment)
        strategy = request.query_params.get("strategy")
        rows = []
        for t in qs.order_by("-close_time")[:2000]:
            strat = _strategy_name_from_comment(t.comment or "")
            if strategy and strat != strategy:
                continue
            rows.append({
                "ticket": t.ticket,
                "symbol": t.symbol,
                "side": t.side,
                "volume": str(t.volume),
                "open_time": t.open_time,
                "close_time": t.close_time,
                "open_price": str(t.open_price),
                "close_price": str(t.close_price) if t.close_price is not None else None,
                "profit": str(t.profit),
                "commission": str(t.commission),
                "swap": str(t.swap),
                "net_pnl": str((t.profit or 0) + (t.commission or 0) + (t.swap or 0)),
                "magic_number": t.magic_number,
                "comment": t.comment,
                "strategy_name": strat,
            })

        return Response({"account_id": int(account_id), "count": len(rows), "trades": rows})


class StrategyMetricsView(APIView):
    """
    GET /api/analytics/strategy-metrics/?account=<id>

    Aggregates DB trade history by strategy_name (derived from comment).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        account_id = request.query_params.get("account")
        if not account_id:
            return Response({"detail": "account is required"}, status=400)

        # Ownership gate
        acc_qs = TradingAccount.objects.filter(id=account_id)
        if not user.is_staff:
            acc_qs = acc_qs.filter(user=user)
        if not acc_qs.exists():
            return Response({"detail": "account not found"}, status=404)

        trades = Trade.objects.filter(account_id=account_id).order_by("-close_time")

        bucket = {}
        for t in trades:
            name = _strategy_name_from_comment(t.comment or "")
            net = (t.profit or 0) + (t.commission or 0) + (t.swap or 0)
            b = bucket.setdefault(name, {"strategy_name": name, "trades": 0, "net_pnl": 0, "wins": 0, "losses": 0})
            b["trades"] += 1
            b["net_pnl"] += float(net)
            if net >= 0:
                b["wins"] += 1
            else:
                b["losses"] += 1

        out = []
        for name, b in bucket.items():
            trades_n = b["trades"]
            win_rate = (b["wins"] / trades_n * 100.0) if trades_n else 0.0
            out.append({
                **b,
                "win_rate_pct": round(win_rate, 2),
            })

        out.sort(key=lambda x: x["net_pnl"], reverse=True)
        return Response({"account_id": int(account_id), "strategies": out})
