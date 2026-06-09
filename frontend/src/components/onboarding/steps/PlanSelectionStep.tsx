"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { apiFetch } from "@/lib/api";
import type { OnboardingState } from "@/types/onboarding";

type Props = {
  state: OnboardingState;
  onComplete: () => void;
};

type PlanOption = {
  key: string;
  label: string;
  description: string;
  badge?: string;
};

const PLANS: PlanOption[] = [
  {
    key: "standard",
    label: "Standard",
    description: "Full platform access including backtests, strategy deployment, and live execution.",
    badge: "Recommended",
  },
  {
    key: "starter_trial",
    label: "Starter Trial",
    description: "Limited access to explore the platform. Backtests and marketplace only.",
  },
];

export function PlanSelectionStep({ state, onComplete }: Props) {
  const [selectedPlan, setSelectedPlan] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (state.plan_selected) {
    return (
      <div>
        <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
          Select Plan
        </h2>
        <p style={{ color: "#86efac", fontSize: "0.9rem" }}>Your plan has been confirmed.</p>
      </div>
    );
  }

  const handleSelectAndConfirm = async () => {
    if (!selectedPlan) return;
    setConfirming(true);
    setError(null);
    try {
      // Step 1: Create/update the subscription via billing endpoint
      await apiFetch("/api/billing/select-plan/", {
        method: "POST",
        body: JSON.stringify({ plan: selectedPlan }),
      });

      // Step 2: Confirm plan selection on onboarding state
      await apiFetch("/api/onboarding/complete-step/", {
        method: "POST",
        body: JSON.stringify({ step: "plan_selected" }),
      });

      onComplete();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to select plan.");
    } finally {
      setConfirming(false);
    }
  };

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        Select Your Plan
      </h2>
      <p style={{ color: "#b7c5dd", fontSize: "0.9rem", marginBottom: "1.25rem", lineHeight: 1.6 }}>
        Choose a plan to get started. You can change your plan later from the billing page.
      </p>

      <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem", marginBottom: "1.25rem" }}>
        {PLANS.map((plan) => {
          const isSelected = selectedPlan === plan.key;
          return (
            <button
              key={plan.key}
              type="button"
              onClick={() => setSelectedPlan(plan.key)}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "0.85rem 1rem",
                borderRadius: 10,
                border: isSelected
                  ? "1.5px solid rgba(74, 179, 255, 0.6)"
                  : "1px solid rgba(74, 179, 255, 0.1)",
                background: isSelected
                  ? "rgba(74, 179, 255, 0.08)"
                  : "rgba(255, 255, 255, 0.02)",
                cursor: "pointer",
                textAlign: "left",
                width: "100%",
                outline: "none",
                transition: "border-color 0.15s, background 0.15s",
              }}
            >
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.25rem" }}>
                  <span style={{ fontSize: "0.95rem", fontWeight: 600, color: "#e9f4ff" }}>
                    {plan.label}
                  </span>
                  {plan.badge && <Badge color="blue">{plan.badge}</Badge>}
                </div>
                <p style={{ margin: 0, fontSize: "0.82rem", color: "#8fa0b7", lineHeight: 1.4 }}>
                  {plan.description}
                </p>
              </div>
              <div
                style={{
                  width: 18,
                  height: 18,
                  borderRadius: "50%",
                  border: isSelected
                    ? "5px solid #4ab3ff"
                    : "2px solid rgba(255, 255, 255, 0.15)",
                  flexShrink: 0,
                  marginLeft: "1rem",
                  transition: "border 0.15s",
                }}
              />
            </button>
          );
        })}
      </div>

      <Button onClick={handleSelectAndConfirm} disabled={confirming || !selectedPlan}>
        {confirming ? "Activating..." : "Activate Plan"}
      </Button>

      {error && (
        <p style={{ color: "#f87171", fontSize: "0.85rem", marginTop: "0.75rem" }}>{error}</p>
      )}
    </div>
  );
}
