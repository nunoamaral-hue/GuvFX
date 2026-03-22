"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";
import type { OnboardingState } from "@/types/onboarding";

type Props = {
  state: OnboardingState;
  onComplete: () => void;
};

export function RiskAcceptanceStep({ state, onComplete }: Props) {
  const [accepting, setAccepting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (state.risk_accepted) {
    return (
      <div>
        <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
          Risk Disclosure
        </h2>
        <p style={{ color: "#86efac", fontSize: "0.9rem" }}>
          Risk disclosure accepted{state.risk_accepted_at
            ? ` on ${new Date(state.risk_accepted_at).toLocaleDateString()}`
            : ""}.
        </p>
      </div>
    );
  }

  const handleAccept = async () => {
    setAccepting(true);
    setError(null);
    try {
      await apiFetch("/api/onboarding/risk/accept/", { method: "POST" });
      onComplete();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to accept risk disclosure.");
    } finally {
      setAccepting(false);
    }
  };

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        Risk Disclosure
      </h2>
      <div
        style={{
          padding: "1rem",
          borderRadius: 10,
          border: "1px solid rgba(251, 191, 36, 0.2)",
          background: "rgba(251, 191, 36, 0.04)",
          marginBottom: "1.25rem",
          lineHeight: 1.7,
          fontSize: "0.85rem",
          color: "#b7c5dd",
        }}
      >
        <p style={{ marginTop: 0 }}>
          Trading in financial instruments carries a high level of risk and may not be suitable
          for all investors. You should carefully consider your investment objectives, level of
          experience, and risk appetite before making any trading decisions.
        </p>
        <p style={{ marginBottom: 0 }}>
          Past performance does not guarantee future results. GuvFX is a strategy management
          platform and does not provide investment advice. You are solely responsible for all
          trading decisions made through this platform.
        </p>
      </div>

      <Button onClick={handleAccept} disabled={accepting}>
        {accepting ? "Processing..." : "I Understand and Accept the Risks"}
      </Button>

      {error && (
        <p style={{ color: "#f87171", fontSize: "0.85rem", marginTop: "0.75rem" }}>{error}</p>
      )}
    </div>
  );
}
