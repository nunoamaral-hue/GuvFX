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

/** A strategy the member may assign to an account: either one they own (private) or a PUBLISHED
 * marketplace strategy (availability, not ownership). `is_marketplace` lets the UI label a published
 * catalogue strategy. */
export type AvailableStrategy = { id: number; name: string; is_marketplace?: boolean };

/** Per-assignment position sizing (the authoritative execution control) from the leg-sizing endpoint. */
export type LegSizing = {
  assignment_id: number;
  lot_per_leg: string;
  is_override: boolean;
  default_lot_per_leg: string;
  min: string;
  step: string;
  max: string;
  source_cap: string;
  max_legs: number;
  applies_to_live_execution: boolean;
  note: string;
};

const ASSIGNMENTS = "/api/strategies/assignments/";
const ASSIGNABLE = "/api/strategies/strategies/assignable/";

/** Assignments for ONE explicit account (owner-scoped + IDOR-safe on the backend). */
export async function listAccountAssignments(accountId: number): Promise<StrategyAssignment[]> {
  return (await apiFetch<StrategyAssignment[]>(`${ASSIGNMENTS}?account=${accountId}`)) || [];
}

/** One assignment by id (owner-scoped; used by the assignment-config page). */
export function getAssignment(id: number): Promise<StrategyAssignment> {
  return apiFetch<StrategyAssignment>(`${ASSIGNMENTS}${id}/`);
}

/** Strategies AVAILABLE to assign — the member's own PLUS published marketplace strategies (availability,
 * not ownership). This is the "Add strategy" picker source. */
export async function listAssignableStrategies(): Promise<AvailableStrategy[]> {
  return (await apiFetch<AvailableStrategy[]>(ASSIGNABLE)) || [];
}

/** GET the authoritative per-assignment sizing (lot per leg + bounds + member copy). */
export function getLegSizing(assignmentId: number): Promise<LegSizing> {
  return apiFetch<LegSizing>(`${ASSIGNMENTS}${assignmentId}/leg-sizing/`);
}

/** PUT a new per-assignment lot per leg. Affects ONLY this assignment's future signals (never siblings,
 * other users, or open positions); the backend clamps to the operator source cap and versions the change. */
export function setLegSizing(assignmentId: number, lotPerLeg: string): Promise<LegSizing> {
  return apiFetch<LegSizing>(`${ASSIGNMENTS}${assignmentId}/leg-sizing/`, {
    method: "PUT", body: JSON.stringify({ lot_per_leg: lotPerLeg }),
  });
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
