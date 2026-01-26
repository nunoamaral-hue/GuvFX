"use client";

import { useState } from "react";
import Link from "next/link";
import { type Lang, t } from "@/lib/i18n";

const LS_ONBOARDING_DISMISSED_KEY = "guvfx_onboarding_dismissed";

type OnboardingChecklistProps = {
  lang: Lang;
};

/**
 * First-time user onboarding checklist for the Dashboard.
 * - Dismissible via localStorage flag
 * - Non-advisory language only
 * - Never auto-trades or recommends strategies
 */
export function OnboardingChecklist({ lang }: OnboardingChecklistProps) {
  // Lazy initialization: read from localStorage once on first render (client-side)
  const [dismissed, setDismissed] = useState<boolean>(() => {
    if (typeof window === "undefined") return true; // SSR: hide by default
    return window.localStorage.getItem(LS_ONBOARDING_DISMISSED_KEY) === "true";
  });

  const handleDismiss = () => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(LS_ONBOARDING_DISMISSED_KEY, "true");
    }
    setDismissed(true);
  };

  if (dismissed) return null;

  const steps = [
    { key: "onboarding.step1", href: "/accounts" },
    { key: "onboarding.step2", href: "/strategies/create" },
    { key: "onboarding.step3", href: "/backtests" },
    { key: "onboarding.step4", href: null }, // No link, just guidance
  ];

  return (
    <div
      style={{
        marginBottom: "1.5rem",
        padding: "1.25rem",
        borderRadius: 12,
        border: "1px solid rgba(74, 179, 255, 0.2)",
        background: "rgba(74, 179, 255, 0.04)",
      }}
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "1rem",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          <RocketIcon />
          <h3
            style={{
              margin: 0,
              fontSize: "1rem",
              fontWeight: 600,
              color: "#e5f4ff",
            }}
          >
            {t(lang, "onboarding.title")}
          </h3>
        </div>
      </div>

      {/* Checklist */}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "0.6rem",
          marginBottom: "1rem",
        }}
      >
        {steps.map((step, index) => (
          <div
            key={step.key}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.6rem",
            }}
          >
            <span
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                width: 22,
                height: 22,
                borderRadius: "50%",
                border: "1px solid rgba(74, 179, 255, 0.3)",
                background: "rgba(74, 179, 255, 0.1)",
                fontSize: "0.7rem",
                fontWeight: 600,
                color: "#4ab3ff",
              }}
            >
              {index + 1}
            </span>
            {step.href ? (
              <Link
                href={step.href}
                style={{
                  fontSize: "0.85rem",
                  color: "#93c5fd",
                  textDecoration: "none",
                }}
              >
                {t(lang, step.key)}
              </Link>
            ) : (
              <span style={{ fontSize: "0.85rem", color: "#94a3b8" }}>
                {t(lang, step.key)}
              </span>
            )}
          </div>
        ))}
      </div>

      {/* Footer note */}
      <p
        style={{
          margin: 0,
          marginBottom: "0.75rem",
          fontSize: "0.75rem",
          color: "#64748b",
          lineHeight: 1.5,
        }}
      >
        {t(lang, "onboarding.footerNote")}
      </p>

      {/* Dismiss button */}
      <button
        type="button"
        onClick={handleDismiss}
        style={{
          padding: "0.4rem 0.8rem",
          borderRadius: 6,
          border: "1px solid rgba(255, 255, 255, 0.1)",
          background: "rgba(255, 255, 255, 0.04)",
          color: "#94a3b8",
          fontSize: "0.8rem",
          cursor: "pointer",
          transition: "background 150ms ease, border-color 150ms ease",
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.background = "rgba(255, 255, 255, 0.08)";
          e.currentTarget.style.borderColor = "rgba(255, 255, 255, 0.15)";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.background = "rgba(255, 255, 255, 0.04)";
          e.currentTarget.style.borderColor = "rgba(255, 255, 255, 0.1)";
        }}
      >
        {t(lang, "onboarding.dismiss")}
      </button>
    </div>
  );
}

// Simple rocket icon for the header
function RocketIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="#4ab3ff"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" />
      <path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" />
      <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0" />
      <path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" />
    </svg>
  );
}
