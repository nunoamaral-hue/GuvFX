"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { type Lang, detectLang, setLang as persistLang, t } from "@/lib/i18n";
import { LegalFooter } from "@/components/LegalFooter";
import { LanguageDropdown } from "@/components/LanguageDropdown";

type RegisterResponse = { id: number; email: string; username: string };

const STEPS = [
  { num: 1, label: "Create account" },
  { num: 2, label: "Select plan" },
  { num: 3, label: "Complete profile" },
  { num: 4, label: "Connect broker" },
  { num: 5, label: "Get started" },
];

export default function RegisterPage() {
  const router = useRouter();

  const [lang, setLangState] = useState<Lang>(() => {
    if (typeof window === "undefined") return "en";
    return detectLang();
  });

  const [email, setEmail] = useState("");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [password, setPassword] = useState("");
  const [username, setUsername] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("https://api.guvfx.com/api/onboarding/state/", {
      method: "GET",
      credentials: "include",
    })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!data) return;
        if (data.onboarding_completed) window.location.replace("/dashboard");
        else window.location.replace("/onboarding");
      })
      .catch(() => {});
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const fn = firstName.trim();
    const ln = lastName.trim();
    if (!fn || !ln) {
      setError("First name and last name are required.");
      return;
    }
    if (password.length < 8) {
      setError(t(lang, "register.passwordTooShort"));
      return;
    }
    setLoading(true);
    try {
      await apiFetch<RegisterResponse>("/api/auth/register/", {
        method: "POST",
        body: JSON.stringify({ email, username: username || email, password, first_name: fn, last_name: ln }),
      });
      await apiFetch("/api/auth/cookie/login/", {
        method: "POST",
        body: JSON.stringify({ email, username: email, password }),
      });
      router.push("/onboarding");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t(lang, "register.errorDefault"));
      setLoading(false);
    }
  };

  return (
    <div style={pageStyle}>
      {/* ── Top bar ── */}
      <header style={topBarStyle}>
        <div style={logoStyle} onClick={() => router.push("/")}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#4ab3ff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polygon points="12 2 2 7 12 12 22 7 12 2" />
            <polyline points="2 17 12 22 22 17" />
            <polyline points="2 12 12 17 22 12" />
          </svg>
          <span style={logoTextStyle}>GuvFX</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          <LanguageDropdown lang={lang} onChange={(next) => { persistLang(next); setLangState(next); }} />
          <button onClick={() => router.push("/login")} style={loginBtnStyle}>
            {t(lang, "register.login")}
          </button>
        </div>
      </header>

      {/* ── Wizard ── */}
      <main style={mainStyle}>
        <div style={wizardStyle}>
          {/* Left: Step rail */}
          <aside style={railStyle}>
            <div style={railHeaderStyle}>
              <span style={railLabelStyle}>Setup</span>
              <span style={railCountStyle}>1 / 5</span>
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: "2px" }}>
              {STEPS.map((step) => {
                const active = step.num === 1;
                const future = step.num > 1;
                return (
                  <div key={step.num} style={{ ...railItemStyle, ...(active ? railItemActiveStyle : {}), opacity: future ? 0.38 : 1 }}>
                    <span style={{ ...railDotStyle, ...(active ? railDotActiveStyle : {}) }}>
                      {step.num}
                    </span>
                    <span style={{ fontSize: "0.8rem", fontWeight: active ? 600 : 400, color: active ? "#e9f4ff" : "#64748b" }}>
                      {step.label}
                    </span>
                  </div>
                );
              })}
            </div>

            {/* Progress indicator */}
            <div style={railProgressWrapStyle}>
              <div style={railProgressTrackStyle}>
                <div style={railProgressFillStyle} />
              </div>
              <span style={railProgressTextStyle}>20% complete</span>
            </div>
          </aside>

          {/* Right: Form surface */}
          <section style={formSurfaceStyle}>
            {/* Step badge */}
            <span style={stepBadgeStyle}>Step 1 of 5</span>

            {/* Title block */}
            <h1 style={titleStyle}>{t(lang, "register.createAccount")}</h1>
            <p style={subtitleStyle}>{t(lang, "register.stepNote")}</p>

            {/* Divider */}
            <div style={dividerStyle} />

            {/* Form */}
            <form onSubmit={handleSubmit} style={{ marginTop: 0 }}>
              <div style={{ marginBottom: "1.1rem" }}>
                <label htmlFor="email" style={labelStyle}>{t(lang, "register.email")}</label>
                <input id="email" type="email" required placeholder={t(lang, "register.emailPlaceholder")} value={email} onChange={(e) => setEmail(e.target.value)} style={inputStyle} />
              </div>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.9rem", marginBottom: "1.1rem" }}>
                <div>
                  <label htmlFor="firstName" style={labelStyle}>First name</label>
                  <input id="firstName" type="text" required placeholder="First name" value={firstName} onChange={(e) => setFirstName(e.target.value)} style={inputStyle} />
                </div>
                <div>
                  <label htmlFor="lastName" style={labelStyle}>Last name</label>
                  <input id="lastName" type="text" required placeholder="Last name" value={lastName} onChange={(e) => setLastName(e.target.value)} style={inputStyle} />
                </div>
              </div>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.9rem", marginBottom: "1.1rem" }}>
                <div>
                  <label htmlFor="password" style={labelStyle}>{t(lang, "register.password")}</label>
                  <input id="password" type="password" required placeholder={t(lang, "register.passwordPlaceholder")} value={password} onChange={(e) => setPassword(e.target.value)} style={inputStyle} />
                </div>
                <div>
                  <label htmlFor="username" style={labelStyle}>{t(lang, "register.username")}</label>
                  <input id="username" type="text" placeholder={t(lang, "register.usernamePlaceholder")} value={username} onChange={(e) => setUsername(e.target.value)} style={inputStyle} />
                </div>
              </div>

              {error && <div style={errorStyle}>{error}</div>}

              <button type="submit" disabled={loading} style={{ ...ctaStyle, ...(loading ? ctaLoadingStyle : {}) }}>
                {loading ? t(lang, "register.creating") : t(lang, "register.continue")}
                {!loading && (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ marginLeft: 6 }}>
                    <path d="M5 12h14" /><path d="m12 5 7 7-7 7" />
                  </svg>
                )}
              </button>
            </form>

            {/* Trust footer */}
            <div style={trustStyle}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" strokeWidth="2" style={{ flexShrink: 0, marginTop: 1 }}>
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              </svg>
              <p style={trustTextStyle}>
                <strong style={{ color: "#94a3b8" }}>{t(lang, "register.trustMiniTitle")}</strong>
                {" — "}
                {t(lang, "register.trustMiniBody")}
              </p>
            </div>
          </section>
        </div>
      </main>

      <LegalFooter lang={lang} />
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Styles
// ─────────────────────────────────────────────────────────────────────

