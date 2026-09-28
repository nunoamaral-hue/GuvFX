/** Multi-account portfolio dashboard types — mirror backend analytics.portfolio / views_portfolio.
 *  Reporting currency is USD; monetary aggregates carry a `basis` ("USD" fully converted, "PARTIAL" if any
 *  account currency could not be converted and was excluded). Observation-only. */

export type UsdAggregate = {
  total_usd: number;
  basis: "USD" | "PARTIAL";
  converted_count: number;
  unconverted: { account_id: number; native: number | null; native_currency: string; reason: string }[];
};

/** GET /api/analytics/portfolio/open-trades/?scope=ALL|<id> */
export type OpenTrade = {
  position_id: string;          // account-scoped: "<account_id>:<ticket>" (never collides across brokers)
  ticket: number | string | null;
  account_id: number;
  broker: string;
  account_masked: string;       // ••••1234
  symbol: string | null;
  side: string | null;          // BUY | SELL
  volume: number | string | null;
  open_price: number | null;
  current_price: number | null;
  pl_native: number | null;
  pl_native_currency: string;
  pl_usd: number | null;        // null when unconverted (non-USD, no rate)
  strategy: string;
};

export type OpenTradesResult = {
  reporting_currency: "USD";
  scope: string;
  generated_at: string;
  count: number;                 // full number of open positions (the floating-P/L total covers all of them)
  truncated?: boolean;           // true when the row list was capped server-side (count still reflects the full set)
  open_pl_usd: UsdAggregate;
  stale_accounts: number[];      // accounts whose live positions could not be read (excluded from the total)
  trades: OpenTrade[];
};

/** GET /api/analytics/portfolio/summary/?scope=ALL|<id> */
export type PortfolioAccountRow = {
  account_id: number;
  broker: string;
  account_masked: string;
  currency: string;
  is_active: boolean;
  trading_state: { state: string | null; label: string | null };
  balance_native: number | null;
  equity_native: number | null;
  net_pnl: number | null;        // null when the account's realized P&L cannot be reported as a trustworthy USD figure
  open_count: number;
  open_pl_usd: number | null;
  stale: boolean;
};

/** Portfolio realized-performance metrics. Monetary/classified fields are null when the money `basis` is PARTIAL
 *  (a non-USD account excluded, or a mixed-currency trade) — never a mixed-denomination sum. Counts stay valid. */
export type PortfolioMetrics = {
  total_trades: number;
  wins: number | null; losses: number | null; breakeven: number | null;
  win_rate_pct: number | null; profit_factor: number | null; profit_factor_infinite: boolean;
  expectancy: number | null; net_pnl_total: number | null;
  gross_profit: number | null; gross_loss: number | null;
  max_drawdown_money: number | null; breakeven_rule: string;
  basis: "USD" | "PARTIAL"; excluded_accounts: number[];
};

export type PortfolioSummary = {
  reporting_currency: "USD";
  scope: string;
  generated_at: string;
  account_count: number;
  trading_count: number;
  stale_accounts: number[];
  accounts: PortfolioAccountRow[];
  aggregate: {
    balance_usd: UsdAggregate;
    equity_usd: UsdAggregate;
    open_pl_usd: UsdAggregate;
    daily_realized_pnl_usd: number | null;      // null when non-USD/mixed; basis states it
    daily_realized_pnl_basis: "USD" | "PARTIAL";
    metrics: PortfolioMetrics;
  };
};
