"""analytics.portfolio — read-only multi-account portfolio projection for the dashboard.

OBSERVATION ONLY. Nothing here mutates account state, strategy, magic, sizing, execution, or the broker; it never
places or modifies an order. It resolves the authenticated member's owned, non-decommissioned accounts, aggregates
their balance/equity/P&L into USD (honestly — see ``portfolio_fx``), computes portfolio performance from the
UNDERLYING closed-trade set (never by averaging per-account percentages), and lists live open positions with
account attribution.

Live broker figures (balance/equity/currency and open positions) are read per-account from that account's OWN
per-tenant bridge via injected fetchers (the views wire the identity-firewalled, fail-closed readers in
``views_trade_history``); a fetch failure marks that account ``stale`` and never drops it silently.
"""
from __future__ import annotations

import logging
from typing import Callable, List, Optional

from django.utils import timezone

from analytics import portfolio_fx as FX
from trading.models import Trade, TradingAccount

logger = logging.getLogger("guvfx.portfolio")

_MAGIC_BASE = 1_000_000_000   # magic = base + StrategyAssignment.id (Phase-A allocator)


def owned_accounts(user):
    """The dashboard portfolio universe: the user's OWNED, NON-decommissioned accounts (tombstones excluded).
    The accounts viewset does not exclude tombstones itself, so this helper is the canonical row source."""
    return TradingAccount.objects.filter(user=user, disconnected_at__isnull=True).order_by("id")


def resolve_scope(user, scope: Optional[str]):
    """Return ``(accounts, error_status)``. ``scope='ALL'`` -> all owned non-decommissioned; a numeric id -> that
    single OWNED account. Fail-closed: a foreign/nonexistent id returns ``(None, 404)`` (identical to nonexistent,
    so there is no account-existence oracle); a malformed scope returns ``(None, 400)``. Never trusts the client."""
    qs = owned_accounts(user)
    s = (scope or "ALL").strip()
    if s.upper() == "ALL":
        return list(qs), None
    try:
        aid = int(s)
    except (TypeError, ValueError):
        return None, 400
    acc = qs.filter(id=aid).first()
    if acc is None:
        return None, 404
    return [acc], None


def mask_account(number: Optional[str]) -> str:
    n = (number or "").strip()
    return ("••••" + n[-4:]) if len(n) >= 4 else "••••"


def _net(t: Trade) -> float:
    return float((t.profit or 0) + (t.commission or 0) + (t.swap or 0))


def portfolio_metrics(accounts, *, stage: str = "LIVE") -> dict:
    """Portfolio performance computed from the UNDERLYING closed-trade set across ``accounts`` — NOT averaged.

    Definitions (single documented breakeven rule): a closed position's net = profit+commission+swap;
    net>0 = win, net<0 = loss, net==0 = breakeven (counted in the total, but neither win nor loss).
    win_rate = wins/total; profit_factor = Σ gross_profit / Σ |gross_loss| (None if there are no losing trades and
    no profit; flagged infinite if profit but zero loss); expectancy = Σ net / total; max_drawdown = peak-to-trough
    on the chronological cumulative-net curve.
    """
    ids = [a.id for a in accounts]
    qs = Trade.objects.filter(account_id__in=ids, close_time__isnull=False)
    if stage in ("LIVE", "TEST"):
        qs = qs.filter(source_stage=stage)
    rows = list(qs.order_by("close_time").only("profit", "commission", "swap", "close_time", "account_id"))

    total = len(rows)
    wins = losses = breakeven = 0
    gross_profit = gross_loss = 0.0
    net_total = 0.0
    peak = cum = max_dd = 0.0
    for t in rows:
        n = _net(t)
        net_total += n
        if n > 0:
            wins += 1
            gross_profit += n
        elif n < 0:
            losses += 1
            gross_loss += -n
        else:
            breakeven += 1
        cum += n
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    if gross_loss > 0:
        pf, pf_inf = round(gross_profit / gross_loss, 3), False
    elif gross_profit > 0:
        pf, pf_inf = None, True                      # winners, no losers -> "infinite"
    else:
        pf, pf_inf = None, False                     # no completed trades either way
    return {
        "total_trades": total, "wins": wins, "losses": losses, "breakeven": breakeven,
        "win_rate_pct": round((wins / total * 100.0), 2) if total else 0.0,
        "profit_factor": pf, "profit_factor_infinite": pf_inf,
        "expectancy": round(net_total / total, 2) if total else 0.0,
        "net_pnl_total": round(net_total, 2),
        "gross_profit": round(gross_profit, 2), "gross_loss": round(gross_loss, 2),
        "max_drawdown_money": round(max_dd, 2),
        "breakeven_rule": "net>0 win; net<0 loss; net==0 breakeven (in total, not win/loss)",
    }