const pageStyle: React.CSSProperties = {
  minHeight: "100vh",
  display: "flex",
  flexDirection: "column",
  background: "radial-gradient(ellipse at 15% 0%, #0d1f35 0%, #050816 45%, #030610 100%)",
  color: "#e5f4ff",
  fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
};

const topBarStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "0.9rem 2rem",
  borderBottom: "1px solid rgba(255,255,255,0.04)",
};

const logoStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
  cursor: "pointer",
};

const logoTextStyle: React.CSSProperties = {
  fontSize: "1.05rem",
  fontWeight: 700,
  background: "linear-gradient(120deg, #4ab3ff 0%, #7af0ff 100%)",
  WebkitBackgroundClip: "text",
  WebkitTextFillColor: "transparent",
};

const loginBtnStyle: React.CSSProperties = {
  padding: "0.4rem 1rem",
  borderRadius: 7,
  border: "1px solid rgba(255,255,255,0.1)",
  background: "rgba(255,255,255,0.03)",
  color: "#8fa0b7",
  fontSize: "0.8rem",
  fontWeight: 500,
  cursor: "pointer",
  transition: "border-color 0.15s",
};

const mainStyle: React.CSSProperties = {
  flex: 1,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  padding: "1.5rem 2rem",
};

const wizardStyle: React.CSSProperties = {
  width: "100%",
  maxWidth: 880,
  display: "grid",
  gridTemplateColumns: "200px 1fr",
  borderRadius: 16,
  border: "1px solid rgba(74, 179, 255, 0.08)",
  background: "linear-gradient(160deg, rgba(10, 16, 38, 0.98) 0%, rgba(5, 8, 20, 0.99) 100%)",
  boxShadow: "0 20px 60px rgba(0,0,0,0.5), 0 0 0 1px rgba(74, 179, 255, 0.04)",
  overflow: "hidden",
  minHeight: 520,
};

const railStyle: React.CSSProperties = {
  borderRight: "1px solid rgba(255,255,255,0.04)",
  padding: "1.75rem 1rem 1.5rem",
  display: "flex",
  flexDirection: "column",
  background: "rgba(0,0,0,0.12)",
};

const railHeaderStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  marginBottom: "1.25rem",
  paddingLeft: "0.5rem",
  paddingRight: "0.25rem",
};

const railLabelStyle: React.CSSProperties = {
  fontSize: "0.68rem",
  fontWeight: 700,
  color: "#475569",
  textTransform: "uppercase",
  letterSpacing: "0.1em",
};

const railCountStyle: React.CSSProperties = {
  fontSize: "0.65rem",
  fontWeight: 600,
  color: "#4ab3ff",
  background: "rgba(74, 179, 255, 0.1)",
  padding: "0.15rem 0.45rem",
  borderRadius: 4,
};

const railItemStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.55rem",
  padding: "0.5rem 0.55rem",
  borderRadius: 8,
  border: "1px solid transparent",
  transition: "all 0.15s ease",
};

