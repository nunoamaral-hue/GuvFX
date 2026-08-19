from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from typing import Optional, Dict, List, Any
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

# Patterns to match execution job attribution:
# - Legacy: GUVFX_DEMO_JOB:<job_id>
# - Demo: GJ<4-digit-zero-padded-job_id> (e.g., "GJ0031" for job_id=31)
# - Signal: GS<4-digit-zero-padded-job_id> (e.g., "GS0039" for signal job_id=39)
DEMO_JOB_PATTERN_LEGACY = re.compile(r"GUVFX_DEMO_JOB:(\d+)")
DEMO_JOB_PATTERN_NEW = re.compile(r"^G[JS](\d{4})$")  # Match both GJ and GS tags


def _get_windows_agent_config() -> tuple[str, str]:
    """Get Windows Agent base URL and token from environment."""
    base = (os.getenv("WINDOWS_AGENT_BASE") or os.getenv("GUVFX_AGENT_URL") or "").rstrip("/")
    token = (os.getenv("WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_AGENT_TOKEN") or "").strip()
    return base, token


def _account_windows_username(account) -> str:
    """The account's Windows tenant identity for the per-tenant MT5 balance read — hosted-aware.

    Legacy accounts carry it on ``mt5_instance``; HOSTED per-tenant accounts (the beta path) carry it on
    ``AccountProvisioning`` (server-derived; they have NO ``mt5_instance``, which is why the balance was
    previously never fetched and the chart fell back to a synthetic 10,000 opening balance). Only a fully
    PROVISIONED, non-admin customer identity is used. Empty string when none exists (the caller then skips
    the balance read — an honest "unavailable" state, never a foreign account)."""
    inst = getattr(account, "mt5_instance", None)
    wu = getattr(inst, "windows_username", "") if inst else ""
    if wu:
        return wu
    try:
        from terminal_provisioning.models import AccountProvisioning
        prov = (AccountProvisioning.objects
                .filter(trading_account_id=getattr(account, "id", None),
                        status=AccountProvisioning.Status.PROVISIONED, is_admin=False)
                .only("windows_username").first())
        return (getattr(prov, "windows_username", "") or "") if prov else ""
    except Exception:  # pragma: no cover - defensive; a resolution error must not break the page
        return ""


def _fetch_mt5_account_balance(account, windows_username: str) -> Optional[dict]:
    """
    Fetch THIS account's OWN MT5 balance/equity/currency from its per-tenant bridge.

    P0 DATA-ISOLATION: the destination is resolved from the account's OWN HostedExecutionEndpoint
    (never the module-global agent, which co-resident is a sibling tenant's bridge), and the balance is
    bound to the bridge's OBSERVED session identity — a mismatch returns None (no foreign balance is ever
    shown). Fail-closed on any resolution/identity failure.

    Handles response shapes:
      - {"ok": true, "data": {"balance": ..., "equity": ..., "currency": ...}}
      - {"ok": true, "data": {"account": {"balance": ..., ...}}}
      - {"balance": ..., "equity": ..., "currency": ...}  (direct)
    """
    from execution.snapshot_transport import resolve_account_snapshot_base, verify_snapshot_identity
    global_base, token = _get_windows_agent_config()
    if not token:
        logger.warning("Windows agent token not configured, cannot fetch MT5 balance")
        return None
    st = resolve_account_snapshot_base(account, global_base_url=global_base)
    if not st.ok:
        logger.warning("MT5 balance read transport unresolved for account %s: %s",
                       getattr(account, "id", None), st.reason_code)
        return None
    base = st.base_url

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

            # DOWNSTREAM FIREWALL: verify the observed session identity is this account's before returning
            # ANY financial figure (a mis-routed balance read that reached the wrong tenant is refused).
            _inner_id = data.get("data") if isinstance(data.get("data"), dict) else {}
            observed_login = (data.get("account_login") or _inner_id.get("account_login")
                              or _inner_id.get("login") or data.get("login"))
            observed_server = (data.get("account_server") or _inner_id.get("account_server")
                               or _inner_id.get("server") or data.get("server"))
            _idc = verify_snapshot_identity(account, observed_login, observed_server)
            if not _idc.ok:
                logger.warning("MT5 balance identity firewall refused for account %s: %s",
                               getattr(account, "id", None), _idc.reason_code)
                return None

            # Handle nested response shapes
            # Shape 1: {"ok": true, "data": {"account": {...}}}
            if isinstance(data.get("data"), dict):
                inner = data["data"]
                if isinstance(inner.get("account"), dict):
                    return inner["account"]
                # Shape 2: {"ok": true, "data": {"balance": ...}}
                if "balance" in inner:
                    return inner

            # Shape 3: direct {"balance": ..., "equity": ...}
            if "balance" in data:
                return data

            logger.warning(f"Unexpected MT5 account response shape: {list(data.keys())}")
            return None
    except Exception as e:
        logger.warning(f"Failed to fetch MT5 account balance: {e}")
        return None

