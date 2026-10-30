# ADR-0066 — Single-currency realized-P&L conversion (per-account) + Open Trades tile layout stability

- **Status:** Accepted (additive; dashboard read path only; no order/execution change). Conversion activates only for a
  user with the vetted FX feed enabled (founder cohort), and only for a single-currency scope.
- **Date:** 2026-10-29
- **Workstream:** Dashboard ingestion closure + Open Trades layout stability (P0 submission)
- **Relates to / refines:** the H1 "currency-honest" realized invariant in `analytics/portfolio.py`
  (`portfolio_metrics` / `daily_realized_pnl` / `account_net_pnl`), the governed FX source
  (`analytics/fx_source.StoredDbFxRateSource`, `DailyFxObservation`, prior-day BoE policy), ADR-0065 (monitored
  deal-history ingestion that populated the GBP accounts' closed trades).

## Context

After ADR-0065 ingested the monitored accounts' broker closed-trade history, two GBP accounts carry real closed
trades — #49 (Pepperstone): 4 trades, +£17.28 net (swap −£3.07); #52 (Vantage): 1 trade, −£15.01 (commission
−£6.00); deposits correctly excluded. Their **equity** already converts GBP→USD with disclosure (governed prior-day
BoE rate, e.g. 1.3301 eff 2026-10-28). Their **realized metrics**, however, showed `UNAVAILABLE / currency_unconvertible`:
the realized path (H1) deliberately withheld any non-USD figure because, historically, there was "no vetted FX feed."
That rationale is now obsolete for the conversion case — the balance/equity path converts GBP via a vetted governed
feed. The submission requires accounts #46 (USD), #49 and #52 to show metrics derived from their histories, with #49's
GBP conversion using the documented prior-day policy and the effective date disclosed, and with **no silent currency
mixing**.

## Decision

1. **Convert a SINGLE-currency scope's realized P&L to USD, with disclosure.** `portfolio_metrics`,
   `daily_realized_pnl` and `account_net_pnl` take the `rate_source` and per-account observed currency. When a scope
   has no USD-eligible account and every account shares ONE non-USD currency with an available vetted rate
   (`_single_currency_rate`), the realized figures are computed natively and the MONETARY fields are scaled by that one
   governed rate; count/ratio fields (win rate, profit factor) are denomination-invariant and unchanged. The metrics
   carry `fx_converted / fx_currency / fx_rate / fx_rate_as_of / fx_rate_source`, and the summary already discloses the
   same rate in its top-level `fx` block. State becomes `OK`.
