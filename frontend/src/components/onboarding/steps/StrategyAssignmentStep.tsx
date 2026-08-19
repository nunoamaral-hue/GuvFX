"use client";

import { useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";
import type { OnboardingState } from "@/types/onboarding";
import { useLang } from "@/components/AppShell";
import { t } from "@/lib/i18n";

type Props = {
  state: OnboardingState;
  onComplete: () => void;
};

export function StrategyAssignmentStep({ state, onComplete }: Props) {
  const lang = useLang();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (state.strategy_assigned) {
    return (
      <div>
        <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
          {t(lang, "onboarding.strategy.title")}
        </h2>
        <p style={{ color: "#86efac", fontSize: "0.9rem" }}>
          {t(lang, "onboarding.strategy.assigned")}
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
    } catch {
      setError(t(lang, "onboarding.strategy.confirmError"));
    } finally {
      setConfirming(false);
    }
  };

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        {t(lang, "onboarding.strategy.assignTitle")}
      </h2>
      <p style={{ color: "#b7c5dd", fontSize: "0.9rem", marginBottom: "1rem", lineHeight: 1.6 }}>
        {t(lang, "onboarding.strategy.bodyPrefix")} {" "}
        <Link href="/strategies" style={{ color: "#4ab3ff", textDecoration: "none" }}>
          {t(lang, "nav.myStrategies")}
        </Link>{" "}
        {t(lang, "onboarding.strategy.bodySuffix")}
      </p>

      <Button onClick={handleConfirm} disabled={confirming}>
        {confirming ? t(lang, "onboarding.strategy.confirming") : t(lang, "onboarding.strategy.confirm")}
      </Button>

      {error && (
        <p style={{ color: "#f87171", fontSize: "0.85rem", marginTop: "0.75rem" }}>{error}</p>
      )}
    </div>
  );
}
