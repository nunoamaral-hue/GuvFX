/** Member withdrawal metrics — shape of GET /api/broker-intelligence/withdrawals/metrics/ (WP6).
 *
 * Truthful by contract (mirrors backend `broker_intelligence.metrics`): PENDING (in flight) is distinct from
 * failed; amounts are per-currency (never mixed); `processing_duration` is null — rendered "N/A", never 0 — when no
 * completed withdrawal has both a requested and completed anchor. */
export type WithdrawalDuration = {
  count: number;
  avg_seconds: number;
  median_seconds: number;
  min_seconds: number;
  max_seconds: number;
};

export type WithdrawalMetrics = {
  total: number;
  by_status: Record<string, number>;
  pending_count: number;              // requested/processing/pending — in flight, NOT failed
  completed_count: number;
  failed_count: number;               // rejected/cancelled
  unresolved_count: number;
  completed_amount_by_currency: Record<string, string>;   // decimal strings, per currency (never mixed)
  completed_amount_known_count: number;
  processing_duration: WithdrawalDuration | null;          // null => "N/A" (never fabricated 0)
  genuine_count?: number;             // REAL-provenance withdrawals counted (== total unless demonstration)
  excluded_non_genuine_count?: number; // synthetic/sanitised records set aside (in NO statistic) — transparency only
  demonstration?: boolean;            // DEMO env only: true => figures include labelled synthetic rows
  sample_caveat?: boolean;            // true => a headline aggregate rests on a single sample (not representative)
};

/** Shape of GET /api/broker-intelligence/withdrawals/history/ (PR2). Per-withdrawal rows + the broker/account
 * selector lists. Lifecycle `state` is coarse + truthful: "pending" is in-flight (NOT failed); "unresolved" is a
 * visible state of its own; amounts are AS OBSERVED (never summed); `duration_seconds` is null when both anchors
 * are not present (rendered "N/A", never 0). Account numbers are masked (last 4). Owner-private. */
export type WithdrawalState = "pending" | "completed" | "failed" | "unresolved";

export type WithdrawalRow = {
  id: number;
  broker: string;
  trading_account_id: number | null;   // null => owner-attributed, account-UNRESOLVED (ADR-0050)
  account_masked: string;               // last-4, or the sentinel "UNRESOLVED" for an account-unresolved row
  account_unresolved?: boolean;         // true => the broker account is not registered in GuvFX (owner-attributed)
  state: WithdrawalState;
  status: string;
  amount: string | null;              // observed amount (decimal string) or null — never fabricated
  currency: string;
  requested_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;    // null => "N/A" (never 0)
  broker_reference_id: string;
  reference_correlated: boolean;       // true => matched deterministically by broker reference id
  demonstration?: boolean;             // DEMO env only: true => labelled synthetic/sanitised row
  provenance?: string;                 // REAL | SYNTHETIC | SANITISED (present on the demo surface)
};

export type WithdrawalAccountRef = { id: number; broker: string; account_masked: string };

export type WithdrawalHistory = {
  withdrawals: WithdrawalRow[];
  brokers: string[];
  accounts: WithdrawalAccountRef[];
  demonstration?: boolean;             // DEMO env only: true => the list contains labelled synthetic rows
};

/** WS3 — withdrawal-SPEED analytics; shape of GET /api/broker-intelligence/withdrawals/speed/.
 *
 * Truthful by contract (mirrors backend `broker_intelligence.withdrawal_speed`): speed figures come ONLY from
 * confidently-paired completed withdrawals with both anchors; `headline.state` is "empty" (no sample) / "insufficient"
 * (1..min_sample-1, shown but not yet "typical") / "ok" (>= min_sample); medians/averages are null — never 0 — until a
 * sample exists; pending ages (`in_progress`) are live `now − requested_at` and are NEVER part of any completed figure;
 * impossible durations are quarantined (`quarantined_impossible_count`); currency is preserved (duration is seconds,
 * amounts keep their own currency, no FX). This is a reusable metric-series shape (future dimensions reuse `metric` +
 * `headline` + `summary` + `series` + `brokers`). */
export type SpeedMetric = {
  key: string; unit: string; domain: string; direction: string; label: string;
};

export type SpeedHeadline = {
  state: "empty" | "insufficient" | "ok";
  median_seconds: number | null;
  median_human: string | null;
  average_seconds: number | null;
  average_human: string | null;
  sample_count: number;
  min_sample: number;
  qualifier: string;
  reference_count: number;   // charted samples matched deterministically by a broker reference
  heuristic_count: number;   // charted samples matched by the bounded account+amount+currency+time heuristic
};

export type SpeedSummary = {
  total: number;
  in_progress_count: number;
  completed_count: number;
  completed_with_duration_count: number;
  failed_count: number;
  unresolved_count: number;
  median_seconds: number | null;
  average_seconds: number | null;
  fastest_seconds: number | null;
  slowest_seconds: number | null;
  coverage: { measured: number; completed: number };
  quarantined_impossible_count: number;
  observed_timing_count: number;   // completed, but completion time only observed (ingestion) — not charted/averaged
  untimed_count: number;           // completed, but missing a request/completion anchor — never zeroed
};

export type SpeedSeriesPoint = {
  withdrawal_id: number;
  broker: string;
  account_masked: string;
  account_unresolved: boolean;
  amount: string | null;
  currency: string;
  requested_at: string | null;
  completed_at: string | null;
  duration_seconds: number;      // valid, non-negative (impossible durations are excluded server-side)
  duration_human: string | null;
  match_basis: "reference" | "heuristic";
  demonstration?: boolean;
};

export type SpeedBrokerRow = {
  broker: string;
  sample_count: number;
  median_seconds: number | null;
  median_human: string | null;
  average_seconds: number | null;
  fastest_seconds: number | null;
  slowest_seconds: number | null;
  data_quality: "sufficient" | "limited" | "insufficient";
  min_sample: number;
};

export type SpeedInProgressRow = {
  withdrawal_id: number;
  broker: string;
  account_masked: string;
  account_unresolved: boolean;
  amount: string | null;
  currency: string;
  requested_at: string | null;
  elapsed_seconds: number | null;
  elapsed_human: string | null;
  status: string;
  demonstration?: boolean;
};

export type WithdrawalSpeed = {
  metric: SpeedMetric;
  headline: SpeedHeadline;
  summary: SpeedSummary;
  series: SpeedSeriesPoint[];
  brokers: SpeedBrokerRow[];
  in_progress: SpeedInProgressRow[];
  currencies: string[];
  excluded_non_genuine_count?: number;
  demonstration?: boolean;
  generated_at: string | null;
};
