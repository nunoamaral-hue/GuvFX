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
from analytics.fx_source import StoredDbFxRateSource, fx_conversion_enabled_for, fx_source_priority
from analytics.views_trade_history import (
    _account_windows_username, _fetch_mt5_account_balance, _fetch_mt5_open_positions,
)

# Bound the rows returned to the client so the open-trades payload can never grow without limit as the estate
# scales (M3). The floating-P/L TOTAL is computed from every position (never capped); only the displayed row
# LIST is bounded, and ``truncated``/``count`` tell the client honestly when it was. Live open positions across a
# customer's accounts are normally far below this.
_MAX_OPEN_ROWS = 300


def _rate_source(user) -> FX.FxRateSource:
    """Rate source for CURRENT monetary figures (portfolio summary total + open floating P/L). When conversion is
    enabled FOR THIS USER (global GA gate OR the founder cohort — ``fx_conversion_enabled_for``), GBP->USD converts
    from the DB-only Fed H.10 ``DEXUSUK`` store (today's selected rate — appropriate for a *current* value, with
    honest refusal when stale/missing). Otherwise the USD->USD-only ``NullFxRateSource`` — byte-identical to today.
    Point-in-time HISTORICAL curves must NOT use a single spot rate, so ``PortfolioEquityCurveView`` deliberately
    does not (data.md point-in-time)."""
    return (StoredDbFxRateSource(priority=fx_source_priority())
            if fx_conversion_enabled_for(user) else FX.NullFxRateSource())


# The aggregation (``portfolio.open_positions`` / ``build_summary``) passes a deadline-aware ``timeout`` so one
# hung bridge can neither blow the request's wall-clock budget nor starve the healthy accounts' reads. Default
# preserved for any caller that does not supply one; the single-account trade-history path keeps the fetchers'
# own 10s default (it is unchanged).
def _balance_fetcher(account, timeout: float = 10.0):
    return _fetch_mt5_account_balance(account, _account_windows_username(account), timeout=timeout)


def _positions_fetcher(account, timeout: float = 10.0):
    return _fetch_mt5_open_positions(account, _account_windows_username(account), timeout=timeout)


