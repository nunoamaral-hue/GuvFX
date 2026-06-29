"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";
import type { OnboardingState } from "@/types/onboarding";

type Props = {
  state: OnboardingState;
  onComplete: () => void;
};

export function AccountConnectionStep({ state, onComplete }: Props) {
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (state.account_connected) {
    return (
      <div>
        <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
          Account Connection
        </h2>
        <p style={{ color: "#86efac", fontSize: "0.9rem" }}>Your trading account is connected.</p>
      </div>
    );
  }

  const handleConfirm = async () => {
    setConfirming(true);
    setError(null);
    try {
      await apiFetch("/api/onboarding/complete-step/", {
        method: "POST",
        body: JSON.stringify({ step: "account_connected" }),
      });
      onComplete();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to confirm account connection.");
    } finally {
      setConfirming(false);
    }
  };

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        Confirm Account Connection
      </h2>
      <p style={{ color: "#b7c5dd", fontSize: "0.9rem", marginBottom: "1rem", lineHeight: 1.6 }}>
        If you have already connected your MT5 trading account on the{" "}
        <a href="/accounts" style={{ color: "#4ab3ff", textDecoration: "none" }}>
          Broker Accounts
        </a>{" "}
        page, click below to confirm. The system will verify that you have an active account.
      </p>

      <Button onClick={handleConfirm} disabled={confirming}>
        {confirming ? "Confirming..." : "Confirm Account Connection"}
      </Button>

      {error && (
        <p style={{ color: "#f87171", fontSize: "0.85rem", marginTop: "0.75rem" }}>{error}</p>
      )}
    </div>
  );
}
