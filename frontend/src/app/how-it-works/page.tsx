"use client";

import Link from "next/link";
import { AppShell, useLang } from "@/components/AppShell";
import { t } from "@/lib/i18n";

export default function HowItWorksPage() {
  const lang = useLang();

  return (
    <AppShell>
      <div style={{ maxWidth: 820, margin: "0 auto" }}>
        {/* ----------------------------------------------------------------
            TITLE + SUBTITLE
            ---------------------------------------------------------------- */}
        <h1
          style={{
            fontSize: "2rem",
            fontWeight: 700,
            marginBottom: "0.5rem",
            color: "#e5f4ff",
          }}
        >
          {t(lang, "howItWorks.title")}
        </h1>
        <p
          style={{
            fontSize: "0.95rem",
            color: "#8fa0b7",
            lineHeight: 1.6,
            marginBottom: "0.75rem",
            maxWidth: 650,
          }}
        >
          {t(lang, "howItWorks.subtitle")}
        </p>
        <p style={{ fontSize: "0.75rem", color: "#64748b", marginBottom: "2.5rem" }}>
          {t(lang, "legal.microDisclaimer")}
        </p>

        {/* ----------------------------------------------------------------
            SECTION 1: WHAT GUVFX PROVIDES
            ---------------------------------------------------------------- */}
        <section style={{ marginBottom: "2.5rem" }}>
          <SectionHeading text={t(lang, "howItWorks.sectionWhatIsTitle")} />
          <p
            style={{
              fontSize: "0.9rem",
              color: "#8fa0b7",
              lineHeight: 1.6,
              marginBottom: "1rem",
            }}
          >
            {t(lang, "howItWorks.sectionWhatIsBody")}
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
            <ToolItem
              icon={
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#4ab3ff" strokeWidth="2">
                  <rect x="3" y="3" width="18" height="18" rx="2" />
                  <path d="M8 12h8M12 8v8" />
                </svg>
              }
              text={t(lang, "howItWorks.toolDesign")}
            />
            <ToolItem
              icon={
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#4ab3ff" strokeWidth="2">
                  <path d="M3 3v18h18" />
                  <path d="M7 14l4-4 4 4 5-5" />
                </svg>
              }
              text={t(lang, "howItWorks.toolTest")}
            />
            <ToolItem
              icon={
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#4ab3ff" strokeWidth="2">
                  <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
                </svg>
              }
              text={t(lang, "howItWorks.toolExecute")}
            />
          </div>
        </section>

        {/* ----------------------------------------------------------------
            SECTION 2: WHAT GUVFX IS NOT
            ---------------------------------------------------------------- */}
        <section style={{ marginBottom: "2.5rem" }}>
          <SectionHeading text={t(lang, "howItWorks.sectionWhatNotTitle")} />
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: "0.6rem",
              padding: "1rem 1.25rem",
              borderRadius: 10,
              background: "rgba(255, 255, 255, 0.02)",
              border: "1px solid rgba(255, 255, 255, 0.06)",
            }}
          >
            <NotBullet text={t(lang, "howItWorks.bullet1")} />
            <NotBullet text={t(lang, "howItWorks.bullet2")} />
            <NotBullet text={t(lang, "howItWorks.bullet3")} />
            <NotBullet text={t(lang, "howItWorks.bullet4")} />
          </div>
        </section>

        {/* ----------------------------------------------------------------
            SECTION 3: CONTROL & TRANSPARENCY
            ---------------------------------------------------------------- */}
        <section style={{ marginBottom: "2.5rem" }}>
          <SectionHeading text={t(lang, "howItWorks.sectionControlTitle")} />
          <p
            style={{
              fontSize: "0.9rem",
              color: "#8fa0b7",
              lineHeight: 1.6,
              marginBottom: "1rem",
            }}
          >
            {t(lang, "howItWorks.sectionControlBody")}
          </p>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
              gap: "0.75rem",
            }}
          >
            <ControlCard
              icon={
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#4ab3ff" strokeWidth="2">
                  <circle cx="12" cy="12" r="10" />
                  <path d="M8 12h8" />
                </svg>
              }
              text={t(lang, "howItWorks.control1")}
            />
            <ControlCard
              icon={
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#4ab3ff" strokeWidth="2">
                  <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
                </svg>
              }
              text={t(lang, "howItWorks.control2")}
            />
            <ControlCard
              icon={
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#4ab3ff" strokeWidth="2">
                  <rect x="3" y="3" width="18" height="18" rx="2" />
                  <path d="M9 9l6 6M15 9l-6 6" />
                </svg>
              }
              text={t(lang, "howItWorks.control3")}
            />
            <ControlCard
              icon={
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#4ab3ff" strokeWidth="2">
                  <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                  <circle cx="12" cy="12" r="3" />
                </svg>
              }
              text={t(lang, "howItWorks.control4")}
            />
          </div>
        </section>

        {/* ----------------------------------------------------------------
            SECTION 4: SAFE WORKFLOW
            ---------------------------------------------------------------- */}
        <section style={{ marginBottom: "2.5rem" }}>
          <SectionHeading text={t(lang, "howItWorks.sectionWorkflowTitle")} />
          <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
            <WorkflowStep step={1} text={t(lang, "howItWorks.workflowStep1")} />
            <WorkflowStep step={2} text={t(lang, "howItWorks.workflowStep2")} />
            <WorkflowStep step={3} text={t(lang, "howItWorks.workflowStep3")} />
            <WorkflowStep step={4} text={t(lang, "howItWorks.workflowStep4")} />
          </div>
        </section>

        {/* ----------------------------------------------------------------
            SECTION 5: NEXT STEPS
            ---------------------------------------------------------------- */}
        <section
          style={{
            marginBottom: "2rem",
            padding: "1.5rem",
            borderRadius: 12,
            background: "rgba(74, 179, 255, 0.04)",
            border: "1px solid rgba(74, 179, 255, 0.12)",
          }}
        >
          <h3
            style={{
              fontSize: "1.1rem",
              fontWeight: 600,
              marginBottom: "1rem",
              color: "#e5f4ff",
            }}
          >
            {t(lang, "howItWorks.nextTitle")}
          </h3>
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: "0.75rem",
            }}
          >
            <CTALink href="/dashboard" label={t(lang, "howItWorks.ctaDashboard")} />
            <CTALink href="/strategies/create" label={t(lang, "howItWorks.ctaCreateStrategy")} />
            <CTALink href="/accounts" label={t(lang, "howItWorks.ctaLinkAccount")} />
          </div>
        </section>
      </div>
    </AppShell>
  );
}

