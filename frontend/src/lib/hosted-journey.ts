// Hosted Workspace customer journey (G18) — the customer-facing state machine + typed API wrappers.
//
// The backend (hosted_workspace.onboarding_read_model) is the single source of journey truth: it emits a
// stable `phase` + `next_action` + `delivery` derived from canonical/observation state. This module REUSES
// those states (never invents new ones) and maps them to deterministic, customer-safe view copy — NO internal
// identifiers (canonical_state, execution_node, PROVISIONING, …) ever reach the customer. Execution is never
// touched here: the journey stops at "assign a strategy", strictly below arming and the order gate.

import { apiFetch } from "@/lib/api";

// ---- Backend contract (exact string values from onboarding_read_model.py) --------------------------------
export type JourneyPhase =
  | "NO_WORKSPACE" | "WORKSPACE_REQUESTED" | "WORKSPACE_PREPARING" | "AWAITING_BROKER_LOGIN"
  | "BROKER_CONNECTED" | "ACCOUNT_CONFIRMATION_REQUIRED" | "ACCOUNT_BOUND" | "WORKSPACE_READY"
  | "WORKSPACE_UNAVAILABLE";

export type NextAction =
  | "request_workspace" | "wait" | "open_mt5_and_log_in" | "confirm_broker_account"
  | "assign_strategy" | "contact_support";

export type DeliveryState =
  | "DELIVERY_NOT_AVAILABLE" | "DELIVERY_PREPARING" | "DELIVERY_DELIVERABLE"
  | "DELIVERY_READY" | "DELIVERY_EXTERNAL_GATE";

export interface HostedJourney {
  phase: JourneyPhase;
  next_action: NextAction;
  confirmed: boolean;
  strategy_eligible: boolean;
  delivery: DeliveryState;
  active_login_masked: string;
  /** Server-derived: has the customer's expected broker identity already been recorded (write-once bind)?
   *  The single source of truth for whether the declaration form is still needed — deterministic across
   *  reloads/devices, so the form is never re-shown for an account that is already linked. */
  identity_declared: boolean;
}

// ---- Customer-facing view model --------------------------------------------------------------------------
export type JourneyTone = "action" | "progress" | "ready" | "error";
export type ActionKind = "request" | "launch" | "confirm" | "assign" | "support";

export interface JourneyAction {
  kind: ActionKind;
  label: string;
}

export interface JourneyView {
  /** 0-based index into STEPS; -1 for the terminal error state. */
  stepIndex: number;
  tone: JourneyTone;
  title: string;
  description: string;
  action: JourneyAction | null;
  /** true when the RemoteApp descriptor is deliverable now (drives the Launch button being live vs preparing). */
  canLaunch: boolean;
}

/** The ordered customer stepper. Internal phases collapse onto these five. */
export const STEPS = [
  "Request workspace",
  "Preparing workspace",
  "Open workspace",
  "Confirm your account",
  "Ready to trade",
] as const;

const PHASE_STEP: Record<JourneyPhase, number> = {
  NO_WORKSPACE: 0,
  WORKSPACE_REQUESTED: 1,
  WORKSPACE_PREPARING: 1,
  AWAITING_BROKER_LOGIN: 2,
  BROKER_CONNECTED: 2,
  ACCOUNT_CONFIRMATION_REQUIRED: 3,
  ACCOUNT_BOUND: 3,
  WORKSPACE_READY: 4,
  WORKSPACE_UNAVAILABLE: -1,
};

// next_action → the primary button. `wait` yields no action (progress spinner only).
const ACTION_FOR: Record<NextAction, JourneyAction | null> = {
  request_workspace: { kind: "request", label: "Request workspace" },
  wait: null,
  open_mt5_and_log_in: { kind: "launch", label: "Open MetaTrader & log in" },
  confirm_broker_account: { kind: "confirm", label: "Confirm my account" },
  assign_strategy: { kind: "assign", label: "Choose a strategy" },
  contact_support: { kind: "support", label: "Contact support" },
};

const FALLBACK: JourneyView = {
  stepIndex: -1,
  tone: "error",
  title: "Something needs attention",
  description: "We couldn't read your workspace status. Please contact support so we can help.",
  action: { kind: "support", label: "Contact support" },
  canLaunch: false,
};

/**
 * Map the backend journey to a deterministic customer view. PURE (no I/O). Fail-closed: an unknown/malformed
 * phase or a null journey with no request affordance resolves to the safe FALLBACK, never a leak or a crash.
 */
