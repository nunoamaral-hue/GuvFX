/** Backend onboarding state — GET /api/onboarding/state/ */
export type OnboardingState = {
  email_verified: boolean;
  two_factor_enabled: boolean;
  risk_accepted: boolean;
  plan_selected: boolean;
  account_connected: boolean;
  strategy_assigned: boolean;
  onboarding_completed: boolean;
  risk_accepted_at: string | null;
  onboarding_completed_at: string | null;
};

/** Backend readiness response — GET /api/onboarding/readiness/ */
export type ReadinessResponse = {
  onboarding_completed: boolean;
  missing_steps: string[];
  readiness_eligible: boolean;
  readiness_checks: {
    has_active_account: boolean;
    has_live_assignment: boolean;
    entitlement_valid: boolean;
    terminal_node_valid: boolean;
  };
  permitted: boolean;
};

/** Broker partner — from GET /api/onboarding/brokers/ */
export type BrokerPartner = {
  id: number;
  name: string;
  broker_code: string;
  referral_url: string;
  is_active: boolean;
};

/** Step identifiers matching backend flags */
export type OnboardingStepId =
  | "email_verified"
  | "two_factor_enabled"
  | "risk_accepted"
  | "plan_selected"
  | "account_connected"
  | "strategy_assigned";

/**
 * Canonical 5-step onboarding model (public-facing labels).
 *
 * Step 1: Create account       — handled by /register (not in this list)
 * Step 2: Select plan          — plan_selected
 * Step 3: Complete profile     — risk_accepted (+ optional email_verified, 2FA)
 * Step 4: Connect broker       — account_connected
 * Step 5: Get started          — strategy_assigned + readiness review
 */
export type StepConfig = {
  /** UI step number (2–5, since step 1 is /register) */
  stepNumber: number;
  /** Canonical label shown in progress rail */
  label: string;
  /**
   * Backend flags that must ALL be true for this step to be considered complete.
   * Only required flags are listed — optional flags (2FA) are handled within
   * the step component itself.
   */
  requiredFlags: OnboardingStepId[];
  /** Component key used by OnboardingShell to render the right step */
  componentKey: string;
};

/** Ordered onboarding steps (steps 2–5; step 1 is /register) */
export const ONBOARDING_STEPS: StepConfig[] = [
  {
    stepNumber: 2,
    label: "Select plan",
    requiredFlags: ["plan_selected"],
    componentKey: "plan",
  },
  {
    stepNumber: 3,
    label: "Complete profile",
    requiredFlags: ["risk_accepted"],
    componentKey: "profile",
  },
  {
    stepNumber: 4,
    label: "Connect broker",
    requiredFlags: ["account_connected"],
    componentKey: "broker",
  },
  {
    stepNumber: 5,
    label: "Get started",
    requiredFlags: ["strategy_assigned"],
    componentKey: "get_started",
  },
];

/** Check if all required flags for a step are satisfied */
export function isStepComplete(state: OnboardingState, step: StepConfig): boolean {
  return step.requiredFlags.every((flag) => state[flag]);
}

/** Find the index of the first incomplete step, or -1 if all complete */
export function findCurrentStepIndex(state: OnboardingState): number {
  for (let i = 0; i < ONBOARDING_STEPS.length; i++) {
    if (!isStepComplete(state, ONBOARDING_STEPS[i])) return i;
  }
  return -1; // all complete
}