# MT5 deal types on a balance-operation deal (NOT a trade): a DEPOSIT/WITHDRAWAL is DEAL_TYPE_BALANCE(2)
# with the signed cash amount in ``profit`` (deposit positive, withdrawal negative); broker bonus is
# DEAL_TYPE_CREDIT(3). These carry no ``position_id`` so ``build_positions_from_deals`` skips them — they are
# NEVER trades and never enter trading P&L. We read them ONLY to place external cash flows correctly on the
# balance chart (a deposit must not look like trading profit, nor a withdrawal like a trading loss).
DEAL_TYPE_BALANCE = 2
DEAL_TYPE_CREDIT = 3


def _fetch_mt5_balance_ops(account, windows_username: str) -> Optional[dict]:
    """Fetch THIS account's OWN external cash-flow operations (deposits/withdrawals + any credit) from its
    per-tenant bridge — for grounding the balance chart on real funding rather than assuming current balance
    equals deposited capital.

    Same isolation contract as ``_fetch_mt5_account_balance``: the destination is the account's OWN
    HostedExecutionEndpoint (never the module-global agent), and the whole batch is bound to the bridge's
    OBSERVED session identity (#378 firewall) — a mismatch returns None (no foreign funding is ever read).
    Fail-closed: any resolution/identity/parse failure returns None and the caller keeps the existing
    reconstruction (never a fabricated funding history).

    Returns ``{"balance_ops": [{"time": aware-datetime|None, "amount": float}], "credit": float}`` or None.
    """
    from execution.snapshot_transport import resolve_account_snapshot_base, verify_snapshot_identity
    from trading.position_ingest import deal_time_to_utc
    global_base, token = _get_windows_agent_config()
    if not token:
        return None
    st = resolve_account_snapshot_base(account, global_base_url=global_base)
    if not st.ok:
        logger.warning("MT5 balance-ops transport unresolved for account %s: %s",
                       getattr(account, "id", None), st.reason_code)
        return None
    base = st.base_url
    try:
        url = f"{base}/mt5/snapshots/deals?username={urllib.parse.quote(windows_username)}"
        req = urllib.request.Request(url, method="GET", headers={"X-GuvFX-Agent-Token": token})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8", "ignore")
            data = json.loads(raw) if raw else {}
    except Exception as e:
        logger.warning("Failed to fetch MT5 balance ops: %s", e)
        return None
    if not isinstance(data, dict):  # a top-level JSON array/scalar is not a valid snapshot -> fail closed
        return None

    # DOWNSTREAM FIREWALL: bind the batch to the observed session identity BEFORE reading any figure.
    _inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    observed_login = data.get("account_login", _inner.get("account_login"))
    observed_server = data.get("account_server", _inner.get("account_server"))
    _idc = verify_snapshot_identity(account, observed_login, observed_server)
    if not _idc.ok:
        logger.warning("MT5 balance-ops identity firewall refused for account %s: %s",
                       getattr(account, "id", None), _idc.reason_code)
        return None

    deals = data.get("deals") or _inner.get("deals") or []
    if not isinstance(deals, list):
        return None
    balance_ops: List[Dict[str, Any]] = []
    credit_total = 0.0
    seen_tickets: set = set()  # dedup by unique MT5 deal ticket so a paginated/overlapping repeat of the
    #                            SAME balance deal is not double-counted (would inflate net_funding).
    for d in deals:
        if not isinstance(d, dict):
            continue
        try:
            dtype = int(d.get("type")) if d.get("type") is not None else None
        except (TypeError, ValueError):
            dtype = None
        if dtype not in (DEAL_TYPE_BALANCE, DEAL_TYPE_CREDIT):
            continue
        ticket = d.get("ticket")
        if ticket is not None:
            key = str(ticket)
            if key in seen_tickets:
                continue
            seen_tickets.add(key)
        if dtype == DEAL_TYPE_BALANCE:
            t = deal_time_to_utc(d)
            if t is None:
                # ISO fallback (some bridge shapes emit ``time_utc`` rather than unix ``time``). Force an
                # aware UTC datetime — a naive value would raise TypeError when ordered against the aware
                # trade close_times, so a naive/parse-failed timestamp is dropped (op treated as untimed).
                raw_iso = d.get("time_utc")
                if isinstance(raw_iso, str) and raw_iso:
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        parsed = _dt.fromisoformat(raw_iso.replace("Z", "+00:00"))
                        t = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=_tz.utc)
                    except ValueError:
                        t = None
            try:
                amount = float(d.get("profit")) if d.get("profit") is not None else 0.0
            except (TypeError, ValueError):
                amount = 0.0
            balance_ops.append({"time": t, "amount": amount})
        elif dtype == DEAL_TYPE_CREDIT:
            try:
                credit_total += float(d.get("profit")) if d.get("profit") is not None else 0.0
            except (TypeError, ValueError):
                pass
    return {"balance_ops": balance_ops, "credit": round(credit_total, 2)}


