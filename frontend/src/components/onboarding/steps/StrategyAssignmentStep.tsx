"use client";

import { useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";
import type { OnboardingState } from "@/types/onboarding";

type Props = {
  state: OnboardingState;
  onComplete: () => void;
};

export function StrategyAssignmentStep({ state, onComplete }: Props) {
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (state.strategy_assigned) {
    return (
      <div>
        <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
          Strategy Assignment
        </h2>
        <p style={{ color: "#86efac", fontSize: "0.9rem" }}>
          A strategy is assigned to your account.
        </p>
      </div>
    );
  }

  const handleConfirm = async () => {
    setConfirming(true);
    setError(null);
    try {
      await apiFetch("/api/onboarding/complete-step/", {
        method: "POST",
        body: JSON.stringify({ step: "strategy_assigned" }),
      });
      onComplete();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to confirm strategy assignment.");
    } finally {
      setConfirming(false);
    }
  };

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        Assign a Strategy
      </h2>
      <p style={{ color: "#b7c5dd", fontSize: "0.9rem", marginBottom: "1rem", lineHeight: 1.6 }}>
        Create and assign a strategy to your trading account. Visit the{" "}
        <Link href="/strategies" style={{ color: "#4ab3ff", textDecoration: "none" }}>
          Strategies
        </Link>{" "}
        page to create a strategy and assign it to your account, then return here to confirm.
      </p>

      <Button onClick={handleConfirm} disabled={confirming}>
        {confirming ? "Confirming..." : "Confirm Strategy Assignment"}
      </Button>

      {error && (
        <p style={{ color: "#f87171", fontSize: "0.85rem", marginTop: "0.75rem" }}>{error}</p>
      )}
    </div>
  );
}
