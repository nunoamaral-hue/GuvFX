"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import type { OnboardingState } from "@/types/onboarding";
import { ONBOARDING_STEPS } from "@/types/onboarding";
import { OnboardingProgress } from "./OnboardingProgress";
import { EmailVerificationStep } from "./steps/EmailVerificationStep";
import { TwoFactorStep } from "./steps/TwoFactorStep";
import { RiskAcceptanceStep } from "./steps/RiskAcceptanceStep";
import { PlanSelectionStep } from "./steps/PlanSelectionStep";
import { BrokerStep } from "./steps/BrokerStep";
import { AccountConnectionStep } from "./steps/AccountConnectionStep";
import { StrategyAssignmentStep } from "./steps/StrategyAssignmentStep";
import { ReadinessStep } from "./steps/ReadinessStep";

// ─────────────────────────────────────────────────────────────────────
// Step index computation from backend state
// ─────────────────────────────────────────────────────────────────────

function computeCurrentStepIndex(state: OnboardingState): number {
  // Walk through steps in order; first incomplete required step is current.
  // Optional steps (2FA) are current only if prior steps are done and 2FA isn't.
  for (let i = 0; i < ONBOARDING_STEPS.length; i++) {
    const step = ONBOARDING_STEPS[i];
    if (step.id === "readiness") {
      // Readiness is the final step — shown when all flags are true
      return i;
    }
    const complete = state[step.id] ?? false;
    if (!complete) {
      // For optional steps, skip to next if prior required steps are done
      if (!step.required) {
        // Check if the step BEFORE this optional one is complete
        // If so, pause here to offer the option
        const priorDone = i === 0 || (() => {
          for (let j = 0; j < i; j++) {
            const prior = ONBOARDING_STEPS[j];
            if (prior.required && !(state[prior.id as keyof OnboardingState] ?? false)) {
              return false;
            }
          }
          return true;
        })();
        if (!priorDone) continue; // skip, prior required steps not done
      }
      return i;
    }
  }
  // All steps complete → readiness step
  return ONBOARDING_STEPS.length - 1;
}

// ─────────────────────────────────────────────────────────────────────
// Glass card style (matches existing GuvFX pattern)
// ─────────────────────────────────────────────────────────────────────

const glassCard: React.CSSProperties = {
  borderRadius: 16,
  border: "1px solid rgba(74, 179, 255, 0.12)",
  background:
    "linear-gradient(135deg, rgba(10, 15, 40, 0.95) 0%, rgba(5, 8, 22, 0.98) 100%)",
  boxShadow:
    "0 8px 32px rgba(0, 0, 0, 0.4), 0 0 60px rgba(30, 111, 255, 0.04)",
  padding: "1.5rem",
};

// ─────────────────────────────────────────────────────────────────────
// Shell component
// ─────────────────────────────────────────────────────────────────────

export function OnboardingShell() {
  const [state, setState] = useState<OnboardingState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Fetch onboarding state from backend (single source of truth)
  const fetchState = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<OnboardingState>("/api/onboarding/state/", {});
      setState(data);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load onboarding state.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchState();
  }, [fetchState]);

  // After any step completes, re-fetch backend state to advance
  const handleStepComplete = useCallback(() => {
    fetchState();
  }, [fetchState]);

  if (loading && !state) {
    return (
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Getting Started</h1>
        <p style={{ color: "#94a3b8", fontSize: "0.9rem" }}>Loading your onboarding progress...</p>
      </div>
    );
  }

  if (error && !state) {
    return (
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Getting Started</h1>
        <div style={{ ...glassCard, borderColor: "rgba(248, 113, 113, 0.3)" }}>
          <p style={{ color: "#f87171", fontSize: "0.9rem", margin: 0 }}>{error}</p>
        </div>
      </div>
    );
  }

  if (!state) return null;

  const currentStepIndex = computeCurrentStepIndex(state);
  const currentStep = ONBOARDING_STEPS[currentStepIndex];

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Getting Started</h1>
      <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1.5rem" }}>
        Complete the steps below to set up your GuvFX platform.
      </p>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "260px 1fr",
          gap: "1.25rem",
          alignItems: "start",
        }}
      >
        {/* Left: Progress sidebar */}
        <div style={glassCard}>
          <OnboardingProgress state={state} currentStepIndex={currentStepIndex} />
        </div>

        {/* Right: Active step content */}
        <div style={glassCard}>
          {currentStep.id === "email_verified" && (
            <EmailVerificationStep state={state} onComplete={handleStepComplete} />
          )}
          {currentStep.id === "two_factor_enabled" && (
            <TwoFactorStep
              state={state}
              onComplete={handleStepComplete}
              onSkip={handleStepComplete}
            />
          )}
          {currentStep.id === "risk_accepted" && (
            <RiskAcceptanceStep state={state} onComplete={handleStepComplete} />
          )}
          {currentStep.id === "plan_selected" && (
            <PlanSelectionStep state={state} onComplete={handleStepComplete} />
          )}
          {currentStep.id === "account_connected" && !state.account_connected && (
            <BrokerStep onContinue={() => {}} />
          )}
          {currentStep.id === "account_connected" && (
            <div style={{ marginTop: currentStep.id === "account_connected" && !state.account_connected ? "1.25rem" : 0 }}>
              <AccountConnectionStep state={state} onComplete={handleStepComplete} />
            </div>
          )}
          {currentStep.id === "strategy_assigned" && (
            <StrategyAssignmentStep state={state} onComplete={handleStepComplete} />
          )}
          {currentStep.id === "readiness" && <ReadinessStep />}
        </div>
      </div>
    </div>
  );
}
