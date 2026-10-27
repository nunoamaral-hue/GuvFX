export type TradingAccount = {
  id: number;
  name: string;
  broker_name: string;
  account_number: string;
  is_demo: boolean;
  is_active: boolean;
  disconnected_at?: string | null;
  /** Objective E — the authoritative server-computed trading state (mirrors types/broker.ts). The UI renders it
   * verbatim and never derives account status from is_active. Present on /api/trading/accounts/ responses. */
  trading_state?: { state?: string; label?: string; detail?: string } | null;
  created_at: string;
  updated_at: string;
};

export type StrategyAssignment = {
  id: number;
  strategy: number;
  account: number;
  is_active: boolean;
  stage: "TEST" | "LIVE";
  risk_per_trade_override_pct: number | string | null;
  created_at: string;
  updated_at: string;
};

// ---------------------------------------------------------------------------
// Engine Runtime Status (from /execution/engine-status/ endpoint)
// ---------------------------------------------------------------------------

export type RuntimeStateEntry = {
  strategy_key: string;
  symbol: string;
  last_eval_at: string | null;
  daily_r_pnl: string;
  daily_trade_count: number;
  weekly_r_pnl: string;
  consecutive_losses: number;
  paused_until: string | null;
  pause_reason: string;
  regime_blob: Record<string, unknown>;
  updated_at: string;
};

export type RuntimeEventEntry = {
  event_type: string;
  strategy_key: string;
  symbol: string;
  reason_code: string;
  bar_close_time: string;
  created_at: string;
};

export type EngineStatusResponse = {
  strategy_id: number;
  account_id: number;
  assignment_id: number | null;
  stage: "TEST" | "LIVE" | null;
  checked_at: string;
  runtime_states: RuntimeStateEntry[];
  recent_events: RuntimeEventEntry[];
};
