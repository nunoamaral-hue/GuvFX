"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";
import type { OnboardingState } from "@/types/onboarding";

type Props = {
  state: OnboardingState;
  onComplete: () => void;
};

export function EmailVerificationStep({ state, onComplete }: Props) {
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [token, setToken] = useState("");
  const [tokenSent, setTokenSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (state.email_verified) {
    return (
      <div>
        <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
          Email Verification
        </h2>
        <p style={{ color: "#86efac", fontSize: "0.9rem" }}>Your email has been verified.</p>
      </div>
    );
  }

  const handleSend = async () => {
    setSending(true);
    setError(null);
    try {
      await apiFetch("/api/onboarding/email/send-verification/", { method: "POST" });
      setTokenSent(true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to send verification email.");
    } finally {
      setSending(false);
    }
  };

  const handleVerify = async () => {
    if (!token.trim()) return;
    setVerifying(true);
    setError(null);
    try {
      await apiFetch("/api/onboarding/email/verify/", {
        method: "POST",
        body: JSON.stringify({ token: token.trim() }),
      });
      onComplete();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Verification failed.");
    } finally {
      setVerifying(false);
    }
  };

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        Verify Your Email
      </h2>
      <p style={{ color: "#b7c5dd", fontSize: "0.9rem", marginBottom: "1.25rem", lineHeight: 1.6 }}>
        We need to verify your email address to proceed. Click below to receive a verification code.
      </p>

      {!tokenSent ? (
        <Button onClick={handleSend} disabled={sending}>
          {sending ? "Sending..." : "Send Verification Code"}
        </Button>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <p style={{ color: "#86efac", fontSize: "0.85rem" }}>
            Verification code sent. Check your email and enter the code below.
          </p>
          <input
            type="text"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Enter verification code"
            style={{
              padding: "0.6rem 0.85rem",
              borderRadius: 8,
              border: "1px solid rgba(74, 179, 255, 0.2)",
              background: "rgba(255, 255, 255, 0.04)",
              color: "#e9f4ff",
              fontSize: "0.9rem",
              outline: "none",
              maxWidth: 400,
            }}
          />
          <div style={{ display: "flex", gap: "0.5rem" }}>
            <Button onClick={handleVerify} disabled={verifying || !token.trim()}>
              {verifying ? "Verifying..." : "Verify"}
            </Button>
            <Button variant="secondary" onClick={handleSend} disabled={sending}>
              {sending ? "Resending..." : "Resend Code"}
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
