export type EquityPoint = {
  timestamp?: string;
  equity: number;
};

export type BacktestMetrics = {
  total_trades?: number;
  num_trades?: number;
  win_rate_pct?: number;
  avg_rr?: number;
  net_profit?: number;
  max_drawdown?: number;
  equity_curve?: number[] | EquityPoint[];

  max_drawdown_pct?: number;
  total_return_pct?: number;

  // Demo mode flag (Phase 1 confirmable pipeline)
  demo?: boolean;
  notes?: string;

  [key: string]: unknown;
};

export type BacktestRunStatus =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "SUCCESS"
  | "FAILED";

export type BacktestRun = {
  id: number;
  config: number;
  config_name: string;
  strategy: number | null;
  symbol: string;
  timeframe: string;
  date_from: string;
  date_to: string;
  initial_balance: number | string;
  status: BacktestRunStatus;
  error_message: string;
  started_at: string | null;
  finished_at: string | null;
  metrics: BacktestMetrics | null;
  equity_curve?: EquityPoint[] | null;
  created_at: string;
};

// B5 Canonical: Promotion candidate (Packet B — B7)
export type PromotionCandidate = {
  id: number;
  backtest_execution_id: number;
  state: string; // "pending" | "approved" | "rejected"
  created_at: string;
  updated_at: string;
};

// B5 Canonical: Results response shape (GET /api/backtests/results/{job_id}/)
export type BacktestResultsResponse = {
  job_id: number;
  status: string;
  summary_available: boolean;
  summary: {
    total_trades: number;
    win_rate: number | null;
    profit_factor: number | null;
    max_drawdown: number | null;
    sharpe_ratio: number | null;
    expectancy: number | null;
  } | null;
  execution_id: number | null;
  execution_status: string | null;
  artifact_count: number;
  promotion_candidate: PromotionCandidate | null;
};

export type BacktestConfig = {
  id: number;
  name: string;
  description?: string;
  strategy: number;
  strategy_name?: string | null;
  reference_account?: number | null;
  reference_account_name?: string | null;
  symbol: string;
  timeframe: string;
  date_from: string;
  date_to: string;
  initial_balance: number | string;
  risk_per_trade_pct?: number | string | null;
  slippage_points?: number | null;
  commission_per_lot?: number | string | null;
  is_active?: boolean;
  created_at: string;
  updated_at: string;
};
