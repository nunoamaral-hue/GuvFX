from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from typing import Optional, Dict, List, Any
from collections import defaultdict
from decimal import Decimal
from django.db.models import Q
from django.http import Http404
from django.utils import timezone
import logging
import os
import re
import urllib.parse
import urllib.request
import json

from trading.models import TradingAccount, Trade
from strategies.models import Strategy
from execution.models import ExecutionJob

logger = logging.getLogger(__name__)

# Pattern to match demo job attribution: GUVFX_DEMO_JOB:<job_id>
DEMO_JOB_PATTERN = re.compile(r"GUVFX_DEMO_JOB:(\d+)")


def _get_windows_agent_config() -> tuple[str, str]:
    """Get Windows Agent base URL and token from environment."""
    base = (os.getenv("WINDOWS_AGENT_BASE") or os.getenv("GUVFX_AGENT_URL") or "").rstrip("/")
    token = (os.getenv("WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_AGENT_TOKEN") or "").strip()
    return base, token


def _fetch_mt5_account_balance(windows_username: str) -> Optional[dict]:
    """
    Fetch MT5 account info (balance, equity, currency) from Windows Agent.
    Returns dict with: balance, equity, currency, margin, free_margin, etc.
    Returns None on error.
    """
    base, token = _get_windows_agent_config()
    if not base or not token:
        logger.warning("Windows agent not configured, cannot fetch MT5 balance")
        return None

    try:
        url = f"{base}/mt5/snapshots/account?username={urllib.parse.quote(windows_username)}"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"X-GuvFX-Agent-Token": token}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8", "ignore")
            data = json.loads(raw) if raw else {}
            return data
    except Exception as e:
        logger.warning(f"Failed to fetch MT5 account balance: {e}")
        return None

# Pattern to extract strategy_id from guvfx comment
STRATEGY_ID_PATTERN = re.compile(r"guvfx:(?:sid|strategy_id)=(\d+)")


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


def _get_pairing_key(trade: Trade) -> str:
    """
    Get the pairing key for a trade.

    Priority:
    1. If comment contains GUVFX_DEMO_JOB:<id>, pair by exact comment (same job)
    2. If comment contains guvfx:strategy_id=<id>, pair by strategy_id
    3. Fallback: pair by (symbol, volume)
    """
    comment = trade.comment or ""

    # Priority 1: Demo job - pair by exact comment
    demo_match = DEMO_JOB_PATTERN.search(comment)
    if demo_match:
        return f"demo:{demo_match.group(0)}"

    # Priority 2: Strategy ID - pair by strategy
    strategy_match = STRATEGY_ID_PATTERN.search(comment)
    if strategy_match:
        return f"strategy:{strategy_match.group(1)}|{trade.symbol}|{trade.volume}"

    # Priority 3: Fallback - pair by symbol and volume (FIFO)
    return f"fifo:{trade.symbol}|{trade.volume}"


def _build_round_trips(
    trades: List[Trade],
    raw_labels: Dict[str, str],
    job_to_strategy: Dict[int, tuple],
    sid_to_name: Dict[int, str],
) -> List[Dict[str, Any]]:
    """
    Build round-trip rows from a list of trades (sorted by open_time ascending).

    Pairing rules:
    - For demo jobs: BUY and SELL with same GUVFX_DEMO_JOB:<id> form a pair
    - For strategy trades: BUY and SELL with same (strategy_id, symbol, volume) FIFO
    - Fallback: BUY and SELL with same (symbol, volume) FIFO

    Returns list of round-trip dicts with all required fields.
    """
    # FIFO queues per pairing key: key -> list of BUY trades waiting for SELL
    buy_queues: Dict[str, List[Trade]] = defaultdict(list)

    # Completed round-trips
    round_trips: List[Dict[str, Any]] = []

    # Sort trades by open_time ascending for FIFO pairing
    sorted_trades = sorted(trades, key=lambda t: t.open_time)

    for trade in sorted_trades:
        key = _get_pairing_key(trade)
        side = trade.side.upper() if trade.side else "BUY"

        if side == "BUY":
            # Push BUY into queue for this key
            buy_queues[key].append(trade)
        elif side == "SELL":
            # Try to pop earliest BUY from same key
            if buy_queues[key]:
                buy_trade = buy_queues[key].pop(0)

                # Build round-trip row
                rt = _build_round_trip_row(
                    buy_trade=buy_trade,
                    sell_trade=trade,
                    raw_labels=raw_labels,
                    job_to_strategy=job_to_strategy,
                    sid_to_name=sid_to_name,
                )
                round_trips.append(rt)
            # else: orphan SELL with no matching BUY - skip silently

    # Sort round-trips by close_time descending (most recent first)
    round_trips.sort(key=lambda r: r["close_time"] or "", reverse=True)

    return round_trips


