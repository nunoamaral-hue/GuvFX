// Typed read-only client for the Copy Trading API (ADR-0051, PR C). Monitoring only.
import { apiFetch } from "@/lib/api";
import type { CopyTradingOverview, CopyTradingStatus } from "@/types/copy-trading";

export async function getCopyTradingOverview(accountId: number): Promise<CopyTradingOverview> {
  return apiFetch<CopyTradingOverview>(`/api/copy-trading/accounts/${accountId}/overview/`, {});
}

// Per-user visibility probe: `enabled` is true only for a founder/operator cohort member (default-DENY).
// Used to hide the Copy Trading navigation entry for non-cohort users.
export async function getCopyTradingStatus(): Promise<CopyTradingStatus> {
  return apiFetch<CopyTradingStatus>(`/api/copy-trading/status/`, {});
}
