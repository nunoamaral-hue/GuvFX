/** Multi-account portfolio dashboard types — mirror backend analytics.portfolio / views_portfolio.
 *  Reporting currency is USD; monetary aggregates carry a `basis` ("USD" fully converted, "PARTIAL" if any
 *  account currency could not be converted and was excluded). Observation-only. */

/** Machine reporting state of a USD aggregate. OK = all converted (trustworthy number); PARTIAL = measured over the
 *  INCLUDED subset, some excluded; UNAVAILABLE = items existed but NONE converted (total_usd is null — never a
 *  measured 0); EMPTY = no items at all. The frontend renders an explicit label for anything but OK/EMPTY. */
export type AggregateState = "OK" | "PARTIAL" | "UNAVAILABLE" | "EMPTY";
/** Safe, non-identifying exclusion category (no credentials/account-numbers/logins). "mixed" = more than one.
 *  `positions_unsupported_live` + `endpoint_unavailable` are STRUCTURAL (permanent for this read path — the UI must
 *  not imply "reconnecting"); `stale_observation` is a transient disconnect that may self-heal. */
export type ExclusionReason =
  | "currency_unconvertible" | "stale_observation" | "missing_snapshot" | "read_refused"
  | "positions_unsupported_live" | "endpoint_unavailable" | "mixed";

/** Per-account reason an account's live positions could not be read (ADR-0100). `structural` = permanent for this
 *  read path (e.g. a LIVE terminal whose /mt5/positions is demo-gated) — never a transient "reconnecting". */
export type UnavailableAccount = {
  account_id: number;
  reason: string;                 // low-level code (e.g. "account_not_demo"), coarse + non-identifying
  reason_category: ExclusionReason;
  structural: boolean;
};

export type UsdAggregate = {
  total_usd: number | null;      // null when state === "UNAVAILABLE" (all eligible accounts excluded) — never 0
  basis: "USD" | "PARTIAL";
  converted_count: number;
  // Additive honesty fields (optional for backward-compatibility with older payloads / test fixtures).
  state?: AggregateState;
  reason?: ExclusionReason | null;
  included_count?: number;
  excluded_count?: number;
  unconverted: { account_id: number; native: number | null; native_currency: string; reason: string; category?: ExclusionReason }[];
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
  // Usually "USD"; for a single non-USD account scope the backend reports that account's OBSERVED native
  // currency (e.g. "GBP") so the floating-P/L card can render honestly in the account's own currency.
  reporting_currency: string;
  scope: string;
  generated_at: string;
  count: number;                 // full number of open positions (the floating-P/L total covers all of them)
  truncated?: boolean;           // true when the row list was capped server-side (count still reflects the full set)
  open_pl_usd: UsdAggregate;
  stale_accounts: number[];      // accounts whose live positions could not be read (excluded from the total)
  // Per-account exclusion detail (ADR-0100): structural (permanent for this read path) vs transient. The panel
  // uses this to explain WHY each account is unavailable instead of implying every one is "reconnecting".
  unavailable_accounts?: UnavailableAccount[];
  accounts_total?: number;
  accounts_included?: number;
  accounts_unavailable?: number;
  accounts_structurally_unavailable?: number;
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
  // Why this account's live positions are unavailable (ADR-0100); null category when read OK.
  position_unavailable_category?: ExclusionReason | null;
  position_structural?: boolean;
};

/** GET /api/analytics/portfolio/equity-curve/?scope=ALL|<id> — truthful observed equity curve (never fabricated).
 *  Each point carries COVERAGE provenance so the UI can show partial-coverage honestly instead of a smooth line. */
export type EquityCurvePoint = {
  t: string;                     // ISO bucket timestamp
  equity_usd: number;
  basis: "USD" | "PARTIAL";
  complete?: boolean;            // all in-scope accounts contributed a fresh, convertible observation
  present?: number;              // accounts contributing to THIS point
  total?: number;                // in-scope accounts
  stale_accounts?: number[];     // accounts excluded from this point (stale/unconvertible)
};
export type EquityCurve = {
  points: EquityCurvePoint[];
  count: number;
  // OK = ≥2 points; SINGLE = one point; BUILDING = no snapshots yet; PARTIAL = snapshots exist but none usable;
  // EMPTY = no accounts. The UI must distinguish BUILDING ("history is building") from a real short/flat trend.
  state: "OK" | "SINGLE" | "BUILDING" | "PARTIAL" | "EMPTY";
  accounts: number;
  reporting_currency: string;
};

