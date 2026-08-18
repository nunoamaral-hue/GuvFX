"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";
import type { OnboardingState } from "@/types/onboarding";
import { customerSafeError } from "@/lib/customer-safe-error";
import { useLang } from "@/components/AppShell";
import { t } from "@/lib/i18n";

type Props = {
  state: OnboardingState;
  onComplete: () => void;
  onSkip: () => void;
};

export function TwoFactorStep({ state, onComplete, onSkip }: Props) {
  const lang = useLang();
  const [setting, setSetting] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [setupData, setSetupData] = useState<{ provisioning_uri: string; secret: string } | null>(null);
  const [otp, setOtp] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (state.two_factor_enabled) {
    return (
      <div>
        <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
          {t(lang, "onboarding.twoFactor.title")}
        </h2>
        <p style={{ color: "#86efac", fontSize: "0.9rem" }}>{t(lang, "onboarding.twoFactor.enabled")}</p>
      </div>
    );
  }

  const handleSetup = async () => {
    setSetting(true);
    setError(null);
    try {
      const data = await apiFetch<{ provisioning_uri: string; secret: string }>(
        "/api/onboarding/2fa/setup/",
        { method: "POST" },
      );
      setSetupData(data);
    } catch (err: unknown) {
      setError(customerSafeError(err, t(lang, "onboarding.twoFactor.setupError")));
    } finally {
      setSetting(false);
    }
  };

  const handleVerify = async () => {
    if (otp.length !== 6) return;
    setVerifying(true);
    setError(null);
    try {
      await apiFetch("/api/onboarding/2fa/verify/", {
        method: "POST",
        body: JSON.stringify({ otp_code: otp }),
      });
      onComplete();
    } catch (err: unknown) {
      setError(customerSafeError(err, t(lang, "onboarding.twoFactor.invalid"), [
        { match: /invalid.*(otp|code)/i, message: t(lang, "onboarding.twoFactor.invalid") },
      ]));
    } finally {
      setVerifying(false);
    }
  };

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        {t(lang, "onboarding.twoFactor.title")}
      </h2>
      <p style={{ color: "#b7c5dd", fontSize: "0.9rem", marginBottom: "1.25rem", lineHeight: 1.6 }}>
        {t(lang, "onboarding.twoFactor.body")}
      </p>

      {!setupData ? (
        <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
          <Button onClick={handleSetup} disabled={setting}>
            {setting ? t(lang, "onboarding.twoFactor.settingUp") : t(lang, "onboarding.twoFactor.setup")}
          </Button>
          <Button variant="secondary" onClick={onSkip}>
            {t(lang, "onboarding.twoFactor.skip")}
          </Button>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <p style={{ color: "#fbbf24", fontSize: "0.85rem", fontWeight: 600 }}>
            {t(lang, "onboarding.twoFactor.scan")}
          </p>
          <div
            style={{
              padding: "0.75rem",
              borderRadius: 8,
              background: "rgba(255, 255, 255, 0.04)",
              border: "1px solid rgba(74, 179, 255, 0.15)",
              fontFamily: "monospace",
              fontSize: "0.82rem",
              color: "#e9f4ff",
              wordBreak: "break-all",
              maxWidth: 480,
            }}
          >
            {setupData.secret}
          </div>
          <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap" }}>
            <input
              type="text"
              value={otp}
              onChange={(e) => setOtp(e.target.value.replace(/\D/g, "").slice(0, 6))}
              placeholder={t(lang, "onboarding.twoFactor.placeholder")}
              maxLength={6}
              style={{
                padding: "0.6rem 0.85rem",
                borderRadius: 8,
                border: "1px solid rgba(74, 179, 255, 0.2)",
                background: "rgba(255, 255, 255, 0.04)",
                color: "#e9f4ff",
                fontSize: "0.9rem",
                outline: "none",
                width: 180,
                letterSpacing: "0.2em",
                textAlign: "center",
              }}
            />
            <Button onClick={handleVerify} disabled={verifying || otp.length !== 6}>
              {verifying ? t(lang, "onboarding.twoFactor.verifying") : t(lang, "onboarding.twoFactor.verify")}
            </Button>
          </div>
        </div>
      )}

      {error && (
        <p style={{ color: "#f87171", fontSize: "0.85rem", marginTop: "0.75rem" }}>{error}</p>
      )}
    </div>
  );
}
