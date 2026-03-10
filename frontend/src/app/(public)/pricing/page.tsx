"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { type Lang, detectLang, setLang as persistLang } from "@/lib/i18n";
import { LegalFooter } from "@/components/LegalFooter";
import { LanguageDropdown } from "@/components/LanguageDropdown";

// ─────────────────────────────────────────────────────────────────────
// Plan definitions
// ─────────────────────────────────────────────────────────────────────
type Plan = {
  name: string;
  badge?: string;
  badgeColor: string;
  price: string;
  period: string;
  subtitle: string;
  features: string[];
  cta: string;
  highlight: boolean;
};

const PLANS: Plan[] = [
  {
    name: "Starter Trial",
    badge: "30-day trial",
    badgeColor: "#67e8f9",
    price: "$0",
    period: "for 30 days",
    subtitle: "Explore the full platform before you commit.",
    features: [
      "Full dashboard access",
      "Create & edit strategies",
      "Run backtests (limited)",
      "Marketplace browsing",
      "Community support",
    ],
    cta: "Start Trial",
    highlight: false,
  },
  {
    name: "Standard",
    badge: "Most Popular",
    badgeColor: "#93c5fd",
    price: "$29",
    period: "/month",
    subtitle: "For active traders building and testing strategies.",
    features: [
      "Everything in Starter",
      "Unlimited backtests",
      "Strategy marketplace access",
      "Priority execution queue",
      "Email support",
    ],
    cta: "Get Started",
    highlight: true,
  },
  {
    name: "Pro",
    badge: "Best Value",
    badgeColor: "#86efac",
    price: "$79",
    period: "/month",
    subtitle: "For serious traders who need full automation.",
    features: [
      "Everything in Standard",
      "Live automation",
      "Multi-account support",
      "Advanced analytics",
      "Priority support",
    ],
    cta: "Get Started",
    highlight: false,
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
    cta: "Contact Us",
    highlight: false,
    badgeColor: "#c4b5fd",
  },
];

// ─────────────────────────────────────────────────────────────────────
// Shared styles
// ─────────────────────────────────────────────────────────────────────
const glassCard: React.CSSProperties = {
  background: "rgba(5, 8, 22, 0.85)",
  borderRadius: 16,
  border: "1px solid rgba(74, 179, 255, 0.1)",
  padding: "2rem 1.75rem",
  display: "flex",
  flexDirection: "column",
};

const highlightCard: React.CSSProperties = {
  ...glassCard,
  border: "1px solid rgba(59, 130, 246, 0.4)",
  boxShadow: "0 0 40px rgba(59, 130, 246, 0.12), 0 12px 40px rgba(0,0,0,0.5)",
};