def daily_realized_pnl(accounts, *, stage: str = "LIVE") -> float:
    """Today's (UTC) REALIZED net P&L across accounts (closed positions closed today). Distinct from floating P/L."""
    ids = [a.id for a in accounts]
    today = timezone.now().date()
    qs = Trade.objects.filter(account_id__in=ids, close_time__date=today)
    if stage in ("LIVE", "TEST"):
        qs = qs.filter(source_stage=stage)
    return round(sum(_net(t) for t in qs.only("profit", "commission", "swap")), 2)


def account_net_pnl(account, *, stage: str = "LIVE") -> float:
    qs = Trade.objects.filter(account_id=account.id, close_time__isnull=False)
    if stage in ("LIVE", "TEST"):
        qs = qs.filter(source_stage=stage)
    return round(sum(_net(t) for t in qs.only("profit", "commission", "swap")), 2)


def _strategy_for_magic(account, magic) -> str:
    """Map an MT5 position magic (base + StrategyAssignment.id) to a strategy name for THIS account only. Returns ''
    when unknown/foreign (never guesses across accounts)."""
    try:
        m = int(magic)
    except (TypeError, ValueError):
        return ""
    if m <= _MAGIC_BASE:
        return ""
    from strategies.models import StrategyAssignment
    asn = StrategyAssignment.objects.filter(id=m - _MAGIC_BASE, account_id=account.id).select_related("strategy").first()
    return getattr(getattr(asn, "strategy", None), "name", "") or ""


def open_positions(accounts, *, fetch_positions: Callable, rate_source: FX.FxRateSource):
    """Live open positions across ``accounts`` with account attribution. ``fetch_positions(account)`` returns the
    broker position list (or None on failure). Position identity is ACCOUNT-SCOPED (``<account_id>:<ticket>``) so
    ticket 123 on two brokers never collides. Floating P/L is normalised to USD (PARTIAL if a currency can't
    convert). Returns ``(rows, per_account_open)`` where per_account_open maps account_id -> {open_count,
    open_pl_usd, open_pl_basis, stale}."""
    rows: List[dict] = []
    per_account: dict = {}
    for a in accounts:
        cur = FX.normalise_currency(getattr(a, "account_currency", None))
        res = None
        try:
            res = fetch_positions(a)
        except Exception:  # noqa: BLE001 — a broker read must never break the dashboard
            logger.warning("portfolio: open-positions fetch raised for account %s", a.id)
            res = None
        if res is None:
            per_account[a.id] = {"open_count": 0, "open_pl_usd": None, "open_pl_basis": "STALE", "stale": True}
            continue
        acct_rows = []
        pl_items = []
        for p in res:
            try:
                magic = p.get("magic")
                native_pl = float(p.get("profit")) if p.get("profit") is not None else None
            except (TypeError, ValueError):
                native_pl = None
                magic = p.get("magic")
            usd = FX.to_usd(native_pl, cur, rate_source)
            acct_rows.append({
                "position_id": f"{a.id}:{p.get('ticket')}",
                "ticket": p.get("ticket"),
                "account_id": a.id,
                "broker": a.broker_name,
                "account_masked": mask_account(a.account_number),
                "symbol": p.get("symbol"),
                "side": p.get("side") or ("BUY" if p.get("type") == 0 else "SELL"),
                "volume": p.get("volume"),
                "open_price": p.get("price_open"),
                "current_price": p.get("price_current"),
                "pl_native": native_pl,
                "pl_native_currency": cur,
                "pl_usd": (round(usd["usd"], 2) if usd["ok"] else None),
                "strategy": _strategy_for_magic(a, magic),
            })
            pl_items.append({"account_id": a.id, "amount": native_pl, "currency": cur})
        rows.extend(acct_rows)
        agg = FX.aggregate_usd(pl_items, rate_source)
        per_account[a.id] = {"open_count": len(acct_rows),
                             "open_pl_usd": round(agg.total_usd, 2),
                             "open_pl_basis": agg.basis, "stale": False}
    return rows, per_account


