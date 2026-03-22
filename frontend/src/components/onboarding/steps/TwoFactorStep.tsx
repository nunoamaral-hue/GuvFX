"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";
import type { OnboardingState } from "@/types/onboarding";

type Props = {
  state: OnboardingState;
  onComplete: () => void;
  onSkip: () => void;
};

export function TwoFactorStep({ state, onComplete, onSkip }: Props) {
  const [setting, setSetting] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [setupData, setSetupData] = useState<{ provisioning_uri: string; secret: string } | null>(null);
  const [otp, setOtp] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (state.two_factor_enabled) {
    return (
      <div>
        <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
          Two-Factor Authentication
        </h2>
        <p style={{ color: "#86efac", fontSize: "0.9rem" }}>2FA is enabled on your account.</p>
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
      setError(err instanceof Error ? err.message : "Failed to set up 2FA.");
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
      setError(err instanceof Error ? err.message : "Invalid code.");
    } finally {
      setVerifying(false);
    }
  };

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        Two-Factor Authentication
      </h2>
      <p style={{ color: "#b7c5dd", fontSize: "0.9rem", marginBottom: "1.25rem", lineHeight: 1.6 }}>
        Add an extra layer of security to your account with TOTP-based two-factor authentication.
        This step is optional — you can skip it and enable it later.
      </p>

      {!setupData ? (
        <div style={{ display: "flex", gap: "0.5rem" }}>
          <Button onClick={handleSetup} disabled={setting}>
            {setting ? "Setting up..." : "Set Up 2FA"}
          </Button>
          <Button variant="secondary" onClick={onSkip}>
            Skip for Now
          </Button>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <p style={{ color: "#fbbf24", fontSize: "0.85rem", fontWeight: 600 }}>
            Scan the QR code or enter the secret in your authenticator app:
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
          <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
            <input
              type="text"
              value={otp}
              onChange={(e) => setOtp(e.target.value.replace(/\D/g, "").slice(0, 6))}
              placeholder="Enter 6-digit code"
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
              {verifying ? "Verifying..." : "Verify"}
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
