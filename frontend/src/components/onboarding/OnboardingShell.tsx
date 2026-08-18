"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { apiFetch } from "@/lib/api";
import { Button } from "@/components/ui/Button";
import type { OnboardingState } from "@/types/onboarding";
import { ONBOARDING_STEPS, findCurrentStepIndex } from "@/types/onboarding";
import { OnboardingProgress } from "./OnboardingProgress";
import { EmailVerificationStep } from "./steps/EmailVerificationStep";
import { TwoFactorStep } from "./steps/TwoFactorStep";
import { RiskAcceptanceStep } from "./steps/RiskAcceptanceStep";
import { PlanSelectionStep } from "./steps/PlanSelectionStep";
import { useLang } from "@/components/AppShell";
import { t } from "@/lib/i18n";

// Customer Zero Flow Simplification (Option 2): the post-onboarding setup router response
// (GET /api/onboarding/setup-status/ and the `setup` block returned by POST /api/onboarding/complete/).
type SetupStatus = { stage: string; next_route: string };

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
  const lang = useLang();
  const router = useRouter();
  const [state, setState] = useState<OnboardingState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [completing, setCompleting] = useState(false);

  const fetchState = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<OnboardingState>("/api/onboarding/state/", {});
      setState(data);

      // Completed users → resume the next incomplete platform-setup stage. /onboarding acts as an
      // intelligent setup router: ask the backend where this customer should be and go there. On any
      // failure, fall back to the dashboard.
      if (data.onboarding_completed) {
        try {
          const setup = await apiFetch<SetupStatus>("/api/onboarding/setup-status/");
          router.replace(setup.next_route);
        } catch {
          router.replace("/dashboard");
        }
        return;
      }
    } catch {
      setError(t(lang, "onboarding.loadError"));
    } finally {
      setLoading(false);
    }
  }, [router, lang]);

  useEffect(() => {
    fetchState();
  }, [fetchState]);

  const handleStepComplete = useCallback(() => {
    fetchState();
  }, [fetchState]);

  // Explicit "finish setup" hand-off for a customer whose required steps are already done but whose
  // onboarding_completed is still false (e.g. Customer Zero). Finalizes onboarding (idempotent) then hands
  // off into the resolved next platform-setup stage.
  const handleComplete = useCallback(async () => {
    setCompleting(true);
    try {
      const res = await apiFetch<OnboardingState & { setup?: SetupStatus }>(
        "/api/onboarding/complete/",
        { method: "POST" },
      );
      // Only hand off once onboarding actually completed. If a required step is still missing, the backend
      // won't complete it and setup.stage stays "onboarding" — do NOT follow next_route to /onboarding
      // (that would loop). Re-fetch state and surface the remaining step instead.
      if (res.onboarding_completed && res.setup && res.setup.stage !== "onboarding") {
        router.replace(res.setup.next_route);
        return;
      }
      setCompleting(false);
      setError(t(lang, "onboarding.finishRemaining"));
      fetchState();
    } catch {
      setError(t(lang, "onboarding.completeError"));
      setCompleting(false);
    }
  }, [router, fetchState, lang]);

  // ── Loading ──
  if (loading && !state) {
    return (
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem", color: "#f0f6ff" }}>{t(lang, "onboarding.gettingStarted")}</h1>
        <p style={{ color: "#94a3b8", fontSize: "0.9rem" }}>{t(lang, "onboarding.loadingProgress")}</p>
      </div>
    );
  }

  // ── Error ──
  if (error && !state) {
    return (
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem", color: "#f0f6ff" }}>{t(lang, "onboarding.gettingStarted")}</h1>
        <div style={{ ...glassCard, borderColor: "rgba(248, 113, 113, 0.3)" }}>
          <p style={{ color: "#f87171", fontSize: "0.9rem", margin: 0 }}>{error}</p>
        </div>
      </div>
    );
  }

  if (!state) return null;

  const currentStepIndex = findCurrentStepIndex(state);

  // All wizard steps complete but onboarding_completed not yet set — show the completion hand-off panel
  const showReadiness = currentStepIndex === -1;
  const currentStep = showReadiness ? null : ONBOARDING_STEPS[currentStepIndex];
  // Canonical 5-step model (types/onboarding.ts): 1 Create account · 2 Select plan · 3 Complete profile ·
  // 4 Open workspace · 5 Get started. The wizard here covers steps 2-3; the completion panel below is step 4.
  const totalSteps = 5;
  const stepNumber = currentStep ? currentStep.stepNumber : 4;

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem", color: "#f0f6ff" }}>{t(lang, "onboarding.gettingStarted")}</h1>
      <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1.5rem" }}>
        {t(lang, "onboarding.stepSummary", { step: stepNumber, total: totalSteps })}
      </p>

      <div
        className="onboarding-shell-grid"
        style={{
          display: "grid",
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
                    {t(lang, "onboarding.profileTitle")}
                  </h2>
                  <p style={{ color: "#86efac", fontSize: "0.9rem" }}>{t(lang, "onboarding.profileComplete")}</p>
                </div>
              )}
            </>
          )}
          {showReadiness && !state.onboarding_completed && (
            <div>
              <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
                {t(lang, "onboarding.accountReady")}
              </h2>
              <p style={{ color: "#b7c5dd", fontSize: "0.9rem", lineHeight: 1.6, marginBottom: "1.25rem" }}>
                {t(lang, "onboarding.accountReadyBody")}
              </p>
              {/* Primary path: open the hosted workspace journey (this is the beta's main flow). */}
              <Link
                href="/onboarding/hosted"
                style={{
                  display: "inline-block",
                  padding: "0.55rem 1.35rem",
                  borderRadius: 999,
                  background: "linear-gradient(135deg, #2979ff 0%, #3fe0ff 50%, #2979ff 100%)",
                  color: "#ffffff",
                  fontSize: "0.9rem",
                  fontWeight: 600,
                  textDecoration: "none",
                  boxShadow: "0 10px 30px rgba(37, 99, 235, 0.45)",
                }}
              >
                {t(lang, "hostedStatus.continueSetup")}
              </Link>
              {/* Traditional path stays reachable for customers who manage their own broker account. */}
              <div style={{ marginTop: "1.25rem", paddingTop: "1rem", borderTop: "1px solid rgba(74,179,255,0.12)" }}>
                <p style={{ color: "#94a3b8", fontSize: "0.85rem", lineHeight: 1.6, margin: "0 0 0.6rem" }}>
                  {t(lang, "onboarding.selfManagedBody")}
                </p>
                <Button variant="secondary" onClick={handleComplete} disabled={completing} style={{ fontSize: "0.85rem" }}>
                  {completing ? t(lang, "onboarding.finishing") : t(lang, "onboarding.connectOwnBroker")}
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
