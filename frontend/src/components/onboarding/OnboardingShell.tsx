"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch } from "@/lib/api";
import type { OnboardingState } from "@/types/onboarding";
import { ONBOARDING_STEPS, findCurrentStepIndex } from "@/types/onboarding";
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
  const router = useRouter();
  const [state, setState] = useState<OnboardingState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchState = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<OnboardingState>("/api/onboarding/state/", {});
      setState(data);

      // Completed users → redirect to dashboard
      if (data.onboarding_completed) {
        router.replace("/dashboard");
        return;
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load onboarding state.");
    } finally {
      setLoading(false);
    }
  }, [router]);

  useEffect(() => {
    fetchState();
  }, [fetchState]);

  const handleStepComplete = useCallback(() => {
    fetchState();
  }, [fetchState]);

  // ── Loading ──
  if (loading && !state) {
    return (
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Getting Started</h1>
        <p style={{ color: "#94a3b8", fontSize: "0.9rem" }}>Loading your setup progress...</p>
      </div>
    );
  }

  // ── Error ──
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

  const currentStepIndex = findCurrentStepIndex(state);

  // All steps complete but onboarding_completed not yet set — show readiness
  const showReadiness = currentStepIndex === -1;
  const currentStep = showReadiness ? null : ONBOARDING_STEPS[currentStepIndex];
  const stepNumber = currentStep ? currentStep.stepNumber : 5;
  const totalSteps = 5;

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Getting Started</h1>
      <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1.5rem" }}>
        Step {stepNumber} of {totalSteps} — Complete the steps below to set up your GuvFX workspace.
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
          <OnboardingProgress
            state={state}
            currentStepIndex={showReadiness ? ONBOARDING_STEPS.length : currentStepIndex}
          />
        </div>

        {/* Right: Active step content */}
        <div style={glassCard}>
          {currentStep?.componentKey === "plan" && (
            <PlanSelectionStep state={state} onComplete={handleStepComplete} />
          )}
          {currentStep?.componentKey === "profile" && (
            <>
              {/* Optional sub-steps within "Complete profile" */}
              {!state.email_verified && (
                <EmailVerificationStep state={state} onComplete={handleStepComplete} />
              )}
              {state.email_verified && !state.two_factor_enabled && !state.risk_accepted && (
                <TwoFactorStep
                  state={state}
                  onComplete={handleStepComplete}
                  onSkip={handleStepComplete}
                />
              )}
              {state.email_verified && !state.risk_accepted && state.two_factor_enabled && (
                <RiskAcceptanceStep state={state} onComplete={handleStepComplete} />
              )}
              {state.email_verified && !state.risk_accepted && !state.two_factor_enabled && (
                <RiskAcceptanceStep state={state} onComplete={handleStepComplete} />
              )}
              {state.email_verified && state.risk_accepted && (
                <div>
                  <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
                    Complete Profile
                  </h2>
                  <p style={{ color: "#86efac", fontSize: "0.9rem" }}>Profile setup is complete.</p>
                </div>
              )}
            </>
          )}
          {currentStep?.componentKey === "broker" && (
            <>
              <BrokerStep />
              <div style={{ marginTop: "1.25rem" }}>
                <AccountConnectionStep state={state} onComplete={handleStepComplete} />
              </div>
            </>
          )}
          {currentStep?.componentKey === "get_started" && !state.strategy_assigned && (
            <StrategyAssignmentStep state={state} onComplete={handleStepComplete} />
          )}
          {showReadiness && <ReadinessStep />}
        </div>
      </div>
    </div>
  );
}