# Pattern to extract strategy_id from guvfx comment
STRATEGY_ID_PATTERN = re.compile(r"guvfx:(?:sid|strategy_id)=(\d+)")


def _extract_demo_job_id(comment: str) -> Optional[int]:
    """
    Extract ExecutionJob ID from comment.

    Recognizes two patterns:
    - Legacy: 'GUVFX_DEMO_JOB:123' -> job_id=123
    - New: 'GJ0031' -> job_id=31 (4-digit zero-padded)

    Returns the job_id as int, or None if pattern not found.
    """
    if not comment:
        return None

    # Try new pattern first (exact match for "GJdddd")
    match = DEMO_JOB_PATTERN_NEW.match(comment.strip())
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass

    # Try legacy pattern (can appear anywhere in comment)
    match = DEMO_JOB_PATTERN_LEGACY.search(comment)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass

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

    MODE 1 (FIFO pairing with fallback):
    - Always returns a key; all trades can be paired
    - Priority order for pairing key:

    1. Demo job pattern (legacy GUVFX_DEMO_JOB:<id> or new GJdddd)
    2. Strategy ID pattern (guvfx:strategy_id=<id> or guvfx:sid=<id>)
    3. FIFO fallback by symbol+volume
    """
    comment = trade.comment or ""

    # Priority 1: Demo job - pair by job_id (both legacy and new patterns)
    job_id = _extract_demo_job_id(comment)
    if job_id is not None:
        return f"demo:job:{job_id}"

    # Priority 2: Strategy ID - pair by strategy
    strategy_match = STRATEGY_ID_PATTERN.search(comment)
    if strategy_match:
        return f"strategy:{strategy_match.group(1)}|{trade.symbol}|{trade.volume}"

    # Priority 3: FIFO fallback by symbol and volume
    return f"fifo:{trade.symbol}|{trade.volume}"


def _build_round_trips(
    trades: List[Trade],
    raw_labels: Dict[str, str],
    job_to_strategy: Dict[int, tuple],
    sid_to_name: Dict[int, str],
) -> List[Dict[str, Any]]:
    """
    Build round-trip rows from a list of trades.

    Each ``Trade`` row is a COMPLETE POSITION — the ingest (``build_positions_from_deals``) stores one row
    per MT5 position, matched by ``position_id``, carrying the authoritative open AND close price/profit.
    A CLOSED position (``close_time`` set) therefore already IS a round-trip and is emitted directly.

    (Historical note — the P0 read-model defect this replaces: the previous FIFO BUY↔SELL "pairing" treated
    each position as a single leg and silently dropped every unpaired leg. A long-only account — e.g. a
    Wayond BUY-only tenant — has NO SELL legs, so all of its positions were dropped and Trade History /
    Dashboard rendered empty despite correct durable data. Pairing two unrelated positions was also wrong
    for mixed-side accounts. Emitting each closed position directly is the correct, generic behaviour.)

    Still-open positions (no ``close_time``) are not completed round-trips and are omitted (this view is
    "observed CLOSED trades"); their close fills in on a later sync.
    """
    round_trips: List[Dict[str, Any]] = [
        _position_round_trip_row(trade, raw_labels, job_to_strategy, sid_to_name)
        for trade in trades
        if trade.close_time is not None
    ]
    # Most recent first.
    round_trips.sort(key=lambda r: r["close_time"] or "", reverse=True)
    return round_trips


def _position_round_trip_row(
    trade: Trade,
    raw_labels: Dict[str, str],
    job_to_strategy: Dict[int, tuple],
    sid_to_name: Dict[int, str],
) -> Dict[str, Any]:
    """Build ONE round-trip row from a single COMPLETE POSITION ``Trade`` (open + close + profit in one row).

    P&L is counted ONCE (the position's own profit/commission/swap) — never doubled — so aggregate net P&L
    equals the sum of position profits. Row shape is byte-compatible with the legacy paired row so the
    frontend + ``_compute_balance_series`` are unchanged."""
    raw = raw_labels.get(trade.ticket, "Unattributed")
    if raw.startswith("job:"):
        try:
            job_id = int(raw[4:])
        except ValueError:
            job_id = None
        strategy_name = "Unattributed"
        if job_id is not None and job_id in job_to_strategy:
            _, sname = job_to_strategy[job_id]
            strategy_name = sname if sname else "Unattributed"
    else:
        sid = _sid_int(raw)
        strategy_name = sid_to_name.get(sid, raw) if sid is not None else raw

    net_pnl = ((trade.profit or Decimal("0"))
               + (trade.commission or Decimal("0"))
               + (trade.swap or Decimal("0")))
    close_time = trade.close_time or trade.open_time
    direction = (trade.side or "BUY").upper()
    return {
        "open_time": trade.open_time,
        "close_time": close_time,
        "symbol": trade.symbol,
        "volume": str(trade.volume),
        "open_price": str(trade.open_price) if trade.open_price is not None else None,
        "close_price": str(trade.close_price) if trade.close_price is not None else (
            str(trade.open_price) if trade.open_price is not None else None),
        "net_pnl": str(net_pnl),
        "net_pnl_money": float(net_pnl),
        "legs": [trade.ticket],
        "buy_ticket": trade.ticket,
        "sell_ticket": trade.ticket,
        "comment": trade.comment or "",
        "strategy_name": strategy_name,
        "trade_closed": close_time.isoformat() if close_time else None,
        "trade_numbers": str(trade.ticket),
        "direction": direction,
        "buy_profit": str(trade.profit or Decimal("0")) if direction == "BUY" else "0",
        "sell_profit": str(trade.profit or Decimal("0")) if direction == "SELL" else "0",
        "total_commission": str(trade.commission or Decimal("0")),
        "total_swap": str(trade.swap or Decimal("0")),
    }


def _build_round_trip_row(
    open_trade: Trade,
    close_trade: Trade,
    direction: str,
    raw_labels: Dict[str, str],
    job_to_strategy: Dict[int, tuple],
    sid_to_name: Dict[int, str],
) -> Dict[str, Any]:
    """
    Build a single round-trip row from an open and close trade pair.

    Args:
        open_trade:  The trade that opened the position (BUY for longs, SELL for shorts).
        close_trade: The trade that closed the position (SELL for longs, BUY for shorts).
        direction:   "BUY" for long round-trips, "SELL" for short round-trips.
    """
    # Resolve strategy name (prefer opener's, fallback to closer's)
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

    open_strategy = resolve_strategy(open_trade)
    close_strategy = resolve_strategy(close_trade)

    # Prefer opener's strategy if available, else closer's
    strategy_name = open_strategy if open_strategy != "Unattributed" else close_strategy

    # Calculate net P&L: sum of both legs' profit + commission + swap
    open_pnl = (open_trade.profit or Decimal("0")) + (open_trade.commission or Decimal("0")) + (open_trade.swap or Decimal("0"))
    close_pnl = (close_trade.profit or Decimal("0")) + (close_trade.commission or Decimal("0")) + (close_trade.swap or Decimal("0"))
    net_pnl = open_pnl + close_pnl

    # Use opener's comment if available, else closer's
    comment = open_trade.comment or close_trade.comment or ""

    # Format close time for display (Trade Closed column)
    close_time = close_trade.close_time or close_trade.open_time
    trade_closed = close_time.isoformat() if close_time else None

    # Format trade numbers: "OPEN_TICKET → CLOSE_TICKET"
    trade_numbers = f"{open_trade.ticket} → {close_trade.ticket}"

    # Identify BUY and SELL legs for backwards-compatible fields
    if direction == "BUY":
        buy_trade, sell_trade = open_trade, close_trade
    else:
        buy_trade, sell_trade = close_trade, open_trade

    return {
        "open_time": open_trade.open_time,
        "close_time": close_time,
        "symbol": open_trade.symbol,
        "volume": str(open_trade.volume),
        "open_price": str(open_trade.open_price) if open_trade.open_price is not None else None,
        "close_price": str(close_trade.close_price or close_trade.open_price) if (close_trade.close_price or close_trade.open_price) else None,
        "net_pnl": str(net_pnl),
        "net_pnl_money": float(net_pnl),  # Numeric for formatting with currency
        "legs": [open_trade.ticket, close_trade.ticket],
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
    mt5_balance_current: Optional[float] = None,
    balance_ops: Optional[List[Dict[str, Any]]] = None,
    credit: float = 0.0,
) -> tuple[List[Dict[str, Any]], Dict[str, Any], float, str]:
    """
    Compute the cumulative balance series from completed round-trips (chronological) plus, when reliable,
    the account's external cash flows. Also computes observed TRADING statistics.

    Cash-flow-aware baseline (P1): the previous model set ``opening = current_balance - total_trade_pnl``.
    That correctly recovers *net funding* and never counts a deposit as trading P&L, but it silently
    back-dates ANY external cash flow into the opening baseline — so a mid-period deposit/withdrawal would
    move the whole curve up/down from the very start instead of appearing as a step at its real time.

    When ``balance_ops`` (authoritative deposits/withdrawals from the account's OWN per-tenant bridge) is
    supplied AND the account self-reconciles — ``balance == net_funding + total_trade_pnl`` (no credit, and
    the funding+trade history is complete within the snapshot window) — the opening is the funding in place
    at the first trade and later cash flows are placed as their own steps (``net_pnl_money = 0``). This keeps
    a single-initial-deposit account (the beta case) BYTE-IDENTICAL, while a mid-period deposit no longer
    inflates the baseline. If it does NOT reconcile (funding older than the window, credit present, or a data
    gap) we fail closed to the exact previous reconstruction — never a fabricated funding history.

    Args:
        round_trips: round-trip dicts (sorted by close_time DESC from ``_build_round_trips``).
        mt5_balance_current: current MT5 *balance* (not equity) — used to derive/verify the opening.
        balance_ops: optional ``[{"time": aware-datetime|None, "amount": float}]`` external cash flows.
        credit: broker credit total; any non-zero credit forces the fail-closed reconstruction.

    Returns:
        (balance_series, observed_stats, opening_balance_used, opening_balance_source)
        opening_balance_source: "reconciled_ledger" (cash-flow-aware), "last_used" (derived from balance),
        or "fallback_10000" (no balance available).
    """
    _empty_stats = {
        "total_trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0,
        "longest_loss_streak": 0, "max_drawdown_pct": 0.0, "net_pnl_total": 0.0,
    }
    if not round_trips:
        return [], dict(_empty_stats), 10000.0, "fallback_10000"

    # Filter to only completed round-trips (must have close_time)
    completed_rt = [rt for rt in round_trips if rt.get("close_time")]
    if not completed_rt:
        return [], dict(_empty_stats), 10000.0, "fallback_10000"

    # Round-trips come sorted by close_time DESC, reverse for chronological order (ASC)
    sorted_rt = sorted(completed_rt, key=lambda r: r.get("close_time") or "")

    # Calculate total PnL from all completed trades
    total_pnl = sum(float(rt.get("net_pnl_money", 0) or 0) for rt in sorted_rt)

    # ---- Decide opening balance + whether to place external cash flows in time -------------------------
    mid_period_ops: List[Dict[str, Any]] = []
    use_cashflow = False
    if balance_ops and mt5_balance_current is not None and abs(float(credit or 0.0)) < 0.01:
        net_funding = sum(float(op.get("amount", 0.0) or 0.0) for op in balance_ops)
        # Self-consistency identity (no credit, balance excludes open-position float): a match proves we hold
        # the COMPLETE funding+trade history and may place cash flows chronologically. The tolerance is a
        # small FIXED band (float-summation drift is ~1e-7 even for thousands of trades) — it must NOT scale
        # with balance, or a real missing/duplicate cash flow of several dollars would slip through on a
        # large account. A genuine gap (dollars) or a duplicated deal (>= dollars) fails closed instead.
        tol = max(0.05, len(sorted_rt) * 1e-4)
        if abs(mt5_balance_current - (net_funding + total_pnl)) <= tol:
            use_cashflow = True

    if use_cashflow:
        # The account "began trading" at the EARLIEST trade OPEN. Funding at/before that is opening capital;
        # funding after it is a dated cash-flow step — even a deposit that lands while a position is still
        # open (open < deposit < close) must be a step, never folded into the baseline.
        def _rt_open(rt):
            return rt.get("open_time") or rt.get("close_time")
        open_times = [_rt_open(rt) for rt in sorted_rt if _rt_open(rt) is not None]
        first_activity_time = min(open_times) if open_times else sorted_rt[0].get("close_time")

        def _optime(op):
            return op.get("time")
        # Funding at/before trading began — plus any untimed op — is the account's opening capital.
        opening_amt = sum(float(op.get("amount", 0.0) or 0.0) for op in balance_ops
                          if _optime(op) is None or _optime(op) <= first_activity_time)
        opening_balance_used = opening_amt
        opening_balance_source = "reconciled_ledger"
        mid_period_ops = [op for op in balance_ops
                          if _optime(op) is not None and _optime(op) > first_activity_time]
    elif mt5_balance_current is not None:
        opening_balance_used = mt5_balance_current - total_pnl
        opening_balance_source = "last_used"
    else:
        opening_balance_used = 10000.0
        opening_balance_source = "fallback_10000"

    # ---- Merge trades + mid-period cash flows into one time-ordered event stream -----------------------
    events: List[tuple] = [("trade", rt.get("close_time"), rt) for rt in sorted_rt]
    events += [("funding", op.get("time"), op) for op in mid_period_ops]
    events.sort(key=lambda e: e[1])

    balance_series = []
    balance = opening_balance_used
    # Drawdown / win-rate are TRADING metrics — computed on the trade-only curve so a deposit never resets
    # the drawdown peak and a withdrawal never manufactures a drawdown.
    trading_balance = opening_balance_used
    peak = trading_balance
    max_drawdown_pct = 0.0
    wins = 0
    losses = 0
    current_loss_streak = 0
    longest_loss_streak = 0

    for i, (kind, when, obj) in enumerate(events):
        if kind == "trade":
            pnl = float(obj.get("net_pnl_money", 0) or 0)
            balance += pnl
            trading_balance += pnl
            if pnl >= 0:
                wins += 1
                current_loss_streak = 0
            else:
                losses += 1
                current_loss_streak += 1
                longest_loss_streak = max(longest_loss_streak, current_loss_streak)
            if trading_balance > peak:
                peak = trading_balance
            if peak > 0:
                dd = (peak - trading_balance) / peak * 100
                max_drawdown_pct = max(max_drawdown_pct, dd)
            trade_closed = when.isoformat() if hasattr(when, "isoformat") else (str(when) if when else None)
            balance_series.append({
                "index": i,
                "trade_closed": trade_closed,
                "net_pnl_money": round(pnl, 2),
                "balance_after_trade": round(balance, 2),
            })
        else:  # funding: an external deposit(+)/withdrawal(-) — a balance STEP, never trading P&L
            amt = float(obj.get("amount", 0.0) or 0.0)
            balance += amt
            when_iso = when.isoformat() if hasattr(when, "isoformat") else (str(when) if when else None)
            balance_series.append({
                "index": i,
                "trade_closed": when_iso,
                "net_pnl_money": 0.0,
                "balance_after_trade": round(balance, 2),
                "funding_amount": round(amt, 2),
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
    }

    return balance_series, observed_stats, round(opening_balance_used, 2), opening_balance_source


class TradeHistoryView(APIView):
    """
    GET /api/analytics/trade-history/?account=<id>&mode=<roundtrip|deals>&from=<iso>&to=<iso>&strategy=<name>&symbol=<sym>

    Returns trade history from DB (trading.Trade).

    Query params:
    - account (or account_id): Required. The trading account ID.
    - mode: Optional. "roundtrip" (default) or "deals".
        - roundtrip: Returns completed round-trips (BUY→SELL long or SELL→BUY short)
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

        # Ownership gate (TX-AC1F): non-staff users may only access their own
        # account. Return 404 for any foreign/nonexistent id so the response is
        # identical regardless of existence (no account-existence oracle) and the
        # balance/equity enrichment below can never read a foreign account.
        acc_gate = TradingAccount.objects.filter(id=account_id)
        if not user.is_staff:
            acc_gate = acc_gate.filter(user=user)
        if not acc_gate.exists():
            return Response({"detail": "account not found"}, status=404)

        # Mode: roundtrip (default) or deals
        mode = request.query_params.get("mode", "roundtrip").lower()
        if mode not in ("roundtrip", "deals"):
            mode = "roundtrip"

        qs = Trade.objects.select_related("account").filter(account_id=account_id)

        # Ownership gate
        if not user.is_staff:
            qs = qs.filter(account__user=user)

        # Stage filter: LIVE (default), TEST, or ALL
        stage = (request.query_params.get("stage") or "ALL").upper()
        if stage in ("LIVE", "TEST"):
            qs = qs.filter(source_stage=stage)
        # ALL => no filter

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
        # MODE 1: FIFO pairing (all trades paired, no unpaired rows)
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
            mt5_balance_current = None
            mt5_equity_current = None
            currency = "USD"  # Default currency
            balance_ops: Optional[List[Dict[str, Any]]] = None
            credit = 0.0

            try:
                # TX-AC1F: ownership-filtered fetch (defense in depth — the upfront
                # gate already 404s foreign accounts; this ensures the balance/equity
                # enrichment can never read an account the user does not own).
                acc_q = TradingAccount.objects.select_related("mt5_instance").filter(id=account_id)
                if not user.is_staff:
                    acc_q = acc_q.filter(user=user)
                account_obj = acc_q.get()
                # Hosted-aware: resolve the tenant identity from mt5_instance OR AccountProvisioning, so a
                # hosted account's REAL broker balance grounds the chart (no synthetic 10,000 fallback).
                windows_username = _account_windows_username(account_obj)
                if windows_username:
                    account_info = _fetch_mt5_account_balance(account_obj, windows_username)
                    if account_info:
                        mt5_balance_current = account_info.get("balance")
                        mt5_equity_current = account_info.get("equity")
                        currency = account_info.get("currency", "USD") or "USD"
                    # Authoritative external cash flows (deposits/withdrawals) for a cash-flow-aware baseline —
                    # so deposits are not counted as trading profit nor withdrawals as loss. Fail-closed: None
                    # on any failure, and _compute_balance_series keeps the previous reconstruction.
                    ops_info = _fetch_mt5_balance_ops(account_obj, windows_username)
                    if ops_info:
                        balance_ops = ops_info.get("balance_ops") or []
                        credit = float(ops_info.get("credit") or 0.0)
            except TradingAccount.DoesNotExist:
                pass
            except Exception as e:
                logger.warning(f"Error fetching MT5 balance for account {account_id}: {e}")

            # -------------------------------------------------------------------------
            # Compute balance series and observed statistics
            # -------------------------------------------------------------------------
            balance_series, observed_stats, opening_balance_used, opening_balance_source = _compute_balance_series(
                round_trips=round_trips,
                mt5_balance_current=mt5_balance_current,
                balance_ops=balance_ops,
                credit=credit,
            )

            # Net funding (deposits − withdrawals) and realised trading P&L, surfaced as DISTINCT figures so
            # external cash flow is never conflated with trading performance. net_funding is authoritative
            # only when it reconciles (opening_balance_source == "reconciled_ledger"); otherwise null.
            net_funding = None
            if opening_balance_source == "reconciled_ledger" and balance_ops is not None:
                net_funding = round(sum(float(op.get("amount", 0.0) or 0.0) for op in balance_ops), 2)

            return Response({
                "account_id": int(account_id),
                "mode": "roundtrip",
                "count": len(round_trips),
                "trades": round_trips,
                # MT5 account info
                "mt5_balance_current": mt5_balance_current,
                "mt5_equity_current": mt5_equity_current,
                "currency": currency,
                # Balance trajectory
                "opening_balance_used": opening_balance_used,
                "opening_balance_source": opening_balance_source,
                "balance_series": balance_series,
                "observed_stats": observed_stats,
                # Cash-flow separation (funding vs trading P&L) — informational; deposits/withdrawals are
                # NEVER part of trading_pnl.
                "net_funding": net_funding,
                "trading_pnl": observed_stats.get("net_pnl_total", 0.0),
                "credit": round(credit, 2),
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

        # Include the account's ASSIGNED strategies (StrategyAssignment) so an enabled strategy — e.g.
        # Wayond WIM — is SHOWN even when its trades are not yet comment-attributable. Attribution is NEVER
        # fabricated: an assigned strategy with no attributed trades is surfaced with 0 trades and
        # has_attributed_trades=False ("No attributed trades yet"), while genuinely-attributed metrics are
        # kept as computed. (Wayond's WAY### signal comments carry no guvfx:sid tag, so its trades currently
        # fall in the "Unattributed" bucket — a persistence/attribution gap reported separately, out of scope.)
        from strategies.models import StrategyAssignment
        assigned_names = []
        for asn in (StrategyAssignment.objects.filter(account_id=account_id, is_active=True)
                    .select_related("strategy")):
            sname = getattr(asn.strategy, "name", None)
            if sname and sname not in assigned_names:
                assigned_names.append(sname)
        assigned_set = set(assigned_names)
        existing = {r["strategy_name"] for r in out}
        for r in out:
            r["assigned"] = r["strategy_name"] in assigned_set
            r["has_attributed_trades"] = r["trades"] > 0
        for sname in assigned_names:
            if sname not in existing:
                out.append({"strategy_name": sname, "trades": 0, "net_pnl": 0.0, "wins": 0, "losses": 0,
                            "win_rate_pct": 0.0, "assigned": True, "has_attributed_trades": False})

        # Assigned strategies first (customer's live strategies up top), then by net P&L.
        out.sort(key=lambda x: (not x.get("assigned", False), -x["net_pnl"]))
        acct = acc_qs.first()
        return Response({
            "account_id": int(account_id),
            "account_number": getattr(acct, "account_number", "") if acct else "",
            "strategies": out,
        })


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


class DailyPnlView(APIView):
    """
    GET /api/analytics/daily-pnl/?account_id=<id>&days=30&strategy_id=<optional>

    Returns daily aggregated PnL from completed round-trips.
    Uses the SAME pairing logic as trade-history roundtrip mode.

    Query params:
    - account_id (or account): Required. The trading account ID.
    - days: Optional. Number of days to look back (default: 30, max: 365).
    - strategy_id: Optional. Filter by strategy (uses comment tag attribution).
    - mode: Always roundtrip (only supported mode).

    Response shape:
    {
      "account_id": 13,
      "strategy_id": 12|null,
      "mode": "roundtrip",
      "days": 30,
      "series": [{date, trades, wins, losses, win_rate, net_pnl, gross_profit, gross_loss}, ...],
      "totals": {trades, wins, losses, win_rate, net_pnl}
    }

    Win = net_pnl > 0; Loss = net_pnl < 0; Breakeven (== 0) counted as trade but not win/loss.
    Daily grouping is based on UTC date of close_time.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from datetime import timedelta
        from collections import OrderedDict

        user = request.user
        account_id = request.query_params.get("account_id") or request.query_params.get("account")
        if not account_id:
            return Response({"detail": "account_id is required"}, status=400)

        # Parse days (default 30, max 365)
        try:
            days = int(request.query_params.get("days", "30"))
        except (ValueError, TypeError):
            days = 30
        days = max(1, min(days, 365))

        strategy_filter_id = request.query_params.get("strategy_id")

        # Date boundary: only fetch trades that closed in the last N days
        cutoff = timezone.now() - timedelta(days=days)

        qs = Trade.objects.select_related("account").filter(
            account_id=account_id,
            close_time__gte=cutoff,
        )

        # Ownership gate
        if not user.is_staff:
            qs = qs.filter(account__user=user)

        # Stage filter: LIVE (default), TEST, or ALL
        stage = (request.query_params.get("stage") or "ALL").upper()
        if stage in ("LIVE", "TEST"):
            qs = qs.filter(source_stage=stage)

        trades = list(qs.order_by("-close_time")[:2000])

        # --- Reuse attribution logic from TradeHistoryView ---
        raw_labels: dict[str, str] = {}
        sids: set[int] = set()
        demo_job_ids: set[int] = set()

        for t in trades:
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

        job_to_strategy: dict[int, tuple] = {}
        if demo_job_ids:
            jobs_qs = ExecutionJob.objects.select_related("strategy").filter(id__in=demo_job_ids)
            for job in jobs_qs:
                if job.strategy_id:
                    job_to_strategy[job.id] = (job.strategy_id, job.strategy.name if job.strategy else None)
                else:
                    job_to_strategy[job.id] = (None, None)

        sid_to_name: dict[int, str] = {}
        if sids:
            strat_qs = Strategy.objects.filter(Q(id__in=sids) | Q(magic_number__in=sids))
            if not user.is_staff:
                strat_qs = strat_qs.filter(owner=user)
            for s in strat_qs:
                sid_to_name[s.id] = s.name
                if s.magic_number is not None:
                    sid_to_name[int(s.magic_number)] = s.name

        # Build round-trips
        round_trips = _build_round_trips(
            trades=trades,
            raw_labels=raw_labels,
            job_to_strategy=job_to_strategy,
            sid_to_name=sid_to_name,
        )

        # Apply strategy filter if requested
        if strategy_filter_id:
            # Resolve strategy name for the given ID
            try:
                strat_id_int = int(strategy_filter_id)
                strat_obj = Strategy.objects.filter(id=strat_id_int).first()
                if strat_obj:
                    filter_name = strat_obj.name
                    round_trips = [rt for rt in round_trips if rt.get("strategy_name") == filter_name]
                else:
                    round_trips = []
            except (ValueError, TypeError):
                round_trips = []

        # --- Aggregate by UTC date of close_time ---
        daily: dict[str, dict] = {}

        for rt in round_trips:
            close_time = rt.get("close_time")
            if not close_time:
                continue
            # close_time is a datetime or string
            if hasattr(close_time, "strftime"):
                date_key = close_time.strftime("%Y-%m-%d")
            else:
                date_key = str(close_time)[:10]

            pnl = float(rt.get("net_pnl_money", 0) or 0)

            day = daily.setdefault(date_key, {
                "date": date_key,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "net_pnl": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
            })
            day["trades"] += 1
            day["net_pnl"] += pnl
            if pnl > 0:
                day["wins"] += 1
                day["gross_profit"] += pnl
            elif pnl < 0:
                day["losses"] += 1
                day["gross_loss"] += pnl
            # breakeven (pnl == 0): counted as trade but not win/loss

        # Sort by date ascending and compute win_rate per day
        series = sorted(daily.values(), key=lambda d: d["date"])
        for day in series:
            wl = day["wins"] + day["losses"]
            day["win_rate"] = round(day["wins"] / wl, 4) if wl > 0 else 0.0
            day["net_pnl"] = round(day["net_pnl"], 2)
            day["gross_profit"] = round(day["gross_profit"], 2)
            day["gross_loss"] = round(day["gross_loss"], 2)

        # Totals
        total_trades = sum(d["trades"] for d in series)
        total_wins = sum(d["wins"] for d in series)
        total_losses = sum(d["losses"] for d in series)
        total_wl = total_wins + total_losses
        total_net_pnl = round(sum(d["net_pnl"] for d in series), 2)

        return Response({
            "account_id": int(account_id),
            "strategy_id": int(strategy_filter_id) if strategy_filter_id else None,
            "mode": "roundtrip",
            "days": days,
            "series": series,
            "totals": {
                "trades": total_trades,
                "wins": total_wins,
                "losses": total_losses,
                "win_rate": round(total_wins / total_wl, 4) if total_wl > 0 else 0.0,
                "net_pnl": total_net_pnl,
            },
        })
