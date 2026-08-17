// AJ#7 — Strategy customer journey: Get → Configure → Enable → My Strategies.
//
// This module is the EXTENSIBLE contract layer shared by the Marketplace, the Configure page and My
// Strategies. It holds (a) the commercial model (price), designed so future PAID strategies reuse the SAME
// acquisition journey without a redesign; (b) the HONEST per-strategy Configure contract (only real settings —
// no cosmetic controls the execution path does not consume); and (c) the safety-critical Get / Enable / Disable
// API orchestration. Internal terminology (AUTO_DEMO, arm, execution_authorized_at, runtime_ready …) never
// crosses this boundary into customer copy.

import { apiFetch } from "@/lib/api";
import { authorizeExecution, fetchJourney, type HostedJourney } from "@/lib/hosted-journey";

// ---- Commercial model (extensible; beta = everything FREE) -----------------------------------------------
export type StrategyPrice =
  | { kind: "free" }
  | { kind: "monthly"; amount: string }   // future: e.g. "£29"
  | { kind: "oneTime"; amount: string }   // future: e.g. "£99"
  | { kind: "included" };                 // future: included with plan

/** Per-strategy commerce + capability. `automated` marks the signal-copy strategies that genuinely place
 *  trades (only Wayond today); non-automated cards are research/manual and must never imply auto-execution. */
type StrategyMeta = { price: StrategyPrice; automated: boolean; provider?: string; instrument?: string; timeframe?: string };

const STRATEGY_META: Record<string, StrategyMeta> = {
  "mp-010": { price: { kind: "free" }, automated: true, provider: "Wayond", instrument: "XAUUSD", timeframe: "M15" },
};

/** Compact customer-facing name lookup keyed by marketplace id, so the Configure page can title itself
 *  without importing the marketplace card seed. Kept in sync with the marketplace catalogue. */
const MP_DISPLAY_NAME: Record<string, string> = {
  "mp-001": "London Session Box Breakout",
  "mp-002": "Trend EMA Crossover (HTF filter)",
  "mp-003": "Bollinger Mean Reversion",
  "mp-004": "Head & Shoulders Reversal",
  "mp-005": "Trendline Break Pocket",
  "mp-006": "Adaptive Liquidity Trap Scalper",
  "mp-007": "Structural Continuation Engine",
  "mp-009": "TBP V3 Hybrid Sleeve v1",
  "mp-010": "Wayond WIM",
};

export function mpDisplayName(mpId: string): string {
  return MP_DISPLAY_NAME[mpId] || "Strategy";
}

export function priceFor(mpId: string): StrategyPrice {
  return STRATEGY_META[mpId]?.price ?? { kind: "free" };
}
export function isAutomated(mpId: string): boolean {
  return STRATEGY_META[mpId]?.automated ?? false;
}
/** Customer-facing price label. Free/paid all render on the same card slot so the acquisition CTA never
 *  changes shape when paid strategies arrive. */
export function priceLabel(p: StrategyPrice): string {
  switch (p.kind) {
    case "free": return "Free";
    case "monthly": return `${p.amount}/month`;
    case "oneTime": return `${p.amount} one-time`;
    case "included": return "Included with plan";
  }
}

// ---- Honest Configure contract (real settings only; extensible for future editable controls) -------------
export type ConfigRow = {
  key: string;
  label: string;
  value: string;
  /** account = the one genuinely customer-selectable input today; managed = GuvFX/provider-managed (read-only);
   *  info = descriptive. Future editable controls add kind:"control" here without changing the page. */
  kind: "account" | "managed" | "info";
  help?: string;
};

/** The honest Wayond contract. Only `account` is customer-selectable today; everything else is managed /
 *  signal-driven and shown read-only, with a beta note that advanced personalisation is not yet available.
 *  When per-customer config lands (see docs/operations/strategy-journey/AJ7_STRATEGY_JOURNEY.md §6) it plugs
 *  in here as additional editable rows — the page and journey do not change. */
