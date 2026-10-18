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
  genuine_count?: number;             // REAL-provenance withdrawals counted (== total; synthetic/sanitised excluded)
  excluded_non_genuine_count?: number; // synthetic/sanitised records set aside (in NO statistic) — transparency only
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
  trading_account_id: number;
  account_masked: string;
  state: WithdrawalState;
  status: string;
  amount: string | null;              // observed amount (decimal string) or null — never fabricated
  currency: string;
  requested_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;    // null => "N/A" (never 0)
  broker_reference_id: string;
  reference_correlated: boolean;       // true => matched deterministically by broker reference id
};

export type WithdrawalAccountRef = { id: number; broker: string; account_masked: string };

export type WithdrawalHistory = {
  withdrawals: WithdrawalRow[];
  brokers: string[];
  accounts: WithdrawalAccountRef[];
};
