export type TradingAccount = {
  id: number;
  name: string;
  broker_name: string;
  account_number: string;
  is_demo: boolean;
  is_active: boolean;
  created_at: string;
  updated_at: string;
};

export type StrategyAssignment = {
  id: number;
  strategy: number;
  account: number;
  is_active: boolean;
  risk_per_trade_override_pct: number | string | null;
  created_at: string;
  updated_at: string;
};