export function describeJourney(j: HostedJourney | null | undefined): JourneyView {
  if (!j || !(j.phase in PHASE_STEP)) {
    // A caller with no workspace yet is represented server-side as NO_WORKSPACE; a truly absent/garbled
    // payload is a fault → the support fallback. We only treat an explicit NO_WORKSPACE as "start here".
    if (j && j.phase === "NO_WORKSPACE") {
      return startView(j);
    }
    return j === null || j === undefined
      ? { ...startViewBlank() }
      : FALLBACK;
  }
  const stepIndex = PHASE_STEP[j.phase];
  const action = ACTION_FOR[j.next_action] ?? null;
  // BB#1: the live "Open MetaTrader" launch is enabled as soon as delivery is DELIVERABLE (the authority proved
  // the workspace openable) — the customer's click is what CREATES the session — as well as when it is READY
  // (already CONNECTED, e.g. reconnect). Both mean "you can open it now"; the pill only shows for the truly
  // not-yet-openable states (PREPARING / EXTERNAL_GATE / NOT_AVAILABLE).
  const canLaunch = j.delivery === "DELIVERY_READY" || j.delivery === "DELIVERY_DELIVERABLE";

  switch (j.phase) {
    case "NO_WORKSPACE":
      return startView(j);
    case "WORKSPACE_REQUESTED":
      return view(stepIndex, "progress", "Preparing your workspace",
        "Preparing your private MT5 workspace. This usually completes within a few minutes. "
        + "Please keep this page open — we'll move you to the next step automatically.",
        null, canLaunch);
    case "WORKSPACE_PREPARING":
      return view(stepIndex, "progress", "Preparing your workspace",
        "We're building your private, isolated MT5 workspace. This usually completes within a few minutes. "
        + "Next you'll open your workspace and log in. Please keep this page open — we'll move you on automatically.",
        null, canLaunch);
    case "AWAITING_BROKER_LOGIN":
      return view(stepIndex, "action", "Log in to your account",
        "Enter your broker account number and server so we can point your workspace at the right account — "
        + "that's all we use them for. Then open your hosted MetaTrader terminal and log in there: your "
        + "password is typed only inside MetaTrader, and GuvFX never sees or stores it.", action, canLaunch);
    case "BROKER_CONNECTED":
      // Connected, but the active account isn't the one you told us yet → keep guiding the login.
      return view(stepIndex, "action", "Log in to your account",
        loginHint(j) + " Open MetaTrader and make sure you're logged into that account.", action, canLaunch);
    case "ACCOUNT_CONFIRMATION_REQUIRED":
      return view(stepIndex, "action", "Confirm your account",
        loginHint(j) + " If that's correct, confirm it to finish setting up your workspace.", action, canLaunch);
    case "ACCOUNT_BOUND":
      return view(stepIndex, "progress", "Finishing up",
        "Your account is confirmed — we're finishing the last step. Please keep this page open; we'll take "
        + "you through automatically.", null, canLaunch);
    case "WORKSPACE_READY":
      return view(stepIndex, "ready", "Your workspace is ready",
        "Your hosted MT5 workspace is connected and ready. Choose a strategy to get started.", action, canLaunch);
    case "WORKSPACE_UNAVAILABLE":
      return view(stepIndex, "error", "Workspace unavailable",
        "Your hosted workspace isn't available right now. Our team can get it back for you.", action, canLaunch);
  }
  return FALLBACK;
}

function startView(j: HostedJourney): JourneyView {
  return {
    stepIndex: 0, tone: "action", title: "Set up your hosted workspace",
    description: "Request a private hosted MT5 workspace. We'll prepare it, then you'll open it and log in to MetaTrader.",
    action: ACTION_FOR[j.next_action] ?? ACTION_FOR.request_workspace, canLaunch: false,
  };
}

function startViewBlank(): JourneyView {
  return {
    stepIndex: 0, tone: "action", title: "Set up your hosted workspace",
    description: "Request a private hosted MT5 workspace. We'll prepare it, then you'll open it and log in to MetaTrader.",
    action: ACTION_FOR.request_workspace, canLaunch: false,
  };
}

function view(stepIndex: number, tone: JourneyTone, title: string, description: string,
              action: JourneyAction | null, canLaunch: boolean): JourneyView {
  return { stepIndex, tone, title, description, action, canLaunch };
}

function loginHint(j: HostedJourney): string {
  const m = (j.active_login_masked || "").trim();
  return m ? `We found account ${m}.` : "We're checking your broker account.";
}

// ---- Typed API wrappers ----------------------------------------------------------------------------------
const BASE = "/api/hosted-workspace";

/** Result of loading the journey. `unavailable` means the hosted journey isn't open to this user (feature
 * dark, or not entitled) — surfaced as a 404 by the backend. Fail-closed: we never leak why. */
export type JourneyLoad =
  | { ok: true; journey: HostedJourney }
  | { ok: false; unavailable: true };

export async function fetchJourney(): Promise<JourneyLoad> {
  try {
    const journey = await apiFetch<HostedJourney>(`${BASE}/onboarding/journey/`);
    return { ok: true, journey };
  } catch (e) {
    // 404 = feature dark or user not admitted → the journey is simply not available. Any other error rethrows
    // so the page can show a transient "try again" (via toCustomerError), never a dead end.
    if ((e as { status?: number })?.status === 404) return { ok: false, unavailable: true };
    throw e;
  }
}

export async function requestWorkspace(): Promise<HostedJourney> {
  // DEFERRED IDENTITY BIND (Beta UX Correction): request the workspace with NO broker details at all — the
  // customer declares their broker login/server LATER, at the "connect your broker" step, once the workspace
  // has been provisioned. We collect nothing here (and never a password).
  return apiFetch<HostedJourney>(`${BASE}/onboarding/request/`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export interface BindIdentityInput {
  expected_login: string;
  expected_server?: string;
}

export async function bindExpectedAccount(input: BindIdentityInput): Promise<HostedJourney> {
  // Declare the EXPECTED broker identity (login + server) for the already-provisioned workspace. WRITE-ONCE,
  // owner-scoped; the server verifies the live login against it. NEVER send a password.
  return apiFetch<HostedJourney>(`${BASE}/onboarding/bind/`, {
    method: "POST",
    body: JSON.stringify({
      expected_login: input.expected_login.trim(),
      expected_server: (input.expected_server || "").trim(),
    }),
  });
}

export async function confirmAccount(): Promise<HostedJourney> {
  return apiFetch<HostedJourney>(`${BASE}/onboarding/confirm/`, { method: "POST" });
}
