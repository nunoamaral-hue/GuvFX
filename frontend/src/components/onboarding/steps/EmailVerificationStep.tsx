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
};

export function EmailVerificationStep({ state, onComplete }: Props) {
  const lang = useLang();
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [token, setToken] = useState("");
  const [tokenSent, setTokenSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (state.email_verified) {
    return (
      <div>
        <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
          {t(lang, "onboarding.email.title")}
        </h2>
        <p style={{ color: "#86efac", fontSize: "0.9rem" }}>{t(lang, "onboarding.email.verified")}</p>
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
      setError(customerSafeError(err, t(lang, "onboarding.email.sendError")));
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
      setError(customerSafeError(err, t(lang, "onboarding.email.verifyError"), [
        { match: /expired/i, message: t(lang, "onboarding.email.expired") },
        { match: /already.*used|used/i, message: t(lang, "onboarding.email.used") },
        { match: /invalid/i, message: t(lang, "onboarding.email.invalid") },
      ]));
    } finally {
      setVerifying(false);
    }
  };

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        {t(lang, "onboarding.email.verifyTitle")}
      </h2>
      <p style={{ color: "#b7c5dd", fontSize: "0.9rem", marginBottom: "1.25rem", lineHeight: 1.6 }}>
        {t(lang, "onboarding.email.verifyBody")}
      </p>

      {!tokenSent ? (
        <Button onClick={handleSend} disabled={sending}>
          {sending ? t(lang, "onboarding.email.sending") : t(lang, "onboarding.email.sendCode")}
        </Button>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <p style={{ color: "#86efac", fontSize: "0.85rem" }}>
            {t(lang, "onboarding.email.sent")}
          </p>
          <input
            type="text"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={t(lang, "onboarding.email.placeholder")}
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
          <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
            <Button onClick={handleVerify} disabled={verifying || !token.trim()}>
              {verifying ? t(lang, "onboarding.email.verifying") : t(lang, "onboarding.email.verify")}
            </Button>
            <Button variant="secondary" onClick={handleSend} disabled={sending}>
              {sending ? t(lang, "onboarding.email.resending") : t(lang, "onboarding.email.resend")}
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