export function configContract(mpId: string, accountLabel: string): ConfigRow[] {
  const meta = STRATEGY_META[mpId];
  if (mpId !== "mp-010" || !meta) return [];
  return [
    { key: "account", label: "Trading account", value: accountLabel || "—", kind: "account",
      help: "The demo account this strategy will trade on." },
    { key: "strategy", label: "Strategy", value: "Wayond WIM", kind: "info" },
    { key: "provider", label: "Signal provider", value: meta.provider || "Wayond", kind: "info" },
    { key: "instrument", label: "Instrument", value: meta.instrument || "XAUUSD", kind: "info" },
    { key: "timeframe", label: "Timeframe", value: meta.timeframe || "M15", kind: "info" },
    { key: "execution", label: "Execution", value: "Automatically mirrors the provider's signals into your account", kind: "info" },
    { key: "sizing", label: "Position sizing", value: "Managed by GuvFX (beta)", kind: "managed",
      help: "Trade size is set by GuvFX for the beta. Per-account sizing isn't available yet." },
    { key: "takeprofit", label: "Take-profit", value: "Follows the provider's targets", kind: "managed",
      help: "Take-profit levels come from the signal provider and can't be customised yet." },
    { key: "stoploss", label: "Stop loss", value: "Set by the provider's signal", kind: "managed",
      help: "The stop loss comes from the signal provider and can't be customised yet." },
    { key: "trailing", label: "Trailing stop", value: "Not used by this strategy", kind: "managed",
      help: "This strategy doesn't use a trailing stop." },
  ];
}

/** The single beta note shown under the Configure contract. */
export const BETA_CONFIG_NOTE =
  "For the beta, Wayond runs with GuvFX-managed settings. Personalised sizing, take-profit and risk controls are coming soon.";

// ---- Owned-strategy lifecycle (customer-facing) ----------------------------------------------------------
export type OwnedState = "not_owned" | "owned_setup_required" | "ready_to_enable" | "enabled" | "needs_attention";

export type OwnedStateView = { state: OwnedState; label: string; tone: "neutral" | "action" | "ready" | "attention" };

/** Derive the customer-facing lifecycle from the (internal) signal-copy status + hosted journey. No internal
 *  term ever leaves this function. `armed` = an assignment exists (owned); `enabled` = it is active. */
export function deriveOwnedState(opts: {
  owned: boolean; enabled: boolean; ambiguous: boolean; canArm: boolean; journeyReady: boolean;
}): OwnedStateView {
  if (opts.ambiguous) return { state: "needs_attention", label: "Needs attention", tone: "attention" };
  if (!opts.owned) return { state: "not_owned", label: "Not added", tone: "neutral" };
  if (opts.enabled) return { state: "enabled", label: "Enabled", tone: "ready" };
  // owned but not enabled: ready to enable if all gates are green, else setup still required.
  if (opts.canArm && opts.journeyReady) return { state: "ready_to_enable", label: "Ready to enable", tone: "action" };
  return { state: "owned_setup_required", label: "Setup required", tone: "action" };
}

// ---- API orchestration -----------------------------------------------------------------------------------
export type SignalCopyStatus = {
  armed: boolean;
  enabled: boolean;
  ambiguous?: boolean;
  assignment_id?: number | null;
  /** AJ#7 — the account the (unambiguous) owned assignment lives on; null when not owned or ambiguous. */
  account_id?: number | null;
};

export async function fetchSignalCopyStatus(mpId: string): Promise<SignalCopyStatus> {
  return apiFetch<SignalCopyStatus>(
    `/api/strategies/strategies/signal-copy/status/?marketplace_strategy_id=${encodeURIComponent(mpId)}`);
}

/** GET STRATEGY — acquire/own for an account, WITHOUT enabling execution. Idempotent server-side. Creates no
 *  order, no arm, no authorization. Returns the owned assignment id. */