const railItemActiveStyle: React.CSSProperties = {
  background: "rgba(74, 179, 255, 0.06)",
  border: "1px solid rgba(74, 179, 255, 0.15)",
};

const railDotStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  width: 22,
  height: 22,
  borderRadius: "50%",
  fontSize: "0.62rem",
  fontWeight: 700,
  flexShrink: 0,
  background: "rgba(255,255,255,0.03)",
  border: "1px solid rgba(255,255,255,0.07)",
  color: "#475569",
};

const railDotActiveStyle: React.CSSProperties = {
  background: "rgba(74, 179, 255, 0.15)",
  border: "1.5px solid rgba(74, 179, 255, 0.4)",
  color: "#4ab3ff",
};

const railProgressWrapStyle: React.CSSProperties = {
  marginTop: "auto",
  paddingTop: "1.25rem",
  paddingLeft: "0.5rem",
  paddingRight: "0.25rem",
};

const railProgressTrackStyle: React.CSSProperties = {
  height: 3,
  borderRadius: 99,
  background: "rgba(255,255,255,0.05)",
  overflow: "hidden",
  marginBottom: "0.4rem",
};

const railProgressFillStyle: React.CSSProperties = {
  width: "20%",
  height: "100%",
  background: "linear-gradient(90deg, #2563eb, #38bdf8)",
  borderRadius: 99,
};

const railProgressTextStyle: React.CSSProperties = {
  fontSize: "0.62rem",
  color: "#475569",
  fontWeight: 500,
};

const formSurfaceStyle: React.CSSProperties = {
  padding: "2.5rem 2.75rem",
  display: "flex",
  flexDirection: "column",
  justifyContent: "center",
};

const stepBadgeStyle: React.CSSProperties = {
  display: "inline-block",
  fontSize: "0.65rem",
  fontWeight: 700,
  color: "#4ab3ff",
  textTransform: "uppercase",
  letterSpacing: "0.08em",
  marginBottom: "0.85rem",
  padding: "0.25rem 0.6rem",
  borderRadius: 5,
  background: "rgba(74, 179, 255, 0.07)",
  border: "1px solid rgba(74, 179, 255, 0.12)",
  alignSelf: "flex-start",
};

const titleStyle: React.CSSProperties = {
  fontSize: "1.6rem",
  fontWeight: 700,
  margin: "0 0 0.35rem",
  color: "#e9f4ff",
  lineHeight: 1.15,
  letterSpacing: "-0.01em",
};

const subtitleStyle: React.CSSProperties = {
  fontSize: "0.88rem",
  color: "#7a8fa3",
  margin: "0 0 1.5rem",
  lineHeight: 1.55,
  maxWidth: 420,
};

const dividerStyle: React.CSSProperties = {
  height: 1,
  background: "rgba(255,255,255,0.04)",
  marginBottom: "1.5rem",
};

const labelStyle: React.CSSProperties = {
  display: "block",
  fontSize: "0.76rem",
  marginBottom: "0.35rem",
  color: "#8fa0b7",
  fontWeight: 500,
  letterSpacing: "0.01em",
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "0.6rem 0.85rem",
  borderRadius: 8,
  border: "1px solid rgba(255,255,255,0.07)",
  background: "rgba(0,0,0,0.25)",
  color: "#e5f4ff",
  fontSize: "0.88rem",
  outline: "none",
  boxSizing: "border-box",
  transition: "border-color 0.15s ease",
};

const errorStyle: React.CSSProperties = {
  background: "rgba(239, 68, 68, 0.06)",
  border: "1px solid rgba(239, 68, 68, 0.2)",
  borderRadius: 8,
  padding: "0.5rem 0.75rem",
  marginBottom: "1rem",
  fontSize: "0.82rem",
  color: "#fca5a5",
};

const ctaStyle: React.CSSProperties = {
  width: "100%",
  padding: "0.7rem 1rem",
  borderRadius: 9,
  border: "none",
  fontSize: "0.9rem",
  fontWeight: 600,
  cursor: "pointer",
  background: "linear-gradient(135deg, #2563eb 0%, #0284c7 100%)",
  color: "#ffffff",
  boxShadow: "0 2px 12px rgba(37, 99, 235, 0.25), 0 0 0 1px rgba(37, 99, 235, 0.1)",
  transition: "all 0.15s ease",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  letterSpacing: "0.01em",
};

const ctaLoadingStyle: React.CSSProperties = {
  background: "rgba(37, 99, 235, 0.4)",
  boxShadow: "none",
  cursor: "not-allowed",
  opacity: 0.7,
};

const trustStyle: React.CSSProperties = {
  marginTop: "1.5rem",
  paddingTop: "1.25rem",
  borderTop: "1px solid rgba(255,255,255,0.04)",
  display: "flex",
  alignItems: "flex-start",
  gap: "0.55rem",
};

const trustTextStyle: React.CSSProperties = {
  margin: 0,
  fontSize: "0.72rem",
  color: "#5a6a7e",
  lineHeight: 1.55,
};
