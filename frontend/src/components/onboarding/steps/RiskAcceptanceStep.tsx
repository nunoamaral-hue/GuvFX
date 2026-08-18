"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";
import type { OnboardingState } from "@/types/onboarding";
import { customerSafeError } from "@/lib/customer-safe-error";
import { useLang } from "@/components/AppShell";
import { formatDate, t } from "@/lib/i18n";

type Props = {
  state: OnboardingState;
  onComplete: () => void;
};

export function RiskAcceptanceStep({ state, onComplete }: Props) {
  const lang = useLang();
  const [accepting, setAccepting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (state.risk_accepted) {
    return (
      <div>
        <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
          {t(lang, "onboarding.risk.title")}
        </h2>
        <p style={{ color: "#86efac", fontSize: "0.9rem" }}>
          {state.risk_accepted_at
            ? t(lang, "onboarding.risk.acceptedOn", { date: formatDate(lang, state.risk_accepted_at) })
            : t(lang, "onboarding.risk.accepted")}
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
      setError(customerSafeError(err, t(lang, "onboarding.risk.saveError")));
    } finally {
      setAccepting(false);
    }
  };

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        {t(lang, "onboarding.risk.title")}
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
          {t(lang, "onboarding.risk.bodyOne")}
        </p>
        <p style={{ marginBottom: 0 }}>
          {t(lang, "onboarding.risk.bodyTwo")}
        </p>
      </div>

      <Button onClick={handleAccept} disabled={accepting}>
        {accepting ? t(lang, "onboarding.risk.processing") : t(lang, "onboarding.risk.accept")}
      </Button>

      {error && (
        <p style={{ color: "#f87171", fontSize: "0.85rem", marginTop: "0.75rem" }}>{error}</p>
      )}
    </div>
  );
}