export async function getStrategy(mpId: string, accountId: number): Promise<{ status: string; assignment_id: number; enabled: boolean }> {
  return apiFetch(`/api/strategies/strategies/signal-copy/get/`, {
    method: "POST",
    body: JSON.stringify({ marketplace_strategy_id: mpId, account_id: accountId }),
  });
}

export type EnableResult =
  | { ok: true }
  | { ok: false; stage: "authorize" | "arm"; code?: string; message: string };

/** ENABLE STRATEGY — the explicit, confirmed customer action. Folds ADR-0047 workspace authorization into the
 *  same action via FE orchestration: (1) authorize (writes execution_authorized_at + arms the workspace) ONLY
 *  when the workspace is EXECUTION_READY-but-unauthorized, then (2) arm the strategy (activates the assignment).
 *  Both steps are backend-gated and idempotent, so a partial failure is safely retryable and never duplicates.
 *  MUST be called only from the explicit confirmation click (never on load) — that is the ADR-0047 consent. */
export async function enableStrategy(mpId: string, accountId: number): Promise<EnableResult> {
  // Step 1 — ADR-0047 authorization, only if the workspace is ready and not yet authorized.
  try {
    const load = await fetchJourney();
    const j: HostedJourney | null = load.ok ? load.journey : null;
    if (j && j.can_enable_automated_trading === true) {
      await authorizeExecution();   // idempotent; writes execution_authorized_at + arms the workspace
    }
    // If not EXECUTION_READY yet, we still attempt the arm; the backend fail-closes with a truthful reason.
  } catch (e) {
    const err = e as { message?: string };
    return { ok: false, stage: "authorize", message: err?.message || "We couldn't enable automated trading. Please try again." };
  }
  // Step 2 — arm the strategy (activates the assignment). Backend enforces readiness/ADR-0047/cohort/single-tenant.
  try {
    await apiFetch(`/api/strategies/strategies/signal-copy/arm/`, {
      method: "POST",
      body: JSON.stringify({ marketplace_strategy_id: mpId, account_id: accountId }),
    });
    return { ok: true };
  } catch (e) {
    const err = e as { httpStatus?: number; body?: { status?: string }; message?: string };
    return { ok: false, stage: "arm", code: err?.body?.status, message: armErrorMessage(err?.body?.status, err?.httpStatus) };
  }
}

/** DISABLE STRATEGY — pause execution (assignment is_active=False). Always allowed; idempotent. */
export async function disableStrategy(mpId: string, accountId: number): Promise<void> {
  await apiFetch(`/api/strategies/strategies/signal-copy/toggle/`, {
    method: "POST",
    body: JSON.stringify({ marketplace_strategy_id: mpId, account_id: accountId, enabled: false }),
  });
}

function armErrorMessage(code?: string, httpStatus?: number): string {
  switch (code) {
    case "arming_disabled": return "Automated trading isn't available for this environment yet.";
    case "not_pilot_approved": return "Automated trading isn't available for your account yet. Please contact support.";
    case "account_not_ready": return "This account must be a demo account and active before you can enable it.";
    case "credentials_missing": return "Add and validate your MT5 login for this account first.";
    case "runtime_not_ready": return "Your account is still getting ready to trade. Please try again shortly.";
    case "broker_not_connected": return "We're still connecting to your broker. Please try again shortly.";
    case "workspace_execution_not_authorized":
    case "workspace_execution_disabled": return "Your workspace is still getting ready. Please try again shortly.";
    case "broker_validation_unhealthy": return "Your account needs attention before it can trade. Please contact support.";
    case "runtime_paused": return "Trading is paused on your account. Please contact support.";
    case "duplicate_active_assignment": return "Another strategy is already active on this account.";
    case "source_single_tenant": return "Another account is already running this strategy. Only one can run at a time.";
    default: return httpStatus === 404 ? "That account wasn't found. Please refresh and try again."
      : "We couldn't enable the strategy just now. Please try again.";
  }
}
