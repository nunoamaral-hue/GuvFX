"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";
import type { OnboardingState } from "@/types/onboarding";

type Props = {
  state: OnboardingState;
  onComplete: () => void;
};

export function PlanSelectionStep({ state, onComplete }: Props) {
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (state.plan_selected) {
    return (
      <div>
        <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
          Plan Selection
        </h2>
        <p style={{ color: "#86efac", fontSize: "0.9rem" }}>Your plan has been confirmed.</p>
      </div>
    );
  }

  const handleConfirm = async () => {
    setConfirming(true);
    setError(null);
    try {
      await apiFetch("/api/onboarding/complete-step/", {
        method: "POST",
        body: JSON.stringify({ step: "plan_selected" }),
      });
      onComplete();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to confirm plan.");
    } finally {
      setConfirming(false);
    }
  };

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        Confirm Your Plan
      </h2>
      <p style={{ color: "#b7c5dd", fontSize: "0.9rem", marginBottom: "1rem", lineHeight: 1.6 }}>
        If you have already selected a plan through the billing page, click below to confirm
        your selection. If you haven&apos;t selected a plan yet, visit the{" "}
        <a
          href="/account/billing"
          style={{ color: "#4ab3ff", textDecoration: "none" }}
        >
          Billing &amp; Plans
        </a>{" "}
        page first.
      </p>

      <Button onClick={handleConfirm} disabled={confirming}>
        {confirming ? "Confirming..." : "Confirm Plan Selection"}
      </Button>

      {error && (
        <p style={{ color: "#f87171", fontSize: "0.85rem", marginTop: "0.75rem" }}>{error}</p>
      )}
    </div>
  );
}
