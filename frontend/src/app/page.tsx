"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { type Lang, detectLang, setLang as persistLang, t } from "@/lib/i18n";

export default function LandingPage() {
  const router = useRouter();
  // Lazy initialization: detectLang runs once on first render (client-side)
  const [lang, setLangState] = useState<Lang>(() => {
    if (typeof window === "undefined") return "en";
    return detectLang();
  });

  const toggleLang = () => {
    const next: Lang = lang === "en" ? "ja" : "en";
    persistLang(next);
    setLangState(next);
  };

  const scrollToFeatures = () => {
    const el = document.getElementById("features");
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  };

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
      {/* ======================================================================
          NAVBAR
          ====================================================================== */}
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
        {/* Logo + Wordmark */}
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
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
        </div>

        {/* Right side: Lang toggle + Login + CTA */}
        <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
          {/* Language toggle */}
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

          {/* Login */}
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
            {t(lang, "landing.login")}
          </button>

          {/* Get Started CTA */}
          <button
            onClick={() => router.push("/register")}
            style={{
              padding: "0.5rem 1.25rem",
              borderRadius: 999,
              border: "none",
              background: "linear-gradient(135deg, #2979ff 0%, #3fe0ff 100%)",
              color: "#fff",
              fontSize: "0.9rem",
              fontWeight: 500,
              cursor: "pointer",
              boxShadow: "0 4px 15px rgba(41, 121, 255, 0.3)",
            }}
          >
            {t(lang, "landing.getStarted")}
          </button>
        </div>
      </nav>

      {/* ======================================================================
          HERO SECTION
          ====================================================================== */}
      <section
        style={{
          minHeight: "100vh",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          alignItems: "center",
          textAlign: "center",
          padding: "6rem 2rem 4rem",
        }}
      >
        <h1
          style={{
            fontSize: "clamp(2.5rem, 6vw, 4rem)",
            fontWeight: 700,
            margin: 0,
            maxWidth: 800,
            lineHeight: 1.1,
            background:
              "linear-gradient(120deg, #4ab3ff 0%, #7af0ff 40%, #4ab3ff 100%)",
            WebkitBackgroundClip: "text",
            WebkitTextFillColor: "transparent",
          }}
        >
          {t(lang, "landing.heroTitle")}
        </h1>

        <p
          style={{
            fontSize: "clamp(1rem, 2vw, 1.25rem)",
            marginTop: "1.5rem",
            maxWidth: 600,
            color: "#9ab0c5",
            lineHeight: 1.6,
          }}
        >
          {t(lang, "landing.heroSubtitle")}
        </p>

        <div
          style={{
            marginTop: "2.5rem",
            display: "flex",
            gap: "1rem",
            flexWrap: "wrap",
            justifyContent: "center",
          }}
        >
          {/* Primary CTA */}
          <button
            onClick={() => router.push("/register")}
            style={{
              padding: "1rem 2.5rem",
              borderRadius: 999,
              border: "none",
              fontSize: "1rem",
              fontWeight: 600,
              cursor: "pointer",
              background:
                "linear-gradient(135deg, #2979ff 0%, #3fe0ff 50%, #2979ff 100%)",
              color: "#ffffff",
              boxShadow: "0 12px 30px rgba(0, 0, 0, 0.5)",
            }}
          >
            {t(lang, "landing.heroCTA")}
          </button>

          {/* Secondary CTA */}
          <button
            onClick={scrollToFeatures}
            style={{
              padding: "1rem 2rem",
              borderRadius: 999,
              border: "1px solid rgba(255,255,255,0.18)",
              fontSize: "1rem",
              fontWeight: 500,
              cursor: "pointer",
              background: "transparent",
              color: "#c2d5ff",
            }}
          >
            {t(lang, "landing.heroSecondaryCTA")}
          </button>
        </div>

        {/* Scroll indicator */}
        <div
          style={{
            marginTop: "4rem",
            opacity: 0.5,
            animation: "bounce 2s infinite",
          }}
        >
          <svg
            width="24"
            height="24"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
          >
            <path d="M12 5v14M5 12l7 7 7-7" />
          </svg>
        </div>
      </section>

      {/* ======================================================================
          FEATURES SECTION
          ====================================================================== */}
      <section
        id="features"
        style={{
          padding: "5rem 2rem",
          background: "rgba(5, 8, 22, 0.5)",
        }}
      >
        <div style={{ maxWidth: 1100, margin: "0 auto" }}>
          <h2
            style={{
              fontSize: "2rem",
              fontWeight: 700,
              textAlign: "center",
              marginBottom: "0.75rem",
              color: "#e9f4ff",
            }}
          >
            {t(lang, "landing.featuresTitle")}
          </h2>
          <p
            style={{
              fontSize: "1rem",
              textAlign: "center",
              color: "#8fa0b7",
              maxWidth: 500,
              margin: "0 auto 3rem",
            }}
          >
            {t(lang, "landing.featuresSubtitle")}
          </p>

          {/* Feature grid */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
              gap: "1.5rem",
            }}
          >
            {/* Feature 1 */}
            <FeatureCard
              lang={lang}
              icon={
                <svg
                  width="28"
                  height="28"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="#4ab3ff"
                  strokeWidth="2"
                >
                  <rect x="3" y="3" width="18" height="18" rx="2" />
                  <path d="M9 9h6M9 12h6M9 15h4" />
                </svg>
              }
              titleKey="landing.feature1Title"
              descKey="landing.feature1Desc"
            />

            {/* Feature 2 */}
            <FeatureCard
              lang={lang}
              icon={
                <svg
                  width="28"
                  height="28"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="#4ab3ff"
                  strokeWidth="2"
                >
                  <path d="M3 3v18h18" />
                  <path d="M7 14l4-4 4 4 5-5" />
                </svg>
              }
              titleKey="landing.feature2Title"
              descKey="landing.feature2Desc"
            />

            {/* Feature 3 */}
            <FeatureCard
              lang={lang}
              icon={
                <svg
                  width="28"
                  height="28"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="#4ab3ff"
                  strokeWidth="2"
                >
                  <circle cx="12" cy="12" r="9" />
                  <path d="M12 8v4l3 3" />
                </svg>
              }
              titleKey="landing.feature3Title"
              descKey="landing.feature3Desc"
            />

            {/* Feature 4 */}
            <FeatureCard
              lang={lang}
              icon={
                <svg
                  width="28"
                  height="28"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="#4ab3ff"
                  strokeWidth="2"
                >
                  <path d="M4 4h16v16H4z" />
                  <path d="M9 9h6v6H9z" />
                  <path d="M4 9h5M15 9h5M9 4v5M9 15v5" />
                </svg>
              }
              titleKey="landing.feature4Title"
              descKey="landing.feature4Desc"
            />
          </div>
        </div>
      </section>

      {/* ======================================================================
          FOOTER
          ====================================================================== */}
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
          <span
            style={{
              fontSize: "1.1rem",
              fontWeight: 600,
              color: "#e9f4ff",
            }}
          >
            GuvFX
          </span>
        </div>

        <p
          style={{
            fontSize: "0.85rem",
            color: "#6b7c91",
            margin: "0 0 0.5rem",
          }}
        >
          {t(lang, "landing.footerTagline")}
        </p>

        <p
          style={{
            fontSize: "0.75rem",
            color: "#4a5568",
            margin: "0 0 0.75rem",
          }}
        >
          {t(lang, "landing.footerCopyright")}
        </p>

        <p
          style={{
            fontSize: "0.7rem",
            color: "#4a5568",
            maxWidth: 400,
            margin: "0 auto",
          }}
        >
          {t(lang, "landing.footerDisclaimer")}
        </p>
      </footer>

      {/* Keyframe animation for bounce */}
      <style jsx global>{`
        @keyframes bounce {
          0%,
          20%,
          50%,
          80%,
          100% {
            transform: translateY(0);
          }
          40% {
            transform: translateY(-8px);
          }
          60% {
            transform: translateY(-4px);
          }
        }
      `}</style>
    </div>
  );
}

// =============================================================================
// FEATURE CARD COMPONENT
// =============================================================================

function FeatureCard({
  lang,
  icon,
  titleKey,
  descKey,
}: {
  lang: Lang;
  icon: React.ReactNode;
  titleKey: string;
  descKey: string;
}) {
  return (
    <div
      style={{
        background: "rgba(5, 8, 22, 0.8)",
        borderRadius: 14,
        padding: "1.5rem",
        border: "1px solid rgba(74, 179, 255, 0.12)",
        transition: "border-color 0.2s ease, transform 0.2s ease",
      }}
    >
      <div
        style={{
          width: 48,
          height: 48,
          borderRadius: 10,
          background: "rgba(74, 179, 255, 0.08)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          marginBottom: "1rem",
        }}
      >
        {icon}
      </div>
      <h3
        style={{
          fontSize: "1.1rem",
          fontWeight: 600,
          margin: "0 0 0.5rem",
          color: "#e9f4ff",
        }}
      >
        {t(lang, titleKey)}
      </h3>
      <p
        style={{
          fontSize: "0.9rem",
          color: "#8fa0b7",
          margin: 0,
          lineHeight: 1.5,
        }}
      >
        {t(lang, descKey)}
      </p>
    </div>
  );
}
