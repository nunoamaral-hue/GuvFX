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
};
