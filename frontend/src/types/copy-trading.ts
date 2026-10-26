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

// GET/POST /api/copy-trading/config/ — founder-only SHADOW configuration (owner-scoped, audited, idempotent).
export interface EligibleAccount {
  account_id: number;
  name: string;
  broker: string;
  environment: "demo" | "live" | "unknown";
  masked_login: string;
  currency: string | null;
  monitoring_state: "monitored" | "stale" | "no_data";
  data_age_seconds: number | null;
  ready: boolean;
}

export interface ConfigPair {
  follower_account_id: number;
  enabled: boolean;
  follower_current?: boolean;
  sizing_mode: string;
  fixed_lot: string | null;
  max_volume: string | null;
  max_concurrent_positions: number | null;
  instrument_map_ready: boolean;
  instrument_maps: Array<{ master_symbol?: string; follower_symbol?: string; verified?: boolean; identity?: boolean; note?: string }>;
  observer_last_observed_at: string | null;
  last_observed_event: { event_type: string; symbol: string; observed_at: string } | null;
  supported_risk_controls: string[];
  unavailable_risk_metadata: string[];
  live_execution_blocked: boolean;
}

export interface CurrentConfig {
  master_account_id: number;
  observe_enabled: boolean;
  master_current?: boolean;
  needs_operator_attention?: boolean;
  followers: ConfigPair[];
}

export interface CopyTradingConfig {
  enabled: boolean;
  banner: string;
  execution?: string;
  explain?: string;
  eligible_accounts?: EligibleAccount[];
  config?: CurrentConfig | null;
}

// Compact, quiet observation-mode status (no red banner) shared by the Copy Trading read surfaces.
export interface ExecutionStatus {
  mode: string;
  execution: string;
  label: string;
  detail: string;
  note: string;
}

// GET /api/copy-trading/instrument-matrix/ — founder-only, owner-scoped, read-only mapping matrix.
export type MappingClassification =
  | "VERIFIED" | "UNVERIFIED" | "AMBIGUOUS" | "STALE" | "INELIGIBLE" | "ABSENT";

export interface MatrixMaterialDifference {
  master: string | null;
  follower: string | null;
  differs: boolean;
}

export interface MatrixCandidate {
  master_symbol: string;
  follower_symbol: string;
  canonical: { slug: string | null; display: string };
  classification: MappingClassification;
  reason: string;
  confidence: string;
  material_differences: {
    contract_size?: MatrixMaterialDifference;
    settlement_currency?: MatrixMaterialDifference;
    quote_currency?: MatrixMaterialDifference;
    contract_type?: MatrixMaterialDifference;
  };
  verified_at: string | null;
  verified_source: string | null;
}

export interface MatrixDirection {
  master_account_id: number;
  master_broker: string;
  follower_account_id: number;
  follower_broker: string;
  eligible_source: number;
  counts: Record<MappingClassification, number>;
  coverage_pct: number | null;
  candidates: MatrixCandidate[];
}

export interface MatrixAccount {
  account_id: number;
  broker: string;
  catalogue: {
    state: "synced" | "empty" | "stale";
    symbol_count: number;
    enabled_count: number;
    synced_at: string | null;
    age_seconds: number | null;
  };
}

export interface InstrumentMatrix {
  enabled: boolean;
  execution_status?: ExecutionStatus;
  accounts: MatrixAccount[];
  directions: MatrixDirection[];
  note: string;
}

export interface CopyTradingConfigWrite {
  master_account_id: number;
  followers: Array<{ account_id: number; sizing_mode?: string; fixed_lot?: string; max_volume?: string; max_concurrent_positions?: number }>;
}

export interface CopyTradingConfigSaveResult {
  ok: boolean;
  banner: string;
  execution: string;
  audit: { created: number[]; updated: number[]; enabled: number[]; disabled: number[] };
  config: CurrentConfig | null;
}
