from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from typing import Optional
from django.db.models import Q
from django.http import Http404
import re

from trading.models import TradingAccount, Trade
from strategies.models import Strategy
from execution.models import ExecutionJob

# Pattern to match demo job attribution: GUVFX_DEMO_JOB:<job_id>
DEMO_JOB_PATTERN = re.compile(r"GUVFX_DEMO_JOB:(\d+)")


def _extract_demo_job_id(comment: str) -> Optional[int]:
    """
    Extract ExecutionJob ID from comment like 'GUVFX_DEMO_JOB:123'.
    Returns the job_id as int, or None if pattern not found.
    """
    if not comment:
        return None
    match = DEMO_JOB_PATTERN.search(comment)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def _strategy_name_from_comment(comment: str) -> str:
    """
    Extract a strategy label from trade comment.
    Convention (recommended):
      guvfx:strategy_id=<id>;name=<strategy_name>
    If not present, return "Unattributed".

    Note: GUVFX_DEMO_JOB:<job_id> is handled separately via _extract_demo_job_id().
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

    Trade attribution is resolved from multiple sources:
    1. GUVFX_DEMO_JOB:<job_id> pattern -> lookup ExecutionJob.strategy
    2. guvfx:sid=<id> or guvfx:strategy_id=<id> -> lookup Strategy by id/magic_number
    3. name=<strategy_name> -> use directly
    4. Otherwise -> "Unattributed"
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

        # -------------------------------------------------------------------------
        # Pass 1: Extract raw labels and collect IDs for bulk lookups
        # -------------------------------------------------------------------------
        raw_labels: dict[str, str] = {}
        sids: set[int] = set()
        demo_job_ids: set[int] = set()

        for t in trades:
            comment = t.comment or ""
            # Check for demo job pattern first
            job_id = _extract_demo_job_id(comment)
            if job_id is not None:
                demo_job_ids.add(job_id)
                raw_labels[t.ticket] = f"job:{job_id}"
            else:
                raw = _strategy_name_from_comment(comment)
                raw_labels[t.ticket] = raw
                sid = _sid_int(raw)
                if sid is not None:
                    sids.add(sid)

        # -------------------------------------------------------------------------
        # Pass 2: Bulk fetch ExecutionJobs for demo trades
        # -------------------------------------------------------------------------
        job_to_strategy: dict[int, tuple[Optional[int], Optional[str]]] = {}
        if demo_job_ids:
            jobs_qs = ExecutionJob.objects.select_related("strategy").filter(id__in=demo_job_ids)
            for job in jobs_qs:
                if job.strategy_id:
                    job_to_strategy[job.id] = (job.strategy_id, job.strategy.name if job.strategy else None)
                else:
                    job_to_strategy[job.id] = (None, None)

        # -------------------------------------------------------------------------
        # Pass 3: Bulk fetch Strategies by id/magic_number
        # -------------------------------------------------------------------------
        sid_to_name: dict[int, str] = {}
        if sids:
            strat_qs = Strategy.objects.filter(Q(id__in=sids) | Q(magic_number__in=sids))
            if not user.is_staff:
                strat_qs = strat_qs.filter(owner=user)

            # Map BOTH id and magic_number to the same name (magic is optional)
            for s in strat_qs:
                sid_to_name[s.id] = s.name
                if s.magic_number is not None:
                    sid_to_name[int(s.magic_number)] = s.name

        # -------------------------------------------------------------------------
        # Pass 4: Build response rows with resolved strategy names
        # -------------------------------------------------------------------------
        rows = []
        for t in trades:
            raw = raw_labels.get(t.ticket, "Unattributed")

            # Resolve strategy name
            if raw.startswith("job:"):
                # Demo trade: lookup via ExecutionJob
                try:
                    job_id = int(raw[4:])
                except ValueError:
                    job_id = None
                if job_id and job_id in job_to_strategy:
                    strategy_id, strategy_name = job_to_strategy[job_id]
                    strat = strategy_name if strategy_name else "Unattributed"
                else:
                    strat = "Unattributed"
            else:
                # Standard attribution: sid lookup or direct name
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
    Supports both standard attribution (guvfx:sid) and demo job attribution (GUVFX_DEMO_JOB).
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

        # -------------------------------------------------------------------------
        # Pass 1: Extract raw labels and collect IDs for bulk lookups
        # -------------------------------------------------------------------------
        raw_labels: dict[str, str] = {}
        sids: set[int] = set()
        demo_job_ids: set[int] = set()

        for t in trades_list:
            comment = t.comment or ""
            job_id = _extract_demo_job_id(comment)
            if job_id is not None:
                demo_job_ids.add(job_id)
                raw_labels[t.ticket] = f"job:{job_id}"
            else:
                raw = _strategy_name_from_comment(comment)
                raw_labels[t.ticket] = raw
                sid = _sid_int(raw)
                if sid is not None:
                    sids.add(sid)

        # -------------------------------------------------------------------------
        # Pass 2: Bulk fetch ExecutionJobs for demo trades
        # -------------------------------------------------------------------------
        job_to_strategy: dict[int, tuple[Optional[int], Optional[str]]] = {}
        if demo_job_ids:
            jobs_qs = ExecutionJob.objects.select_related("strategy").filter(id__in=demo_job_ids)
            for job in jobs_qs:
                if job.strategy_id:
                    job_to_strategy[job.id] = (job.strategy_id, job.strategy.name if job.strategy else None)
                else:
                    job_to_strategy[job.id] = (None, None)

        # -------------------------------------------------------------------------
        # Pass 3: Bulk fetch Strategies by id/magic_number
        # -------------------------------------------------------------------------
        sid_to_name: dict[int, str] = {}
        if sids:
            strat_qs = Strategy.objects.filter(Q(id__in=sids) | Q(magic_number__in=sids))
            if not user.is_staff:
                strat_qs = strat_qs.filter(owner=user)

            # Map BOTH id and magic_number to the same name (magic is optional)
            for s in strat_qs:
                sid_to_name[s.id] = s.name
                if s.magic_number is not None:
                    sid_to_name[int(s.magic_number)] = s.name

        # -------------------------------------------------------------------------
        # Pass 4: Aggregate by resolved strategy name
        # -------------------------------------------------------------------------
        bucket = {}
        for t in trades_list:
            raw = raw_labels.get(t.ticket, "Unattributed")

            # Resolve strategy name
            if raw.startswith("job:"):
                try:
                    job_id = int(raw[4:])
                except ValueError:
                    job_id = None
                if job_id and job_id in job_to_strategy:
                    _, strategy_name = job_to_strategy[job_id]
                    name = strategy_name if strategy_name else "Unattributed"
                else:
                    name = "Unattributed"
            else:
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


class StrategyHasTradesView(APIView):
    """
    GET /api/analytics/strategy-has-trades/?strategy=<id>

    Returns whether a strategy already has attributed trades in the DB.

    This is used by the frontend to lock magic_number once trades exist.

    Response shape:
      {
        "strategy_id": 1,
        "strategy_name": "MVP Sync Strategy",
        "magic_number": 12345,
        "canonical_id": 12345,
        "has_trades": true,
        "trade_count": 12
      }

    Notes:
    - We treat BOTH Strategy.id and Strategy.magic_number as potential attribution IDs,
      because older trades may have been tagged with id before magic_number was set.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        strategy_id = request.query_params.get("strategy")
        if not strategy_id:
            return Response({"detail": "strategy is required"}, status=400)

        try:
            sid = int(strategy_id)
        except Exception:
            return Response({"detail": "strategy must be an integer"}, status=400)

        qs = Strategy.objects.filter(id=sid)
        if not user.is_staff:
            qs = qs.filter(owner=user)

        strategy = qs.first()
        if not strategy:
            raise Http404("strategy not found")

        ids: set[int] = {int(strategy.id)}
        if strategy.magic_number is not None:
            try:
                ids.add(int(strategy.magic_number))
            except Exception:
                # ignore invalid magic_number shapes
                pass

        # Count attributed trades by magic_number matching either strategy.id or strategy.magic_number
        trade_count = Trade.objects.filter(magic_number__in=list(ids)).count()

        canonical_id = int(strategy.magic_number) if strategy.magic_number is not None else int(strategy.id)

        return Response({
            "strategy_id": int(strategy.id),
            "strategy_name": strategy.name,
            "magic_number": int(strategy.magic_number) if strategy.magic_number is not None else None,
            "canonical_id": canonical_id,
            "has_trades": trade_count > 0,
            "trade_count": int(trade_count),
        })
