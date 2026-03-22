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

/** Step metadata for the UI */
export type StepConfig = {
  id: OnboardingStepId | "readiness";
  label: string;
  required: boolean;
};

/** Ordered step list */
export const ONBOARDING_STEPS: StepConfig[] = [
  { id: "email_verified", label: "Email Verification", required: true },
  { id: "two_factor_enabled", label: "Two-Factor Authentication", required: false },
  { id: "risk_accepted", label: "Risk Disclosure", required: true },
  { id: "plan_selected", label: "Plan Selection", required: true },
  { id: "account_connected", label: "Account Connection", required: true },
  { id: "strategy_assigned", label: "Strategy Assignment", required: true },
  { id: "readiness", label: "Readiness Review", required: true },
];
