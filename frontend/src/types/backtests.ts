export type EquityPoint = {
  timestamp?: string;
  equity: number;
};

export type BacktestMetrics = {
  total_trades?: number;
  win_rate_pct?: number;
  avg_rr?: number;
  net_profit?: number;
  max_drawdown?: number;
  equity_curve?: number[] | EquityPoint[];

  max_drawdown_pct?: number;
  total_return_pct?: number;
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
  created_at: string;
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