def _build_round_trip_row(
    buy_trade: Trade,
    sell_trade: Trade,
    raw_labels: Dict[str, str],
    job_to_strategy: Dict[int, tuple],
    sid_to_name: Dict[int, str],
) -> Dict[str, Any]:
    """
    Build a single round-trip row from a BUY and SELL trade pair.
    """
    # Resolve strategy name (prefer BUY's, fallback to SELL's)
    def resolve_strategy(trade: Trade) -> str:
        raw = raw_labels.get(trade.ticket, "Unattributed")
        if raw.startswith("job:"):
            try:
                job_id = int(raw[4:])
            except ValueError:
                return "Unattributed"
            if job_id in job_to_strategy:
                _, strategy_name = job_to_strategy[job_id]
                return strategy_name if strategy_name else "Unattributed"
            return "Unattributed"
        else:
            sid = _sid_int(raw)
            return sid_to_name.get(sid, raw) if sid is not None else raw

    buy_strategy = resolve_strategy(buy_trade)
    sell_strategy = resolve_strategy(sell_trade)

    # Prefer BUY's strategy if available, else SELL's
    strategy_name = buy_strategy if buy_strategy != "Unattributed" else sell_strategy

    # Calculate net P&L: sum of both legs' profit + commission + swap
    buy_pnl = (buy_trade.profit or Decimal("0")) + (buy_trade.commission or Decimal("0")) + (buy_trade.swap or Decimal("0"))
    sell_pnl = (sell_trade.profit or Decimal("0")) + (sell_trade.commission or Decimal("0")) + (sell_trade.swap or Decimal("0"))
    net_pnl = buy_pnl + sell_pnl

    # Use BUY's comment if available, else SELL's
    comment = buy_trade.comment or sell_trade.comment or ""

    # Format close time for display (Trade Closed column)
    close_time = sell_trade.close_time or sell_trade.open_time
    trade_closed = close_time.isoformat() if close_time else None

    # Format trade numbers: "BUY_TICKET → SELL_TICKET"
    trade_numbers = f"{buy_trade.ticket} → {sell_trade.ticket}"

    # Direction: always "BUY→SELL" for a round-trip (long position closed)
    direction = "BUY"

    return {
        "open_time": buy_trade.open_time,
        "close_time": close_time,
        "symbol": buy_trade.symbol,
        "volume": str(buy_trade.volume),
        "open_price": str(buy_trade.open_price) if buy_trade.open_price is not None else None,
        "close_price": str(sell_trade.close_price or sell_trade.open_price) if (sell_trade.close_price or sell_trade.open_price) else None,
        "net_pnl": str(net_pnl),
        "net_pnl_money": float(net_pnl),  # Numeric for formatting with currency
        "legs": [buy_trade.ticket, sell_trade.ticket],
        "buy_ticket": buy_trade.ticket,
        "sell_ticket": sell_trade.ticket,
        "comment": comment,
        "strategy_name": strategy_name,
        # New UI-friendly fields
        "trade_closed": trade_closed,
        "trade_numbers": trade_numbers,
        "direction": direction,
        # Include breakdown for debugging
        "buy_profit": str(buy_trade.profit or Decimal("0")),
        "sell_profit": str(sell_trade.profit or Decimal("0")),
        "total_commission": str((buy_trade.commission or Decimal("0")) + (sell_trade.commission or Decimal("0"))),
        "total_swap": str((buy_trade.swap or Decimal("0")) + (sell_trade.swap or Decimal("0"))),
    }