def build_summary(user, scope: Optional[str], *, fetch_balance: Callable, fetch_positions: Callable,
                  rate_source: FX.FxRateSource, stage: str = "LIVE"):
    """Assemble the portfolio summary for ``scope``. Returns ``(payload, error_status)``."""
    from trading.trading_state import resolve_trading_state
    accounts, err = resolve_scope(user, scope)
    if err is not None:
        return None, err

    open_rows, per_open = open_positions(accounts, fetch_positions=fetch_positions, rate_source=rate_source)

    bal_items, eq_items, open_pl_items = [], [], []
    acct_payloads = []
    trading_count = 0
    for a in accounts:
        snap = None
        try:
            snap = fetch_balance(a)
        except Exception:  # noqa: BLE001
            logger.warning("portfolio: balance fetch raised for account %s", a.id)
            snap = None
        cur = FX.normalise_currency((snap or {}).get("currency") or getattr(a, "account_currency", None))
        balance = (snap or {}).get("balance")
        equity = (snap or {}).get("equity")
        stale = snap is None
        try:
            ts = resolve_trading_state(a)
            state = getattr(ts, "state", None) or (ts.get("state") if isinstance(ts, dict) else None)
            state_label = getattr(ts, "label", None) or (ts.get("label") if isinstance(ts, dict) else None)
        except Exception:  # noqa: BLE001
            state, state_label = "PREPARING", "Preparing"
        if state == "TRADING":
            trading_count += 1
        op = per_open.get(a.id, {})
        acct_payloads.append({
            "account_id": a.id, "broker": a.broker_name, "account_masked": mask_account(a.account_number),
            "currency": cur, "is_active": a.is_active,
            "trading_state": {"state": state, "label": state_label},
            "balance_native": balance, "equity_native": equity,
            "net_pnl": account_net_pnl(a, stage=stage),
            "open_count": op.get("open_count", 0), "open_pl_usd": op.get("open_pl_usd"),
            "stale": stale or op.get("stale", False),
        })
        bal_items.append({"account_id": a.id, "amount": balance, "currency": cur})
        eq_items.append({"account_id": a.id, "amount": equity, "currency": cur})
        if op.get("open_pl_usd") is not None and not op.get("stale"):
            open_pl_items.append({"account_id": a.id, "amount": op["open_pl_usd"], "currency": FX.USD})

    bal_agg = FX.aggregate_usd(bal_items, rate_source)
    eq_agg = FX.aggregate_usd(eq_items, rate_source)
    open_pl_agg = FX.aggregate_usd(open_pl_items, rate_source)
    metrics = portfolio_metrics(accounts, stage=stage)

    payload = {
        "reporting_currency": FX.USD,
        "scope": (scope or "ALL"),
        "generated_at": timezone.now().isoformat(),
        "account_count": len(accounts),
        "trading_count": trading_count,
        "accounts": acct_payloads,
        "aggregate": {
            "balance_usd": bal_agg.as_dict(),
            "equity_usd": eq_agg.as_dict(),
            "open_pl_usd": open_pl_agg.as_dict(),
            "daily_realized_pnl_usd": daily_realized_pnl(accounts, stage=stage),  # accounts are USD today; see FX notes
            "metrics": metrics,
        },
    }
    return payload, None
