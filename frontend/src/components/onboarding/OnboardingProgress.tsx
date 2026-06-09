"use client";

import type { OnboardingState } from "@/types/onboarding";
import { ONBOARDING_STEPS, isStepComplete } from "@/types/onboarding";

type Props = {
  state: OnboardingState;
  currentStepIndex: number;
};

export function OnboardingProgress({ state, currentStepIndex }: Props) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
      {/* Step 1 — always complete (user is authenticated) */}
      <StepRow
        stepNumber={1}
        label="Create account"
        status="complete"
      />

      {/* Steps 2–5 */}
      {ONBOARDING_STEPS.map((step, idx) => {
        const complete = isStepComplete(state, step);
        const isCurrent = idx === currentStepIndex;
        const isBlocked = idx > currentStepIndex && !complete;

        return (
          <StepRow
            key={step.componentKey}
            stepNumber={step.stepNumber}
            label={step.label}
            status={complete ? "complete" : isCurrent ? "current" : isBlocked ? "blocked" : "pending"}
          />
        );
      })}
    </div>
  );
}

function StepRow({
  stepNumber,
  label,
  status,
}: {
  stepNumber: number;
  label: string;
  status: "complete" | "current" | "pending" | "blocked";
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: "0.65rem",
        padding: "0.5rem 0.75rem",
        borderRadius: 8,
        background: status === "current" ? "rgba(74, 179, 255, 0.08)" : "transparent",
        border: status === "current" ? "1px solid rgba(74, 179, 255, 0.2)" : "1px solid transparent",
        opacity: status === "blocked" ? 0.45 : 1,
        transition: "all 0.15s ease",
      }}
    >
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
          ...(status === "complete"
            ? { background: "rgba(34, 197, 94, 0.2)", border: "1px solid rgba(34, 197, 94, 0.4)", color: "#22c55e" }
            : status === "current"
              ? { background: "rgba(74, 179, 255, 0.15)", border: "1px solid rgba(74, 179, 255, 0.4)", color: "#4ab3ff" }
              : { background: "rgba(255, 255, 255, 0.04)", border: "1px solid rgba(255, 255, 255, 0.1)", color: "#64748b" }),
        }}
      >
        {status === "complete" ? "✓" : stepNumber}
      </span>
      <span
        style={{
          fontSize: "0.82rem",
          fontWeight: status === "current" ? 600 : 400,
          color: status === "complete" ? "#86efac" : status === "current" ? "#e9f4ff" : "#94a3b8",
        }}
      >
        {label}
      </span>
    </div>
  );
}