def _compute_balance_series(
    round_trips: List[Dict[str, Any]],
    mt5_balance: Optional[float] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Compute cumulative balance series from round-trips (oldest to newest).
    Also computes observed statistics.

    Args:
        round_trips: List of round-trip dicts (sorted by close_time DESC from _build_round_trips)
        mt5_balance: Current MT5 balance (optional). If provided, we work backwards.

    Returns:
        (balance_series, observed_stats)
        - balance_series: List of {index, close_time, net_pnl, cumulative_balance}
        - observed_stats: Dict with win_rate, longest_loss_streak, max_drawdown_pct, net_pnl_total
    """
    if not round_trips:
        return [], {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "longest_loss_streak": 0,
            "max_drawdown_pct": 0.0,
            "net_pnl_total": 0.0,
        }

    # Round-trips come sorted by close_time DESC, reverse for chronological order
    sorted_rt = list(reversed(round_trips))

    # Calculate total PnL from all trades
    total_pnl = sum(float(rt.get("net_pnl_money", 0) or 0) for rt in sorted_rt)

    # If we have MT5 balance, calculate opening balance; otherwise assume 10000
    if mt5_balance is not None:
        opening_balance = mt5_balance - total_pnl
    else:
        opening_balance = 10000.0

    # Build balance series and compute stats
    balance_series = []
    balance = opening_balance
    peak = balance
    max_drawdown_pct = 0.0
    wins = 0
    losses = 0
    current_loss_streak = 0
    longest_loss_streak = 0

    for i, rt in enumerate(sorted_rt):
        pnl = float(rt.get("net_pnl_money", 0) or 0)
        balance += pnl

        # Track wins/losses
        if pnl >= 0:
            wins += 1
            current_loss_streak = 0
        else:
            losses += 1
            current_loss_streak += 1
            longest_loss_streak = max(longest_loss_streak, current_loss_streak)

        # Track drawdown
        if balance > peak:
            peak = balance
        if peak > 0:
            dd = (peak - balance) / peak * 100
            max_drawdown_pct = max(max_drawdown_pct, dd)

        balance_series.append({
            "index": i,
            "close_time": rt.get("close_time"),
            "net_pnl": pnl,
            "cumulative_balance": round(balance, 2),
        })

    total_trades = len(sorted_rt)
    win_rate_pct = (wins / total_trades * 100) if total_trades > 0 else 0.0

    observed_stats = {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate_pct, 2),
        "longest_loss_streak": longest_loss_streak,
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "net_pnl_total": round(total_pnl, 2),
        "opening_balance": round(opening_balance, 2),
        "closing_balance": round(balance, 2),
    }

    return balance_series, observed_stats


class TradeHistoryView(APIView):
    """
    GET /api/analytics/trade-history/?account=<id>&mode=<roundtrip|deals>&from=<iso>&to=<iso>&strategy=<name>&symbol=<sym>

    Returns trade history from DB (trading.Trade).

    Query params:
    - account (or account_id): Required. The trading account ID.
    - mode: Optional. "roundtrip" (default) or "deals".
        - roundtrip: Returns completed round-trips (BUY+SELL paired)
        - deals: Returns individual deal rows (legacy behavior)
    - from/to: Optional date filters
    - symbol: Optional symbol filter
    - strategy: Optional strategy name filter

    Trade attribution is resolved from multiple sources:
    1. GUVFX_DEMO_JOB:<job_id> pattern -> lookup ExecutionJob.strategy
    2. guvfx:sid=<id> or guvfx:strategy_id=<id> -> lookup Strategy by id/magic_number
    3. name=<strategy_name> -> use directly
    4. Otherwise -> "Unattributed"

    Response includes:
    - trades: List of round-trip or deal rows
    - mt5_balance: Current MT5 balance (if available)
    - currency: Account currency (e.g., "USD")
    - balance_series: Cumulative balance trajectory
    - observed_stats: Computed statistics (win_rate, drawdown, etc.)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        # Accept both "account" and "account_id" for backwards compatibility
        account_id = request.query_params.get("account") or request.query_params.get("account_id")
        if not account_id:
            return Response({"detail": "account is required"}, status=400)

        # Mode: roundtrip (default) or deals
        mode = request.query_params.get("mode", "roundtrip").lower()
        if mode not in ("roundtrip", "deals"):
            mode = "roundtrip"

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
        # Mode: Round-trip (default) - pair BUY+SELL into single rows
        # -------------------------------------------------------------------------
        if mode == "roundtrip":
            round_trips = _build_round_trips(
                trades=trades,
                raw_labels=raw_labels,
                job_to_strategy=job_to_strategy,
                sid_to_name=sid_to_name,
            )

            # Apply strategy filter if provided
            if strategy:
                round_trips = [rt for rt in round_trips if rt.get("strategy_name") == strategy]

            # -------------------------------------------------------------------------
            # Fetch MT5 balance from Windows Agent (if account has mt5_instance)
            # -------------------------------------------------------------------------
            mt5_balance = None
            currency = "USD"  # Default currency
            mt5_equity = None

            try:
                account_obj = TradingAccount.objects.select_related("mt5_instance").get(id=account_id)
                if account_obj.mt5_instance and hasattr(account_obj.mt5_instance, "windows_username"):
                    windows_username = getattr(account_obj.mt5_instance, "windows_username", "")
                    if windows_username:
                        account_info = _fetch_mt5_account_balance(windows_username)
                        if account_info:
                            mt5_balance = account_info.get("balance")
                            mt5_equity = account_info.get("equity")
                            currency = account_info.get("currency", "USD") or "USD"
            except TradingAccount.DoesNotExist:
                pass
            except Exception as e:
                logger.warning(f"Error fetching MT5 balance for account {account_id}: {e}")

            # -------------------------------------------------------------------------
            # Compute balance series and observed statistics
            # -------------------------------------------------------------------------
            balance_series, observed_stats = _compute_balance_series(
                round_trips=round_trips,
                mt5_balance=mt5_balance,
            )

            return Response({
                "account_id": int(account_id),
                "mode": "roundtrip",
                "count": len(round_trips),
                "trades": round_trips,
                "mt5_balance": mt5_balance,
                "mt5_equity": mt5_equity,
                "currency": currency,
                "balance_series": balance_series,
                "observed_stats": observed_stats,
            })

        # -------------------------------------------------------------------------
        # Mode: Deals (legacy) - return individual deal rows
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

        return Response({
            "account_id": int(account_id),
            "mode": "deals",
            "count": len(rows),
            "trades": rows,
        })


class StrategyMetricsView(APIView):
    """
    GET /api/analytics/strategy-metrics/?account=<id>

    Aggregates DB trade history by strategy_name (derived from comment).
    Supports both standard attribution (guvfx:sid) and demo job attribution (GUVFX_DEMO_JOB).

    Accepts both "account" and "account_id" query params for backwards compatibility.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        # Accept both "account" and "account_id" for backwards compatibility
        account_id = request.query_params.get("account") or request.query_params.get("account_id")
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
