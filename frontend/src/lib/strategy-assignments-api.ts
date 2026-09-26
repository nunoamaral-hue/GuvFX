/** WAYOND account-scoped strategy management — thin client for the EXISTING StrategyAssignment API
 * (backend strategies.StrategyAssignmentViewSet). Every call is account/strategy-explicit; the backend
 * enforces BOTH-axis ownership (the caller must own the TradingAccount AND the Strategy) and IDOR-safety,
 * so this client never needs to guess or scope. No new endpoints. */
import { apiFetch } from "@/lib/api";

/** One StrategyAssignment row (account-scoped). `strategy` is the writable PK; `strategy_name`/`lot_per_leg`
 * are read-only display fields. `lot_per_leg` is null when the assignment has no sizing override (falls back
 * to the source-global cap). */
export type StrategyAssignment = {
  id: number;
  strategy: number;
  strategy_name?: string | null;
  account: number;
  is_active: boolean;
  stage: string;
  risk_per_trade_override_pct?: number | null;
  lot_per_leg?: string | null;
};

/** A strategy the member owns and may assign to an account (from GET /api/strategies/strategies/). */
export type MyStrategy = { id: number; name: string };

const ASSIGNMENTS = "/api/strategies/assignments/";
const STRATEGIES = "/api/strategies/strategies/";

/** Assignments for ONE explicit account (owner-scoped + IDOR-safe on the backend). */
export async function listAccountAssignments(accountId: number): Promise<StrategyAssignment[]> {
  return (await apiFetch<StrategyAssignment[]>(`${ASSIGNMENTS}?account=${accountId}`)) || [];
}

/** Strategies the current member owns (the "Add strategy" picker source). */
export async function listMyStrategies(): Promise<MyStrategy[]> {
  return (await apiFetch<MyStrategy[]>(STRATEGIES)) || [];
}

/** Bind a strategy to THIS account (a NEW StrategyAssignment; sibling accounts are never touched). The
 * backend requires ownership of both the account and the strategy and initializes the assignment
 * (conservative sizing + deterministic magic) via its canonical path. */
export function createAssignment(input: { strategy: number; account: number }): Promise<StrategyAssignment> {
  return apiFetch<StrategyAssignment>(ASSIGNMENTS, { method: "POST", body: JSON.stringify(input) });
}

/** Remove ONE assignment from an account. Only affects this (account, strategy) relationship. */
export function removeAssignment(id: number): Promise<void> {
  return apiFetch<void>(`${ASSIGNMENTS}${id}/`, { method: "DELETE" });
}