class PortfolioSummaryView(APIView):
    """Portfolio (ALL) or single-account summary: per-account balance/equity/state + USD aggregate + metrics.

    Member-facing financial definitions (documented; reconciled to broker truth 2026-09-28):
    * Daily PnL   = realized broker P/L (profit+commission+swap) of positions CLOSED today (UTC day; see
                    ``daily_realized_pnl``). Deposits/withdrawals are broker BALANCE deals, never Trades, so they
                    contribute 0.
    * Net PnL     = LIFETIME realized trading P/L across the scope's closed trades.
    * Win Rate / Profit Factor / Expectancy / Max Drawdown = computed from the underlying BROKER-TRADE (roundtrip)
                    set, per ``portfolio_metrics`` (a multi-leg TI signal counts as its individual broker trades,
                    consistent with the trade-history endpoint the estate already uses).
    ``stage="ALL"`` applies NO source_stage filter — the P0 fix: source_stage is a comment-tag classifier, not a
    performance dimension, and real trades are UNKNOWN, so filtering by "LIVE" wrongly reported $0.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope = request.query_params.get("scope", "ALL")
        rate_source = _rate_source(request.user)
        payload, err = PF.build_summary(
            request.user, scope,
            fetch_balance=_balance_fetcher, fetch_positions=_positions_fetcher,
            rate_source=rate_source, stage="ALL")
        if err is not None:
            return Response({"detail": "account not found" if err == 404 else "bad scope"}, status=err)
        # DARK FX disclosure: when conversion is enabled FOR THIS USER, surface the rate provenance (source, exact
        # rate, effective date, staleness/availability) so the UI can disclose it. Additive + gated — when not
        # enabled for the caller the payload carries no ``fx`` key, so default behaviour is byte-identical to today.
        if fx_conversion_enabled_for(request.user) and hasattr(rate_source, "provenance"):
            payload["fx"] = rate_source.provenance()
        return Response(payload)


class PortfolioOpenTradesView(APIView):
    """Live open positions across the selected scope, account-attributed (position id = <account_id>:<ticket>)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope = request.query_params.get("scope", "ALL")
        accounts, err = PF.resolve_scope(request.user, scope)
        if err is not None:
            return Response({"detail": "account not found" if err == 404 else "bad scope"}, status=err)
        # Bridge-observed currency per account (verified equity ledger) so each open position's P/L is labelled in
        # its true currency; a non-USD account is then excluded from the USD total (PARTIAL), never summed as USD.
        rate_source = _rate_source(request.user)
        rows, per_open = PF.open_positions(accounts, fetch_positions=_positions_fetcher, rate_source=rate_source,
                                           observed_currency=PF.observed_currency_map(accounts))
        # Preserve per-account PARTIAL/STALE in the top-level total (M1): a floating-P/L total that dropped a
        # non-convertible or unreadable account is reported PARTIAL with those accounts surfaced, never "USD".
        open_pl, stale_ids = PF.portfolio_open_pl(per_open, rate_source)
        total_count = len(rows)
        truncated = total_count > _MAX_OPEN_ROWS
        # Single-account scope: report that account's OBSERVED native currency (from its latest equity snapshot —
        # the authoritative source; the model's account_currency is unpopulated in prod) so the client renders the
        # floating total honestly in the account's own currency instead of asserting USD. ALL scope, or an account
        # with no snapshot yet, reports USD (unchanged).
        reporting_currency = FX.USD
        if scope.strip().upper() != "ALL" and len(accounts) == 1:
            from analytics.models import AccountEquitySnapshot
            observed = (AccountEquitySnapshot.objects
                        .filter(trading_account_id=accounts[0].id)
                        .order_by("-observed_at")
                        .values_list("account_currency", flat=True).first())
            reporting_currency = FX.normalise_currency(observed)
        from django.utils import timezone
        # Per-account exclusion detail (ADR-0100): structural (e.g. a LIVE terminal whose /mt5/positions is
        # demo-gated) vs transient disconnect, so the UI explains each unavailable account instead of implying
        # every one is "reconnecting". Coverage counts make partial reads explicit.
        unavailable = PF.unavailable_accounts(per_open)
        # Account-level monitoring ENROLMENT stage (ADR-0103): one bounded, read-only projection for the whole
        # scope. It ANNOTATES each account (why it does/does not produce data) without changing the account
        # universe, and enriches the unavailable list so the panel can say "Setting up monitoring" / "Login
        # required" distinctly from a transient disconnect. Fail-open: the panel still renders if it errors.
        enrol_by_id = {}
        try:
            from execution import monitoring_enrolment as ENROL
            enrol_by_id = ENROL.evaluate_enrolment_for_accounts(accounts)
            for u in unavailable:
                est = enrol_by_id.get(u.get("account_id"))
                if est is not None:
                    u["enrolment_stage"] = est.stage
                    u["enrolment_ui_state"] = est.ui_state
                    u["enrolment_structural"] = bool(est.structural)
            monitoring = ENROL.coverage_rollup(enrol_by_id.values())
        except Exception:  # noqa: BLE001
            monitoring = {"total": len(accounts), "monitored": 0,
                          "not_monitored": len(accounts), "by_stage": {}}
        return Response({
            "reporting_currency": reporting_currency, "scope": scope, "generated_at": timezone.now().isoformat(),
            "count": total_count,                       # full number of open positions (floating-P/L covers all)
            "truncated": truncated,                     # True when the row LIST below was capped at _MAX_OPEN_ROWS
            "open_pl_usd": open_pl.as_dict(),
            "stale_accounts": stale_ids,                # retained (back-compat): the ids whose positions were unread
            "unavailable_accounts": unavailable,        # [{account_id, reason, reason_category, structural, enrolment_*}]
            "monitoring": monitoring,                   # ADR-0103 coverage rollup (monitored N of M + by_stage)
            "accounts_total": len(accounts),
            "accounts_included": len(accounts) - len(stale_ids),
            "accounts_unavailable": len(stale_ids),
            "accounts_structurally_unavailable": sum(1 for u in unavailable if u["structural"]),
            "trades": rows[:_MAX_OPEN_ROWS],
        })


class PortfolioEquityCurveView(APIView):
    """Truthful equity curve from the durable snapshot ledger. ``scope=ALL`` -> portfolio curve (freshness-aligned,
    USD, PARTIAL/STALE-honest); ``scope=<account_id>`` -> that account's observed curve. Never fabricates history:
    an empty/one-point ledger returns state BUILDING/SINGLE so the UI shows "history is building" rather than a
    misleading trend. Observation-only; scoped to the caller's OWNED accounts (IDOR-safe via resolve_scope)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from datetime import timedelta
        from django.utils import timezone
        from analytics import equity_snapshots as EQ
        scope = request.query_params.get("scope", "ALL")
        accounts, err = PF.resolve_scope(request.user, scope)
        if err is not None:
            return Response({"detail": "account not found" if err == 404 else "bad scope"}, status=err)
        # Bounded default window so a single request never materialises the whole snapshot history (scale). Overridable
        # via ?days= (clamped 1..365); the read/downsample stays inside the ledger functions.
        try:
            days = max(1, min(365, int(request.query_params.get("days", "90"))))
        except (TypeError, ValueError):
            days = 90
        since = timezone.now() - timedelta(days=days)
        if scope.strip().upper() == "ALL":
            # Point-in-time correctness (data.md): a HISTORICAL curve must never be converted with a single
            # today's spot rate, so the equity curve stays on the honest USD->USD-only source regardless of the
            # FX flag — a non-USD account's historical points remain honestly excluded (PARTIAL), never distorted.
            return Response(EQ.portfolio_equity_curve(accounts, rate_source=FX.NullFxRateSource(), since=since))
        return Response(EQ.account_equity_curve(accounts[0], since=since))
