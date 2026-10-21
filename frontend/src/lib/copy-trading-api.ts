// Typed read-only client for the Copy Trading API (ADR-0051, PR C). Monitoring only.
import { apiFetch } from "@/lib/api";
import type { CopyTradingOverview } from "@/types/copy-trading";

export async function getCopyTradingOverview(accountId: number): Promise<CopyTradingOverview> {
  return apiFetch<CopyTradingOverview>(`/api/copy-trading/accounts/${accountId}/overview/`, {});
}
