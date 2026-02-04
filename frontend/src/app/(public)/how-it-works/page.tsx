"use client";

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type Lang, detectLang, setLang as persistLang, t } from "@/lib/i18n";
import { LegalFooter } from "@/components/LegalFooter";

const API_BASE = "https://api.guvfx.com";

export default function HowItWorksPage() {
  const router = useRouter();

  // Language detection — standalone (no AppShell, no auth context)
  const [lang, setLangState] = useState<Lang>(() => {
    if (typeof window === "undefined") return "en";
    return detectLang();
  });

  // ---------------------------------------------------------------------------
  // Lightweight auth probe (best-effort, fail-closed).
  // This page is public — the check only decides CTA routing:
  //   authed  => navigate directly to app route (AuthGate will pass)
  //   !authed => route via /login?returnTo=<target>
  // ---------------------------------------------------------------------------
  const [isAuthed, setIsAuthed] = useState(false);

  useEffect(() => {
    let cancelled = false;

    // Skip on localhost — treat as not-authed (CTAs go to /login, harmless)
    const isLocalhost =
      typeof window !== "undefined" &&
      (window.location.hostname === "localhost" ||
        window.location.hostname === "127.0.0.1" ||
        window.location.hostname === "0.0.0.0");

    if (isLocalhost) return;

    fetch(`${API_BASE}/api/auth/me/`, { method: "GET", credentials: "include" })
      .then((res) => {
        if (!cancelled && res.ok) setIsAuthed(true);
      })
      .catch(() => {
        // Network error — stay false (fail closed)
      });

    return () => { cancelled = true; };
  }, []);

  /** Route CTA targets through login when not authenticated. */
  const navTo = useCallback(
    (target: string) => {
      if (isAuthed) {
        router.push(target);
      } else {
        router.push(`/login?reason=unauthenticated&returnTo=${encodeURIComponent(target)}`);
      }
    },
    [isAuthed, router],
  );

  const toggleLang = () => {
    const next: Lang = lang === "en" ? "ja" : "en";
    persistLang(next);
    setLangState(next);
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        width: "100%",
        display: "flex",
        flexDirection: "column",
        background:
          "radial-gradient(circle at 0 0, #12263f 0, #050816 40%, #050816 100%)",
        color: "#e5f4ff",
        fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
      }}
    >
      {/* ================================================================
          MINIMAL PUBLIC NAVBAR (matches landing page style)
          ================================================================ */}
      <nav
        style={{
          padding: "1rem 2rem",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          borderBottom: "1px solid rgba(74, 179, 255, 0.1)",
        }}
      >
        {/* Logo + Wordmark */}
        <Link
          href="/"
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.5rem",
            textDecoration: "none",
          }}
        >
          <div
            style={{
              width: 32,
              height: 32,
              borderRadius: 8,
              background: "linear-gradient(135deg, #2979ff 0%, #3fe0ff 100%)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontWeight: 700,
              fontSize: "0.9rem",
              color: "#fff",
            }}
          >
            G
          </div>
          <span
            style={{
              fontSize: "1.25rem",
              fontWeight: 600,
              background:
                "linear-gradient(120deg, #4ab3ff 0%, #7af0ff 40%, #4ab3ff 100%)",
              WebkitBackgroundClip: "text",
              WebkitTextFillColor: "transparent",
            }}
          >
            GuvFX
          </span>
        </Link>

        {/* Right: lang toggle + login */}
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          <button
            onClick={toggleLang}
            style={{
              padding: "0.4rem 0.8rem",
              borderRadius: 6,
              border: "1px solid rgba(255,255,255,0.15)",
              background: "transparent",
              color: "#c2d5ff",
              fontSize: "0.85rem",
              fontWeight: 500,
              cursor: "pointer",
            }}
          >
            {lang === "en" ? "日本語" : "EN"}
          </button>
          <button
            onClick={() => router.push("/login")}
            style={{
              padding: "0.5rem 1rem",
              borderRadius: 999,
              border: "1px solid rgba(255,255,255,0.15)",
              background: "transparent",
              color: "#c2d5ff",
              fontSize: "0.85rem",
              fontWeight: 500,
              cursor: "pointer",
            }}
          >
            {t(lang, "landing.login")}
          </button>
          <button
            onClick={() => router.push("/register")}
            style={{
              padding: "0.5rem 1rem",
              borderRadius: 999,
              border: "none",
              background: "linear-gradient(135deg, #1e6fff 0%, #00d4ff 50%, #1e6fff 100%)",
              color: "#fff",
              fontSize: "0.85rem",
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            {t(lang, "landing.getStarted")}
          </button>
        </div>
      </nav>

      {/* ================================================================
          PAGE CONTENT
          ================================================================ */}
      <main
        style={{
          flex: 1,
          padding: "3rem 2rem 4rem",
          maxWidth: 820,
          margin: "0 auto",
          width: "100%",
        }}
      >
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
            <CTAButton onClick={() => navTo("/dashboard")} label={t(lang, "howItWorks.ctaDashboard")} />
            <CTAButton onClick={() => navTo("/strategies/create")} label={t(lang, "howItWorks.ctaCreateStrategy")} />
            <CTAButton onClick={() => navTo("/accounts")} label={t(lang, "howItWorks.ctaLinkAccount")} />
          </div>
        </section>
      </main>

      {/* Legal Footer */}
      <LegalFooter lang={lang} />
    </div>
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

function CTAButton({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: "0.5rem 1rem",
        borderRadius: 8,
        border: "1px solid rgba(74, 179, 255, 0.2)",
        background: "rgba(74, 179, 255, 0.06)",
        color: "#7eb8e0",
        fontSize: "0.85rem",
        fontWeight: 500,
        cursor: "pointer",
      }}
    >
      {label}
    </button>
  );
}
