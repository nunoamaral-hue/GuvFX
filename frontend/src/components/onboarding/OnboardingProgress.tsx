"use client";

import type { OnboardingState, StepConfig } from "@/types/onboarding";
import { ONBOARDING_STEPS } from "@/types/onboarding";

type Props = {
  state: OnboardingState;
  currentStepIndex: number;
};

function isStepComplete(state: OnboardingState, step: StepConfig): boolean {
  if (step.id === "readiness") return state.onboarding_completed;
  return state[step.id] ?? false;
}

export function OnboardingProgress({ state, currentStepIndex }: Props) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
      {ONBOARDING_STEPS.map((step, idx) => {
        const complete = isStepComplete(state, step);
        const isCurrent = idx === currentStepIndex;
        const isBlocked = idx > currentStepIndex && !complete;

        return (
          <div
            key={step.id}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.65rem",
              padding: "0.5rem 0.75rem",
              borderRadius: 8,
              background: isCurrent
                ? "rgba(74, 179, 255, 0.08)"
                : "transparent",
              border: isCurrent
                ? "1px solid rgba(74, 179, 255, 0.2)"
                : "1px solid transparent",
              opacity: isBlocked ? 0.45 : 1,
              transition: "all 0.15s ease",
            }}
          >
            {/* Step indicator */}
            <span
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                width: 24,
                height: 24,
                borderRadius: "50%",
                fontSize: "0.7rem",
                fontWeight: 700,
                flexShrink: 0,
                ...(complete
                  ? {
                      background: "rgba(34, 197, 94, 0.2)",
                      border: "1px solid rgba(34, 197, 94, 0.4)",
                      color: "#22c55e",
                    }
                  : isCurrent
                    ? {
                        background: "rgba(74, 179, 255, 0.15)",
                        border: "1px solid rgba(74, 179, 255, 0.4)",
                        color: "#4ab3ff",
                      }
                    : {
                        background: "rgba(255, 255, 255, 0.04)",
                        border: "1px solid rgba(255, 255, 255, 0.1)",
                        color: "#64748b",
                      }),
              }}
            >
              {complete ? "✓" : idx + 1}
            </span>

            {/* Step label */}
            <span
              style={{
                fontSize: "0.82rem",
                fontWeight: isCurrent ? 600 : 400,
                color: complete
                  ? "#86efac"
                  : isCurrent
                    ? "#e9f4ff"
                    : "#94a3b8",
              }}
            >
              {step.label}
              {!step.required && (
                <span
                  style={{
                    fontSize: "0.7rem",
                    color: "#64748b",
                    marginLeft: "0.4rem",
                  }}
                >
                  Optional
                </span>
              )}
            </span>
          </div>
        );
      })}
    </div>
  );
}
