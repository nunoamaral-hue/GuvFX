"use client";

import { useState } from "react";
import { ActionRequestModal } from "@/components/ActionRequestModal";

// ─────────────────────────────────────────────────────────────────────
// Infrastructure hosting tiers — infrastructure vocabulary ONLY
// (No product plan names on this page)
// ─────────────────────────────────────────────────────────────────────

type HostingTier = {
  name: string;
  subtitle: string;
  specs: string[];
  tone: string;
  badge?: string;
  recommended?: boolean;
};

const HOSTING_TIERS: HostingTier[] = [
  {
    name: "Session",
    subtitle: "Ephemeral MT5 instance — active only while you are logged in.",
    tone: "#22c55e",
    badge: "Default",
    specs: [
      "1 CPU core",
      "1 GB RAM",
      "5 GB disk",
      "1 MT5 instance",
      "No 24/7 automation",
      "Resets on logout",
    ],
  },
  {
    name: "Dedicated",
    subtitle: "Always-on dedicated VPS for uninterrupted strategy execution.",
    tone: "#38bdf8",
    recommended: true,
    badge: "Recommended",
    specs: [
      "2 CPU cores",
      "4 GB RAM",
      "40 GB disk",
      "Up to 2 MT5 instances",
      "24/7 automation",
      "Persistent storage",
    ],
  },
  {
    name: "Managed Shared",
    subtitle: "Managed shared infrastructure for multi-strategy portfolios.",
    tone: "#a855f7",
    specs: [
      "4 CPU cores",
      "8 GB RAM",
      "80 GB disk",
      "Up to 10 MT5 instances",
      "24/7 automation",
      "Shared pool resources",
    ],
  },
  {
    name: "Shared Pool",
    subtitle: "Elastic shared pool for teams and high-volume operations.",
    tone: "#f97316",
    specs: [
      "Elastic CPU allocation",
      "Elastic memory allocation",
      "Shared disk pool",
      "Unlimited MT5 instances",
      "24/7 automation",
      "Multi-user support",
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

export default function HostingPage() {
  const [modalOpen, setModalOpen] = useState(false);
  const [modalContext, setModalContext] = useState("");

  // Current hosting cannot be reliably determined from frontend — show fallback
  const currentHostingType: string | null = null;

  const openModal = (tierName: string) => {
    setModalContext(tierName);
    setModalOpen(true);
  };

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Hosting</h1>
      <p
        style={{
          fontSize: "0.9rem",
          color: "#b7c5dd",
          marginBottom: "1.5rem",
        }}
      >
        Manage your MT5 hosting infrastructure.
      </p>

      {/* Current hosting banner */}
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
            Current hosting
          </span>
          <div style={{ fontSize: "1.1rem", color: "#e9f4ff", fontWeight: 600, marginTop: "0.15rem" }}>
            {currentHostingType ?? "Current hosting information unavailable."}
          </div>
        </div>
      </div>

      {/* Hosting tier cards */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
          gap: "1.25rem",
          marginBottom: "2rem",
        }}
      >
        {HOSTING_TIERS.map((tier) => (
          <div
            key={tier.name}
            style={{
              ...glassCard,
              border: tier.recommended
                ? `1px solid rgba(56, 189, 248, 0.3)`
                : "1px solid rgba(74, 179, 255, 0.12)",
            }}
          >
            {/* Badge */}
            {tier.badge && (
              <span
                style={{
                  display: "inline-block",
                  fontSize: "0.72rem",
                  fontWeight: 600,
                  padding: "0.2rem 0.65rem",
                  borderRadius: 999,
                  background: `${tier.tone}18`,
                  color: tier.tone,
                  border: `1px solid ${tier.tone}40`,
                  marginBottom: "0.75rem",
                  width: "fit-content",
                }}
              >
                {tier.badge}
              </span>
            )}

            {/* Tier name */}
            <h3
              style={{
                fontSize: "1.15rem",
                fontWeight: 700,
                margin: "0 0 0.5rem",
                color: tier.tone,
              }}
            >
              {tier.name}
            </h3>

            {/* Subtitle */}
            <p
              style={{
                fontSize: "0.85rem",
                color: "#8fa0b7",
                lineHeight: 1.5,
                margin: "0 0 1rem",
              }}
            >
              {tier.subtitle}
            </p>

            {/* Specs */}
            <ul style={{ listStyle: "none", padding: 0, margin: "0 0 1.25rem", flex: 1 }}>
              {tier.specs.map((s) => (
                <li
                  key={s}
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: 8,
                    fontSize: "0.85rem",
                    color: "#c2d5ff",
                    marginBottom: "0.4rem",
                  }}
                >
                  <span style={{ color: tier.tone, marginTop: 1 }}>✓</span>
                  {s}
                </li>
              ))}
            </ul>

            {/* CTA */}
            <button
              onClick={() => openModal(tier.name)}
              style={{
                marginTop: "auto",
                width: "100%",
                padding: "0.65rem 1rem",
                borderRadius: 999,
                border: tier.recommended
                  ? "none"
                  : "1px solid rgba(255, 255, 255, 0.15)",
                background: tier.recommended
                  ? "linear-gradient(135deg, #1e6fff 0%, #00d4ff 50%, #1e6fff 100%)"
                  : "transparent",
                color: tier.recommended ? "#fff" : "#c2d5ff",
                fontSize: "0.9rem",
                fontWeight: 600,
                cursor: "pointer",
                boxShadow: tier.recommended
                  ? "0 8px 24px rgba(30, 111, 255, 0.3)"
                  : "none",
              }}
            >
              {tier.recommended ? "Select Hosting" : "Change Hosting"}
            </button>
          </div>
        ))}
      </div>

      {/* Modal */}
      <ActionRequestModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title="Hosting change request"
        contextLine={modalContext ? `Requested hosting: ${modalContext}` : undefined}
        confirmationBody="Your request will be recorded once hosting change actions are enabled."
      />
    </div>
  );
}