// =============================================================================
// SUB-COMPONENTS
// =============================================================================

function SectionHeading({ text }: { text: string }) {
  return (
    <h2
      style={{
        fontSize: "1.25rem",
        fontWeight: 600,
        marginBottom: "0.75rem",
        color: "#e5f4ff",
        paddingBottom: "0.5rem",
        borderBottom: "1px solid rgba(74, 179, 255, 0.1)",
      }}
    >
      {text}
    </h2>
  );
}

function ToolItem({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
      <div
        style={{
          width: 32,
          height: 32,
          borderRadius: 7,
          background: "rgba(74, 179, 255, 0.08)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          flexShrink: 0,
        }}
      >
        {icon}
      </div>
      <span style={{ fontSize: "0.85rem", color: "#94a3b8", lineHeight: 1.4 }}>{text}</span>
    </div>
  );
}

function NotBullet({ text }: { text: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#ef4444" strokeWidth="2.5">
        <path d="M18 6L6 18M6 6l12 12" />
      </svg>
      <span style={{ fontSize: "0.85rem", color: "#94a3b8" }}>{text}</span>
    </div>
  );
}

function ControlCard({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <div
      style={{
        padding: "0.8rem 1rem",
        borderRadius: 8,
        background: "rgba(5, 8, 22, 0.5)",
        border: "1px solid rgba(74, 179, 255, 0.08)",
        display: "flex",
        alignItems: "center",
        gap: "0.6rem",
      }}
    >
      {icon}
      <span style={{ fontSize: "0.8rem", color: "#94a3b8", lineHeight: 1.4 }}>{text}</span>
    </div>
  );
}

function WorkflowStep({ step, text }: { step: number; text: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
      <span
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          width: 28,
          height: 28,
          borderRadius: "50%",
          border: "1px solid rgba(74, 179, 255, 0.3)",
          background: "rgba(74, 179, 255, 0.1)",
          fontSize: "0.75rem",
          fontWeight: 600,
          color: "#4ab3ff",
          flexShrink: 0,
        }}
      >
        {step}
      </span>
      <span style={{ fontSize: "0.85rem", color: "#94a3b8", lineHeight: 1.4 }}>{text}</span>
    </div>
  );
}

function CTALink({ href, label }: { href: string; label: string }) {
  return (
    <Link
      href={href}
      style={{
        padding: "0.5rem 1rem",
        borderRadius: 8,
        border: "1px solid rgba(74, 179, 255, 0.2)",
        background: "rgba(74, 179, 255, 0.06)",
        color: "#7eb8e0",
        fontSize: "0.85rem",
        fontWeight: 500,
        textDecoration: "none",
      }}
    >
      {label}
    </Link>
  );
}
