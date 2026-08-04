/** WP5.3 (ADR-0032) — the single client for the merged WP5.1 operational-event API
 * (GET /api/operations/account-events/). READ-ONLY: one GET, no writes, no new backend endpoints. All
 * URLs live here so components never build endpoints; a thin, mockable wrapper over apiFetch (CSRF /
 * 401-refresh / DRF customer-safe `detail` on error). Owner-scoping + operator visibility are enforced
 * by the backend; the frontend never bypasses it. */
import { apiFetch } from "@/lib/api";
import type { OperationsResponse } from "@/types/operations";

const BASE = "/api/operations/account-events";

export type AccountEventsQuery = {
  limit?: number;
  offset?: number;
  category?: string | null;
};

/** Fetch {summary, timeline} for one account. `category` is the ONLY server-side filter the API supports
 * (severity/open/resolved/visibility/date/search are applied client-side over the returned page). */
export function getAccountEvents(accountId: number, q: AccountEventsQuery = {}): Promise<OperationsResponse> {
  const params = new URLSearchParams({ account_id: String(accountId) });
  if (q.limit != null) params.set("limit", String(q.limit));
  if (q.offset != null) params.set("offset", String(q.offset));
  if (q.category) params.set("category", q.category);
  return apiFetch<OperationsResponse>(`${BASE}/?${params.toString()}`);
}
