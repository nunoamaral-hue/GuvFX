"use client";

import { useState } from "react";
import { ActionRequestModal } from "@/components/ActionRequestModal";

// ─────────────────────────────────────────────────────────────────────
// Product subscription tiers — product vocabulary ONLY
// (No infrastructure terms on this page)
// ─────────────────────────────────────────────────────────────────────

type ProductPlan = {
  name: string;
  price: string;
  period: string;
  subtitle: string;
  features: string[];
  highlighted?: boolean;
  badge?: string;
};

const PRODUCT_PLANS: ProductPlan[] = [
  {
    name: "Starter Trial",
    price: "$0",
    period: "for 30 days",
    subtitle: "Explore the full platform before you commit.",
    badge: "30-day trial",
    features: [
      "Full dashboard access",
      "Create & edit strategies",
      "Run backtests (limited)",
      "Marketplace browsing",
      "Community support",
    ],
  },
  {
    name: "Standard",
    price: "$29",
    period: "/month",
    subtitle: "For active traders building and testing strategies.",
    highlighted: true,
    badge: "Most Popular",
    features: [
      "Everything in Starter",
      "Unlimited backtests",
      "Strategy marketplace access",
      "Priority execution queue",
      "Email support",
    ],
  },
  {
    name: "Pro",
    price: "$79",
    period: "/month",
    subtitle: "For serious traders who need full automation.",
    badge: "Best Value",
    features: [
      "Everything in Standard",
      "Live automation",
      "Multi-account support",
      "Advanced analytics",
      "Priority support",
    ],
  },
  {
    name: "Advanced",
    price: "$149",
    period: "/month",
    subtitle: "For power users and institutional workflows.",
    features: [
      "Everything in Pro",
      "Custom execution hooks",
      "API access",
      "Dedicated account manager",
      "SLA guarantees",
    ],
  },
];

// ─────────────────────────────────────────────────────────────────────
// Shared styles
// ─────────────────────────────────────────────────────────────────────

const glassCard: React.CSSProperties = {
  borderRadius: 16,
  border: "1px solid rgba(74, 179, 255, 0.12)",
  background:
    "linear-gradient(135deg, rgba(10, 15, 40, 0.95) 0%, rgba(5, 8, 22, 0.98) 100%)",
  boxShadow:
    "0 8px 32px rgba(0, 0, 0, 0.4), 0 0 60px rgba(30, 111, 255, 0.04)",
  padding: "1.5rem",
  display: "flex",
  flexDirection: "column" as const,
};

// ─────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────

export default function BillingPage() {
  const [modalOpen, setModalOpen] = useState(false);
  const [modalContext, setModalContext] = useState("");

  // Current plan cannot be reliably determined from frontend — show fallback
  const currentPlanName: string | null = null;

  const openModal = (planName: string) => {
    setModalContext(planName);
    setModalOpen(true);
  };

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>
        Billing &amp; Plans
      </h1>
      <p
        style={{
          fontSize: "0.9rem",
          color: "#b7c5dd",
          marginBottom: "1.5rem",
        }}
      >
        Manage your GuvFX product subscription.
      </p>

      {/* Current plan banner */}
      <div
        style={{
          ...glassCard,
          marginBottom: "1.5rem",
          padding: "1rem 1.25rem",
          flexDirection: "row" as const,
          alignItems: "center",
          justifyContent: "space-between",
          gap: "1rem",
          flexWrap: "wrap" as const,
        }}
      >
        <div>
          <span
            style={{
              fontSize: "0.8rem",
              color: "#94a3b8",
              textTransform: "uppercase",
              letterSpacing: "0.06em",
              fontWeight: 600,
            }}
          >
            Current plan
          </span>
          <div style={{ fontSize: "1.1rem", color: "#e9f4ff", fontWeight: 600, marginTop: "0.15rem" }}>
            {currentPlanName ?? "Current plan information unavailable."}
          </div>
        </div>
      </div>

      {/* Plan cards grid */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
          gap: "1.25rem",
          marginBottom: "2rem",
        }}
      >
        {PRODUCT_PLANS.map((plan) => (
          <div
            key={plan.name}
            style={{
              ...glassCard,
              border: plan.highlighted
                ? "1px solid rgba(59, 130, 246, 0.35)"
                : "1px solid rgba(74, 179, 255, 0.12)",
              position: "relative",
            }}
          >
            {/* Badge */}
            {plan.badge && (
              <span
                style={{
                  display: "inline-block",
                  fontSize: "0.72rem",
                  fontWeight: 600,
                  padding: "0.2rem 0.65rem",
                  borderRadius: 999,
                  background: plan.highlighted
                    ? "rgba(59, 130, 246, 0.2)"
                    : "rgba(234, 179, 8, 0.15)",
                  color: plan.highlighted ? "#93c5fd" : "#fbbf24",
                  border: `1px solid ${plan.highlighted ? "rgba(59, 130, 246, 0.3)" : "rgba(234, 179, 8, 0.3)"}`,
                  marginBottom: "0.75rem",
                  width: "fit-content",
                }}
              >
                {plan.badge}
              </span>
            )}

            {/* Plan name */}
            <h3 style={{ fontSize: "1.15rem", fontWeight: 700, margin: "0 0 0.5rem", color: "#e9f4ff" }}>
              {plan.name}
            </h3>

            {/* Price */}
            <div style={{ marginBottom: "0.5rem" }}>
              <span style={{ fontSize: "2rem", fontWeight: 700, color: "#93c5fd" }}>
                {plan.price}
              </span>
              <span style={{ fontSize: "0.85rem", color: "#64748b", marginLeft: 4 }}>
                {plan.period}
              </span>
            </div>

            {/* Subtitle */}
            <p style={{ fontSize: "0.85rem", color: "#8fa0b7", lineHeight: 1.5, margin: "0 0 1rem" }}>
              {plan.subtitle}
            </p>

            {/* Features */}
            <ul style={{ listStyle: "none", padding: 0, margin: "0 0 1.25rem", flex: 1 }}>
              {plan.features.map((f) => (
                <li
                  key={f}
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: 8,
                    fontSize: "0.85rem",
                    color: "#c2d5ff",
                    marginBottom: "0.4rem",
                  }}
                >
                  <span style={{ color: "#4ade80", marginTop: 1 }}>✓</span>
                  {f}
                </li>
              ))}
            </ul>

            {/* CTA */}
            <button
              onClick={() => openModal(plan.name)}
              style={{
                marginTop: "auto",
                width: "100%",
                padding: "0.65rem 1rem",
                borderRadius: 999,
                border: plan.highlighted
                  ? "none"
                  : "1px solid rgba(255, 255, 255, 0.15)",
                background: plan.highlighted
                  ? "linear-gradient(135deg, #1e6fff 0%, #00d4ff 50%, #1e6fff 100%)"
                  : "transparent",
                color: plan.highlighted ? "#fff" : "#c2d5ff",
                fontSize: "0.9rem",
                fontWeight: 600,
                cursor: "pointer",
                boxShadow: plan.highlighted
                  ? "0 8px 24px rgba(30, 111, 255, 0.3)"
                  : "none",
              }}
            >
              {plan.highlighted ? "Upgrade" : "Change Plan"}
            </button>
          </div>
        ))}
      </div>

      {/* Modal */}
      <ActionRequestModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title="Plan change request"
        contextLine={modalContext ? `Requested plan: ${modalContext}` : undefined}
        confirmationBody="Your request will be recorded once account plan management is enabled."
      />
    </div>
  );
}
