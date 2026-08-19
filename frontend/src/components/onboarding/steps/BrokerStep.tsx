"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { apiFetch } from "@/lib/api";
import type { BrokerPartner } from "@/types/onboarding";
import { useLang } from "@/components/AppShell";
import { t } from "@/lib/i18n";

/**
 * Broker Connection — informational panel shown above AccountConnectionStep.
 *
 * Displays partner brokers (if any) and links to the Accounts page.
 * This component does NOT gate onboarding progression — the
 * AccountConnectionStep below it handles the actual flag.
 */
export function BrokerStep() {
  const lang = useLang();
  const [brokers, setBrokers] = useState<BrokerPartner[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [referralSent, setReferralSent] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const data = await apiFetch<BrokerPartner[]>("/api/onboarding/brokers/", {});
        setBrokers(data);
      } catch {
        setError(t(lang, "onboarding.broker.loadError"));
      } finally {
        setLoading(false);
      }
    })();
  }, [lang]);

  const handleReferral = async (broker: BrokerPartner) => {
    try {
      await apiFetch("/api/onboarding/brokers/referral/", {
        method: "POST",
        body: JSON.stringify({ broker_code: broker.broker_code }),
      });
      setReferralSent(broker.broker_code);
      if (broker.referral_url) {
        window.open(broker.referral_url, "_blank", "noopener,noreferrer");
      }
    } catch {
      // Non-blocking — referral tracking is best-effort
    }
  };

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        {t(lang, "onboarding.broker.title")}
      </h2>
      <p style={{ color: "#b7c5dd", fontSize: "0.9rem", marginBottom: "1.25rem", lineHeight: 1.6 }}>
        {t(lang, "onboarding.broker.body")}
      </p>

      {loading && (
        <p style={{ color: "#94a3b8", fontSize: "0.85rem" }}>{t(lang, "onboarding.broker.loading")}</p>
      )}

      {error && (
        <p style={{ color: "#f87171", fontSize: "0.85rem", marginBottom: "0.75rem" }}>{error}</p>
      )}

      {brokers.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", marginBottom: "1.25rem" }}>
          {brokers.map((broker) => (
            <div
              key={broker.id}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "0.75rem 1rem",
                borderRadius: 10,
                border: "1px solid rgba(74, 179, 255, 0.1)",
                background: "rgba(255, 255, 255, 0.02)",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
                <span style={{ fontSize: "0.92rem", fontWeight: 600, color: "#e9f4ff" }}>
                  {broker.name}
                </span>
                <Badge color="blue">{broker.broker_code}</Badge>
                {referralSent === broker.broker_code && (
                  <Badge color="green">{t(lang, "onboarding.broker.referralTracked")}</Badge>
                )}
              </div>
              {broker.referral_url && (
                <Button
                  variant="secondary"
                  onClick={() => handleReferral(broker)}
                  style={{ fontSize: "0.8rem", padding: "0.35rem 0.8rem" }}
                >
                  {t(lang, "onboarding.broker.openAccount")}
                </Button>
              )}
            </div>
          ))}
        </div>
      )}

      <div
        style={{
          padding: "0.75rem 1rem",
          borderRadius: 10,
          border: "1px solid rgba(74, 179, 255, 0.1)",
          background: "rgba(74, 179, 255, 0.04)",
          marginBottom: "1.25rem",
        }}
      >
        <p style={{ color: "#b7c5dd", fontSize: "0.85rem", margin: 0 }}>
          {t(lang, "onboarding.broker.existingPrefix")} {" "}
          <Link href="/accounts" style={{ color: "#4ab3ff", textDecoration: "none" }}>
            {t(lang, "onboarding.broker.accountsLink")}
          </Link>{" "}
          {t(lang, "onboarding.broker.existingSuffix")}
        </p>
      </div>
    </div>
  );
}