/** Portfolio realized-performance metrics. Monetary/classified fields are null when the money `basis` is PARTIAL
 *  (a non-USD account excluded, or a mixed-currency trade) — never a mixed-denomination sum. Counts stay valid. */
export type PortfolioMetrics = {
  total_trades: number | null;   // null when state === "UNAVAILABLE" (all accounts excluded) — never a measured 0
  wins: number | null; losses: number | null; breakeven: number | null;
  win_rate_pct: number | null; profit_factor: number | null; profit_factor_infinite: boolean;
  expectancy: number | null; net_pnl_total: number | null;
  gross_profit: number | null; gross_loss: number | null;
  max_drawdown_money: number | null; breakeven_rule: string;
  basis: "USD" | "PARTIAL"; excluded_accounts: number[];
  // Additive honesty fields (optional for backward-compatibility).
  state?: AggregateState; reason?: ExclusionReason | null;
  included_count?: number; excluded_count?: number;
  // ADR-0066: a SINGLE non-USD currency scope has its realized MONEY fields (net_pnl_total, max_drawdown_money,
  // expectancy, gross_*) CONVERTED to USD via the governed prior-day rate. These disclose the conversion and are
  // present only when fx_converted is true (count/ratio fields stay dimensionless). ``fx_rate`` is USD per the
  // ``fx_currency``; ``fx_rate_as_of`` is the governed effective date; ``fx_rate_source`` names the feed.
  fx_converted?: boolean; fx_currency?: string; fx_rate?: number;
  fx_rate_as_of?: string; fx_rate_source?: string;
};

/** DARK GBP->USD conversion provenance (ADR-0058). Present ONLY when PORTFOLIO_FX_CONVERSION_ENABLED is ON; the
 *  payload carries no `fx` key when the flag is OFF (default), so flag-OFF behaviour is unchanged. The request
 *  path is DB-only (never calls FRED). `rate` is the EXACT published rate as a decimal STRING (USD per GBP). */
export type FxProvenance = {
  enabled: boolean;
  pair: string;                 // "GBPUSD"
  available: boolean;           // false => honest unavailable (stale/missing); conversion refused, never faked
  source: string;               // "FED_H10"
  source_series: string;        // "DEXUSUK"
  direction: string;            // "USD_PER_GBP"
  as_of: string;
  rate?: string;                // exact decimal string, USD per GBP (only when available)
  effective_date?: string;      // the rate's publication date (only when available)
  revision_number?: number;
  observation_id?: number;
  retrieved_at?: string;
  age_days?: number;
  reason?: string;              // category when !available (e.g. "no_rate_within_staleness")
};

export type PortfolioSummary = {
  reporting_currency: "USD";
  scope: string;
  generated_at: string;
  account_count: number;
  trading_count: number;
  stale_accounts: number[];
  unavailable_accounts?: UnavailableAccount[];
  accounts: PortfolioAccountRow[];
  aggregate: {
    balance_usd: UsdAggregate;
    equity_usd: UsdAggregate;
    open_pl_usd: UsdAggregate;
    daily_realized_pnl_usd: number | null;      // null when non-USD/mixed; basis states it
    daily_realized_pnl_basis: "USD" | "PARTIAL";
    // Daily-P&L coverage disclosure (ADR-0100): the day window and the USD-eligible account split it summed over.
    daily_realized_pnl_window?: string;         // "broker_server_calendar_day"
    daily_realized_pnl_included_count?: number;
    daily_realized_pnl_excluded_count?: number;
    daily_realized_pnl_excluded_accounts?: number[];
    metrics: PortfolioMetrics;
  };
  fx?: FxProvenance;            // present only when the DARK FX-conversion flag is ON
};