2. **A MIXED scope is never folded in.** When any USD-eligible account is in scope (e.g. ALL = USD #46 + GBP #49/#52),
   the convert branch is not reached: the aggregate stays `PARTIAL`, sums only the USD accounts, and surfaces the non-USD
   accounts under `excluded_accounts` — exactly as before. There is never a cross-currency sum.
3. **Fail-closed without a vetted rate.** Two different non-USD currencies, an unknown/missing observed currency, or a
   `NullFxRateSource` (a non-founder user) all keep the honest `UNAVAILABLE`/`None` — never a measured zero, never an
   unconverted figure presented as USD.

The USD-only and mixed paths are byte-identical to before (the inline USD computation is untouched; a shared
`_closed_trade_stats` + `_assemble_metrics` serve only the new converted branch with `rate=1.0` parity).

## Open Trades tile layout stability (same PR)

The Open Trades tile is made a fixed-height flex column that fills its stretched grid cell so it occupies the SAME
row height as Market Focus and never grows with the trade count: the header, open-count, floating P/L and
freshness/status stay fixed, and only the trade-list body scrolls (CSS `.open-trades-tile` / `.open-trades-body`,
`globals.css`). Desktop/tablet: the body absorbs the remaining height (matches Market Focus). Mobile (≤640px, stacked):
the body is capped (`max-height: 320px`) and scrolls internally so many trades never produce an unusably tall page.
Long broker/account/symbol values truncate (ellipsis, no widening). The scroll body is a keyboard-accessible, named
region (`role="region"`, `tabindex=0`, localized `aria-label`, visible focus ring). No change to Market Focus or
unrelated sections; design tokens reused.

## Consequences

- #49/#52 per-account dashboards show truthful USD-converted realized metrics with the rate/date disclosed; the ALL
  aggregate stays currency-honest PARTIAL. Non-founder users and non-single-currency scopes are unchanged.
- The Open Trades tile is height-stable across the 15/30s poll, reconnect, and empty/loading/error/trade-count
  transitions, on desktop/tablet/mobile and EN/JA.

## Rollback

Revert the PR. The conversion is gated behind the per-user vetted FX feed and the single-currency condition, so a
revert simply returns #49/#52 realized metrics to `UNAVAILABLE` (equity conversion and all other behaviour unchanged).
No data migration; ingested `Trade` rows are untouched.

## Evidence

- Backend: `analytics/tests_portfolio.py` — `RealizedSingleCurrencyConversionTests` (convert+disclose, mixed-never-folds,
  two-currency UNAVAILABLE, no-rate UNAVAILABLE, account/daily conversion) + all pre-existing H1 tests still green (82).
- Frontend: `OpenTradesPanel.test.tsx` — layout hooks, keyboard-named scroll region (EN/JA), truncation, centered empty
  state, fixed header/count/floating with an overflowing list (20).

## 2026-10-30 — Per-account UI rendering (follow-up; implementation completion)

ADR-0066 converted the realized metrics in the backend summary, but the dashboard's per-account performance strip
read the account's NATIVE trade-history only (`/api/analytics/trade-history/`), so a single GBP account still showed
native GBP realized P/L with no BoE disclosure (authenticated screenshots, 2026-10-30). The backend was correct
(summary returns `net_pnl_total` in USD + `fx_converted`/`fx_rate`/`fx_rate_as_of`/`fx_rate_source` + the `fx` block);
only the UI selection was wrong.

Fix (frontend-only): for a SINGLE account the realized money view is decided by one pure helper
`lib/realized-fx.realizedMoneyView(scopedMetrics, acctNetPnl, perfCurrency)`:
- **converted** (vetted rate applied) → primary realized net P/L in **USD** (from the summary's `net_pnl_total`),
  the **native** amount as a secondary figure, and a visible disclosure sourced from the payload
  ("Converted from GBP using BoE GBP/USD 1.3301, effective 2026-10-28" — rate/source/date never hard-coded);
- **non-USD, no vetted rate** → native figure + explicit "Realized P/L shown in GBP — USD conversion unavailable";
- **USD account** → unchanged.
Win rate / profit factor stay dimensionless; max drawdown stays % and expectancy stays R (no conversion); %-of-equity
is computed from the native figure (currency-invariant) since equity stays native. **All Accounts is untouched**
(it never uses this path and remains PARTIAL — non-USD realized excluded, no silent mixing).

Evidence: `lib/realized-fx.test.ts` (8 — positive/negative GBP, fresh-value pass-through, source mapping, unavailable,
USD unchanged, loading, and a rate-less `fx_converted:true` payload falling to unavailable) and
`dashboard/localization.test.tsx` (converted USD primary + native secondary + BoE disclosure EN/JA, negative sign,
the unavailable fallback, and the all-winners Expectancy-money conversion). `next build` green; eslint 0 errors.
No backend change.

Adversarial review (post-implementation) found and fixed two issues before merge: (1) the **Expectancy** tile falls
back from R to money only for an account with no losing trades, and that native per-trade mean must be converted to
USD too under conversion (otherwise a native GBP magnitude would print with a `$`) — fixed via `expMoneyDisplay`
(native mean × governed rate when converted); (2) the `converted` gate now also requires a numeric `fx_rate`, so a
malformed payload can never emit a blank-rate disclosure (it falls to the honest native "unavailable" line). Max
Drawdown money is unaffected — for a single account `ddMoney` is null, so only the `%` branch renders.
