// Types for the read-only Copy Trading API (GET /api/copy-trading/...). Display-only; the UI never
// mutates through these (ADR-0051, PR C). Mirrors trade_copier.serializers.

export interface CopyTradingMaster {
  id: number;
  account_id: number;
  broker: string;
  environment: "demo" | "live" | "unknown";
  observe_enabled: boolean;
  last_observed_at: string | null;
}

export interface CopyTradingFollower {
  id: number;
  follower_account_id: number;
  broker: string;
  environment: "demo" | "live" | "unknown";
  enabled: boolean;
  sizing_mode: "FIXED_LOT" | "MULTIPLIER" | "BALANCE_PROPORTIONAL" | "RISK_BASED";
  fixed_lot: string | null;
  multiplier: string | null;
  risk_per_trade_pct: string | null;
  max_volume: string | null;
  max_aggregate_exposure: string | null;
  max_concurrent_positions: number | null;
  max_spread_points: number | null;
  max_slippage_points: number | null;
  sltp_translation: "SAME_PRICE" | "SAME_DISTANCE" | "NONE";
  disable_policy: "A_KEEP_MANAGING" | "B_DETACH" | "C_CLOSE";
  unprotected_close_enabled: boolean;
  unprotected_close_authorization_version: number;
  sync_status: string;
  last_outcome: string;
  live_execution_blocked: boolean;
  updated_at: string;
}

export interface CopyTradingEvent {
  sequence: number;
  event_type: string;
  position_ticket: number;
  symbol: string;
  side: string;
  volume_before: string;
  volume_after: string;
  volume_delta: string;
  prev_sl: string | null;
  new_sl: string | null;
  prev_tp: string | null;
  new_tp: string | null;
  reason: string;
  origin: string;
  observed_at: string;
}

export interface CopyTradingReplication {
  id: number;
  event_sequence: number;
  event_type: string;
  follower_account_id: number;
  requested_op: string;
  status: string;
  reason: string;
  target_volume: string | null;
  follower_symbol: string;
  translated_sl: string | null;
  translated_tp: string | null;
  constraints_authoritative: boolean;
  sltp_translation_ok: boolean;
  follower_ticket: number | null;
  latency_ms: number | null;
  created_at: string;
}

export interface CopyTradingLimitations {
  live_follower_execution: string;
  note: string;
}

export interface CopyTradingOverview {
  enabled: boolean;
  banner?: string;
  master: CopyTradingMaster | null;
  followers: CopyTradingFollower[];
  recent_events: CopyTradingEvent[];
  recent_replications: CopyTradingReplication[];
  limitations: CopyTradingLimitations;
}

// GET /api/copy-trading/status/ — per-user visibility probe. `enabled` reflects the founder/operator
// cohort gate for the calling user (default-DENY). `banner` is the permanent SHADOW — EXECUTION OFF text.
export interface CopyTradingStatus {
  enabled: boolean;
  banner: string;
  execution: string;
  limitations: CopyTradingLimitations;
}
