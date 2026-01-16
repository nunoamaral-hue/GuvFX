from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from typing import Optional

from trading.models import TradingAccount, Trade
from strategies.models import Strategy


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

    def extract_value(key: str) -> str:
        idx = c.find(key)
        if idx >= 0:
            tail = c[idx + len(key):]
            end = tail.find(";")
            value = tail[:end] if end >= 0 else tail
            return value.strip()
        return ""

    sid = extract_value("guvfx:sid=")
    if sid:
        return f"sid:{sid}"
    strategy_id = extract_value("guvfx:strategy_id=")
    if strategy_id:
        return f"sid:{strategy_id}"
    name = extract_value("name=")
    if name:
        return name
    return "Unattributed"


def _sid_int(label: str) -> Optional[int]:
    """Convert 'sid:12345' -> 12345, else None."""
    if not label:
        return None
    if not label.startswith("sid:"):
        return None
    raw = label[4:].strip()
    if not raw.isdigit():
        return None
    try:
        return int(raw)
    except Exception:
        return None


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

        trades = list(qs.order_by("-close_time")[:2000])

        raw_labels: dict[str, str] = {}
        sids: set[int] = set()
        for t in trades:
            raw = _strategy_name_from_comment(t.comment or "")
            raw_labels[t.ticket] = raw
            sid = _sid_int(raw)
            if sid is not None:
                sids.add(sid)

        sid_to_name: dict[int, str] = {}
        if sids:
            strat_qs = Strategy.objects.filter(id__in=sids)
            if not user.is_staff:
                strat_qs = strat_qs.filter(owner=user)
            sid_to_name = {s.id: s.name for s in strat_qs}

        rows = []
        for t in trades:
            raw = raw_labels.get(t.ticket, "Unattributed")
            sid = _sid_int(raw)
            strat = sid_to_name.get(sid, raw) if sid is not None else raw

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

        trades_list = list(trades)

        raw_labels: dict[str, str] = {}
        sids: set[int] = set()
        for t in trades_list:
            raw = _strategy_name_from_comment(t.comment or "")
            raw_labels[t.ticket] = raw
            sid = _sid_int(raw)
            if sid is not None:
                sids.add(sid)

        sid_to_name: dict[int, str] = {}
        if sids:
            strat_qs = Strategy.objects.filter(id__in=sids)
            if not user.is_staff:
                strat_qs = strat_qs.filter(owner=user)
            sid_to_name = {s.id: s.name for s in strat_qs}

        bucket = {}
        for t in trades_list:
            raw = raw_labels.get(t.ticket, "Unattributed")
            sid = _sid_int(raw)
            name = sid_to_name.get(sid, raw) if sid is not None else raw

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