// ─────────────────────────────────────────────────────────────────────
// Main Page
// ─────────────────────────────────────────────────────────────────────
export default function PricingPage() {
  const router = useRouter();

  const [lang, setLangState] = useState<Lang>(() => {
    if (typeof window === "undefined") return "en";
    return detectLang();
  });

  return (
    <div
      style={{
        minHeight: "100vh",
        width: "100%",
        background:
          "radial-gradient(circle at 0 0, #12263f 0, #050816 40%, #050816 100%)",
        color: "#e5f4ff",
        fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
      }}
    >
      {/* ── Navbar ── */}
      <nav
        style={{
          position: "fixed",
          top: 0,
          left: 0,
          right: 0,
          zIndex: 100,
          padding: "1rem 2rem",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          background: "rgba(5, 8, 22, 0.85)",
          backdropFilter: "blur(12px)",
          borderBottom: "1px solid rgba(74, 179, 255, 0.1)",
        }}
      >
        <div
          style={{ display: "flex", alignItems: "center", gap: "0.5rem", cursor: "pointer" }}
          onClick={() => router.push("/")}
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
              background: "linear-gradient(120deg, #4ab3ff 0%, #7af0ff 40%, #4ab3ff 100%)",
              WebkitBackgroundClip: "text",
              WebkitTextFillColor: "transparent",
            }}
          >
            GuvFX
          </span>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
          <LanguageDropdown
            lang={lang}
            onChange={(next) => { persistLang(next); setLangState(next); }}
            variant="compact"
          />
          <button
            onClick={() => router.push("/login")}
            style={{
              padding: "0.5rem 1rem",
              borderRadius: 999,
              border: "1px solid rgba(255,255,255,0.15)",
              background: "transparent",
              color: "#c2d5ff",
              fontSize: "0.9rem",
              fontWeight: 500,
              cursor: "pointer",
            }}
          >
            Log in
          </button>
          <button
            onClick={() => router.push("/register")}
            style={{
              padding: "0.5rem 1.25rem",
              borderRadius: 999,
              border: "none",
              background: "linear-gradient(135deg, #1e6fff 0%, #00d4ff 50%, #1e6fff 100%)",
              color: "#fff",
              fontSize: "0.9rem",
              fontWeight: 600,
              cursor: "pointer",
              boxShadow: "0 4px 20px rgba(30, 111, 255, 0.4)",
            }}
          >
            Get Started
          </button>
        </div>
      </nav>

      {/* ── Page Header ── */}
      <section
        style={{
          paddingTop: "8rem",
          paddingBottom: "3rem",
          textAlign: "center",
          maxWidth: 700,
          margin: "0 auto",
          padding: "8rem 2rem 3rem",
        }}
      >
        <h1
          style={{
            fontSize: "clamp(2rem, 5vw, 3rem)",
            fontWeight: 700,
            margin: "0 0 0.75rem",
            background: "linear-gradient(120deg, #4ab3ff 0%, #7af0ff 40%, #4ab3ff 100%)",
            WebkitBackgroundClip: "text",
            WebkitTextFillColor: "transparent",
          }}
        >
          Plans & Pricing
        </h1>
        <p style={{ fontSize: "1.05rem", color: "#9ab0c5", lineHeight: 1.6, margin: 0 }}>
          Start with a 30-day trial. Upgrade when you&apos;re ready.
        </p>
        <p style={{ fontSize: "0.75rem", color: "#4a5568", marginTop: "0.75rem" }}>
          Platform tools only — not financial advice.
        </p>
      </section>

      {/* ── Pricing Cards Grid ── */}
      <section style={{ maxWidth: 1200, margin: "0 auto", padding: "0 2rem 4rem" }}>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
            gap: "1.5rem",
            alignItems: "stretch",
          }}
        >
          {PLANS.map((plan) => (
            <div key={plan.name} style={plan.highlight ? highlightCard : glassCard}>
              {/* Badge */}
              {plan.badge && (
                <div
                  style={{
                    display: "inline-flex",
                    alignSelf: "flex-start",
                    padding: "0.2rem 0.65rem",
                    borderRadius: 999,
                    fontSize: "0.7rem",
                    fontWeight: 700,
                    letterSpacing: "0.02em",
                    color: plan.badgeColor,
                    background: `${plan.badgeColor}18`,
                    border: `1px solid ${plan.badgeColor}40`,
                    marginBottom: "0.75rem",
                  }}
                >
                  {plan.badge}
                </div>
              )}

              {/* Plan name */}
              <h3
                style={{
                  fontSize: "1.3rem",
                  fontWeight: 700,
                  margin: "0 0 0.25rem",
                  color: "#e9f4ff",
                }}
              >
                {plan.name}
              </h3>

              {/* Price */}
              <div style={{ display: "flex", alignItems: "baseline", gap: "0.25rem", marginBottom: "0.5rem" }}>
                <span
                  style={{
                    fontSize: "2.5rem",
                    fontWeight: 700,
                    background: plan.highlight
                      ? "linear-gradient(120deg, #4ab3ff, #7af0ff)"
                      : "none",
                    WebkitBackgroundClip: plan.highlight ? "text" : undefined,
                    WebkitTextFillColor: plan.highlight ? "transparent" : undefined,
                    color: plan.highlight ? undefined : "#e9f4ff",
                  }}
                >
                  {plan.price}
                </span>
                <span style={{ fontSize: "0.85rem", color: "#6b8299" }}>{plan.period}</span>
              </div>

              {/* Subtitle */}
              <p style={{ fontSize: "0.85rem", color: "#8fa0b7", lineHeight: 1.5, margin: "0 0 1.25rem" }}>
                {plan.subtitle}
              </p>

              {/* Features */}
              <ul
                style={{
                  listStyle: "none",
                  padding: 0,
                  margin: "0 0 1.5rem",
                  display: "flex",
                  flexDirection: "column",
                  gap: "0.55rem",
                  flex: 1,
                }}
              >
                {plan.features.map((f) => (
                  <li key={f} style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#4ab3ff" strokeWidth="2.5">
                      <path d="M20 6L9 17l-5-5" />
                    </svg>
                    <span style={{ fontSize: "0.85rem", color: "#c2d5ff" }}>{f}</span>
                  </li>
                ))}
              </ul>

              {/* CTA */}
              <button
                onClick={() => router.push("/register")}
                style={{
                  width: "100%",
                  padding: "0.75rem 1rem",
                  borderRadius: 999,
                  border: plan.highlight ? "none" : "1px solid rgba(255,255,255,0.15)",
                  background: plan.highlight
                    ? "linear-gradient(135deg, #1e6fff 0%, #00d4ff 50%, #1e6fff 100%)"
                    : "transparent",
                  color: plan.highlight ? "#fff" : "#c2d5ff",
                  fontSize: "0.95rem",
                  fontWeight: 600,
                  cursor: "pointer",
                  boxShadow: plan.highlight ? "0 8px 24px rgba(30, 111, 255, 0.35)" : "none",
                  marginTop: "auto",
                }}
              >
                {plan.cta}
              </button>
            </div>
          ))}
        </div>
      </section>

      {/* ── After the Trial ── */}
      <section
        style={{
          maxWidth: 800,
          margin: "0 auto",
          padding: "0 2rem 3rem",
        }}
      >
        <div
          style={{
            ...glassCard,
            padding: "1.75rem 2rem",
          }}
        >
          <h3
            style={{
              fontSize: "1.15rem",
              fontWeight: 700,
              margin: "0 0 0.5rem",
              color: "#e9f4ff",
            }}
          >
            After the trial
          </h3>
          <p
            style={{
              fontSize: "0.9rem",
              color: "#8fa0b7",
              lineHeight: 1.6,
              margin: "0 0 0.75rem",
            }}
          >
            Starter Trial is designed for 30 days. After trial expiry, access becomes
            restricted until you upgrade.
          </p>
          <ul
            style={{
              listStyle: "none",
              padding: 0,
              margin: 0,
              display: "flex",
              flexDirection: "column",
              gap: "0.45rem",
            }}
          >
            <InfoBullet text="Strategy creation and backtests become unavailable" />
            <InfoBullet text="Dashboards remain visible in read-only mode" />
            <InfoBullet text="Upgrade anytime to restore full access" />
          </ul>
        </div>
      </section>

      {/* ── Viewer Mode ── */}
      <section
        style={{
          maxWidth: 800,
          margin: "0 auto",
          padding: "0 2rem 4rem",
        }}
      >
        <div
          style={{
            ...glassCard,
            padding: "1.75rem 2rem",
          }}
        >
          <h3
            style={{
              fontSize: "1.15rem",
              fontWeight: 700,
              margin: "0 0 0.5rem",
              color: "#e9f4ff",
            }}
          >
            Viewer Mode
          </h3>
          <p
            style={{
              fontSize: "0.9rem",
              color: "#8fa0b7",
              lineHeight: 1.6,
              margin: "0 0 0.75rem",
            }}
          >
            Paid subscribers who do not renew move to Viewer Mode.
          </p>
          <ul
            style={{
              listStyle: "none",
              padding: 0,
              margin: 0,
              display: "flex",
              flexDirection: "column",
              gap: "0.45rem",
            }}
          >
            <InfoBullet text="Dashboards and analytics remain visible" />
            <InfoBullet text="Marketplace browsing available" />
            <InfoBullet text="No new backtests or automation" />
          </ul>
        </div>
      </section>

      {/* ── Bottom CTA ── */}
      <section style={{ textAlign: "center", padding: "0 2rem 4rem" }}>
        <button
          onClick={() => router.push("/register")}
          style={{
            padding: "1rem 2.5rem",
            borderRadius: 999,
            border: "none",
            fontSize: "1rem",
            fontWeight: 600,
            cursor: "pointer",
            background: "linear-gradient(135deg, #1e6fff 0%, #00d4ff 50%, #1e6fff 100%)",
            color: "#ffffff",
            boxShadow: "0 12px 30px rgba(0,0,0,0.5), 0 0 50px rgba(30, 111, 255, 0.25)",
          }}
        >
          Start your 30-day trial
        </button>
        <p style={{ fontSize: "0.75rem", color: "#4a5568", marginTop: "1rem" }}>
          No credit card required. Platform tools only — not financial advice.
        </p>
      </section>

      {/* ── Footer ── */}
      <footer
        style={{
          padding: "3rem 2rem",
          borderTop: "1px solid rgba(74, 179, 255, 0.1)",
          textAlign: "center",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "0.5rem",
            marginBottom: "1rem",
          }}
        >
          <div
            style={{
              width: 28,
              height: 28,
              borderRadius: 6,
              background: "linear-gradient(135deg, #2979ff 0%, #3fe0ff 100%)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontWeight: 700,
              fontSize: "0.8rem",
              color: "#fff",
            }}
          >
            G
          </div>
          <span style={{ fontSize: "1.1rem", fontWeight: 600, color: "#e9f4ff" }}>
            GuvFX
          </span>
        </div>
        <p style={{ fontSize: "0.85rem", color: "#6b7c91", margin: "0 0 0.5rem" }}>
          Automated Trading Intelligence
        </p>
        <p style={{ fontSize: "0.75rem", color: "#4a5568", margin: 0 }}>
          © 2025 GuvFX. All rights reserved.
        </p>
      </footer>

      <LegalFooter lang={lang} />
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Info Bullet component
// ─────────────────────────────────────────────────────────────────────
function InfoBullet({ text }: { text: string }) {
  return (
    <li style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#64748b" strokeWidth="2">
        <circle cx="12" cy="12" r="10" />
        <path d="M12 16v-4M12 8h.01" />
      </svg>
      <span style={{ fontSize: "0.85rem", color: "#94a3b8" }}>{text}</span>
    </li>
  );
}
