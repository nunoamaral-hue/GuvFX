"""analytics.views_portfolio — read-only multi-account portfolio dashboard endpoints.

Two GET endpoints, both ``IsAuthenticated`` and OBSERVATION-ONLY (they never mutate account/strategy/execution
state or contact a broker to place/modify an order):

* ``GET /api/analytics/portfolio/summary/?scope=ALL|<account_id>``
* ``GET /api/analytics/portfolio/open-trades/?scope=ALL|<account_id>``

Ownership: the scope is resolved from the AUTHENTICATED user's OWNED, non-decommissioned accounts only
(``portfolio.resolve_scope``); a foreign/nonexistent ``account_id`` returns 404 (no existence oracle). Staff policy
DIVERGES DELIBERATELY from the estate ``is_staff`` bypass: a customer portfolio total must be the customer's own
accounts, so this view scopes strictly to ``request.user`` (no staff cross-user aggregation). [Amber — documented.]

FX: figures are normalised to USD via ``portfolio_fx`` with the ``NullFxRateSource`` (USD->USD only; any non-USD
account degrades the affected metric to PARTIAL and is surfaced, never fabricated). Every production account is
effectively USD today.
"""
from __future__ import annotations

from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from analytics import portfolio as PF
from analytics import portfolio_fx as FX
from analytics.views_trade_history import (
    _account_windows_username, _fetch_mt5_account_balance, _fetch_mt5_open_positions,
)

# Bound the rows returned to the client so the open-trades payload can never grow without limit as the estate
# scales (M3). The floating-P/L TOTAL is computed from every position (never capped); only the displayed row
# LIST is bounded, and ``truncated``/``count`` tell the client honestly when it was. Live open positions across a
# customer's accounts are normally far below this.
_MAX_OPEN_ROWS = 300


def _rate_source() -> FX.FxRateSource:
    # No vetted internal FX feed exists yet; USD->USD only, honest PARTIAL for anything else.
    return FX.NullFxRateSource()


def _balance_fetcher(account):
    return _fetch_mt5_account_balance(account, _account_windows_username(account))


def _positions_fetcher(account):
    return _fetch_mt5_open_positions(account, _account_windows_username(account))


class PortfolioSummaryView(APIView):
    """Portfolio (ALL) or single-account summary: per-account balance/equity/state + USD aggregate + metrics."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope = request.query_params.get("scope", "ALL")
        payload, err = PF.build_summary(
            request.user, scope,
            fetch_balance=_balance_fetcher, fetch_positions=_positions_fetcher,
            rate_source=_rate_source(), stage="LIVE")
        if err is not None:
            return Response({"detail": "account not found" if err == 404 else "bad scope"}, status=err)
        return Response(payload)


class PortfolioOpenTradesView(APIView):
    """Live open positions across the selected scope, account-attributed (position id = <account_id>:<ticket>)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope = request.query_params.get("scope", "ALL")
        accounts, err = PF.resolve_scope(request.user, scope)
        if err is not None:
            return Response({"detail": "account not found" if err == 404 else "bad scope"}, status=err)
        rows, per_open = PF.open_positions(accounts, fetch_positions=_positions_fetcher, rate_source=_rate_source())
        # Preserve per-account PARTIAL/STALE in the top-level total (M1): a floating-P/L total that dropped a
        # non-convertible or unreadable account is reported PARTIAL with those accounts surfaced, never "USD".
        open_pl, stale_ids = PF.portfolio_open_pl(per_open, _rate_source())
        total_count = len(rows)
        truncated = total_count > _MAX_OPEN_ROWS
        from django.utils import timezone
        return Response({
            "reporting_currency": FX.USD, "scope": scope, "generated_at": timezone.now().isoformat(),
            "count": total_count,                       # full number of open positions (floating-P/L covers all)
            "truncated": truncated,                     # True when the row LIST below was capped at _MAX_OPEN_ROWS
            "open_pl_usd": open_pl.as_dict(),
            "stale_accounts": stale_ids,
            "trades": rows[:_MAX_OPEN_ROWS],
        })
