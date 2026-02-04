"use client";

import { useEffect, useCallback, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { type Lang, detectLang, setLang as persistLang, t } from "@/lib/i18n";
import { LegalFooter } from "@/components/LegalFooter";

type RegisterResponse = {
  id: number;
  email: string;
  username: string;
};

export default function RegisterPage() {
  const router = useRouter();

  // Lazy initialization: detectLang runs once on first render (client-side)
  const [lang, setLangState] = useState<Lang>(() => {
    if (typeof window === "undefined") return "en";
    return detectLang();
  });

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [username, setUsername] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // Language dropdown state
  const [langMenuOpen, setLangMenuOpen] = useState(false);
  const langMenuRef = useRef<HTMLDivElement>(null);

  const selectLang = useCallback(
    (next: Lang) => {
      persistLang(next);
      setLangState(next);
      setLangMenuOpen(false);
    },
    [],
  );

  // Close language menu on outside click or Escape
  useEffect(() => {
    if (!langMenuOpen) return;

    function handleClickOutside(e: MouseEvent) {
      if (langMenuRef.current && !langMenuRef.current.contains(e.target as Node)) {
        setLangMenuOpen(false);
      }
    }
    function handleEscape(e: KeyboardEvent) {
      if (e.key === "Escape") setLangMenuOpen(false);
    }

    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleEscape);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleEscape);
    };
  }, [langMenuOpen]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSuccess(null);

    if (password.length < 3) {
      setError(t(lang, "register.passwordTooShort"));
      return;
    }

    setLoading(true);
    try {
      const body = {
        email,
        username: username || email,
        password,
        first_name: "",
        last_name: "",
      };

      const data = await apiFetch<RegisterResponse>("/api/auth/register/", {
        method: "POST",
        body: JSON.stringify(body),
      });

      // Replace {email} placeholder in success message
      const successMsg = t(lang, "register.success").replace("{email}", data.email);
      setSuccess(successMsg);
    } catch (err: unknown) {
      console.error(err);
      const message = err instanceof Error ? err.message : t(lang, "register.errorDefault");
      setError(message);
    } finally {
      setLoading(false);
    }
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
      <div style={{ flex: 1, display: "flex" }}>
      {/* Left panel */}
      <div
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          padding: "3rem 4rem",
        }}
      >
        <div>
          <p
            style={{
              textTransform: "uppercase",
              letterSpacing: "0.15em",
              fontSize: "0.75rem",
              color: "#4ab3ff",
              marginBottom: "0.5rem",
            }}
          >
            {t(lang, "register.welcomeTo")}
          </p>
          <h1
            style={{
              fontSize: "3rem",
              margin: 0,
              background:
                "linear-gradient(120deg, #4ab3ff 0%, #7af0ff 40%, #4ab3ff 100%)",
              WebkitBackgroundClip: "text",
              WebkitTextFillColor: "transparent",
            }}
          >
            GuvFX
          </h1>
          <p
            style={{
              fontSize: "1rem",
              marginTop: "1rem",
              maxWidth: 320,
              color: "#9ab0c5",
            }}
          >
            {t(lang, "register.subtitle")}
          </p>

          <div style={{ marginTop: "2.5rem", display: "flex", gap: "1rem", alignItems: "center" }}>
            {/* Home button */}
            <button
              style={{
                padding: "0.9rem 2.4rem",
                borderRadius: 999,
                border: "1px solid rgba(255,255,255,0.18)",
                fontSize: "1rem",
                fontWeight: 500,
                cursor: "pointer",
                background: "transparent",
                color: "#c2d5ff",
              }}
              onClick={() => router.push("/")}
            >
              {t(lang, "ui.home")}
            </button>

            {/* Language dropdown */}
            <div ref={langMenuRef} style={{ position: "relative" }}>
              <button
                type="button"
                onClick={() => setLangMenuOpen((v) => !v)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.45rem",
                  padding: "0.7rem 1rem",
                  borderRadius: 999,
                  border: "1px solid rgba(255,255,255,0.18)",
                  fontSize: "0.9rem",
                  fontWeight: 500,
                  cursor: "pointer",
                  background: "rgba(255,255,255,0.04)",
                  color: "#c2d5ff",
                  transition: "background 150ms ease",
                }}
              >
                <span>{lang === "en" ? "🇬🇧" : "🇯🇵"}</span>
                <span>{lang === "en" ? t(lang, "ui.langEnglish") : t(lang, "ui.langJapanese")}</span>
                <svg
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.5"
                  style={{
                    transform: langMenuOpen ? "rotate(180deg)" : "rotate(0deg)",
                    transition: "transform 200ms ease",
                    marginLeft: "0.15rem",
                  }}
                >
                  <path d="M6 9l6 6 6-6" />
                </svg>
              </button>

              {/* Dropdown menu */}
              {langMenuOpen && (
                <div
                  style={{
                    position: "absolute",
                    top: "calc(100% + 6px)",
                    left: 0,
                    minWidth: "160px",
                    background: "rgba(10, 15, 30, 0.95)",
                    backdropFilter: "blur(12px)",
                    border: "1px solid rgba(255,255,255,0.12)",
                    borderRadius: 10,
                    boxShadow: "0 10px 40px rgba(0,0,0,0.6)",
                    zIndex: 1000,
                    overflow: "hidden",
                  }}
                >
                  <LangMenuItem
                    flag="🇬🇧"
                    label={t(lang, "ui.langEnglish")}
                    active={lang === "en"}
                    onClick={() => selectLang("en")}
                  />
                  <LangMenuItem
                    flag="🇯🇵"
                    label={t(lang, "ui.langJapanese")}
                    active={lang === "ja"}
                    onClick={() => selectLang("ja")}
                  />
                </div>
              )}
            </div>
          </div>

          {/* Footer link — Log in only (Sign up is redundant on /register) */}
          <div
            style={{
              marginTop: "1.5rem",
              fontSize: "0.9rem",
              color: "#7e8ea5",
            }}
          >
            <span
              style={{ cursor: "pointer" }}
              onClick={() => router.push("/login")}
            >
              {t(lang, "register.login")}
            </span>
          </div>
        </div>
      </div>

      {/* Right panel */}
      <div
        style={{
          flex: 1,
          borderLeft: "1px solid rgba(255,255,255,0.05)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "3rem 4rem",
        }}
      >
        <div
          id="create-account"
          style={{
            width: "100%",
            maxWidth: 380,
            background: "rgba(5, 8, 22, 0.9)",
            borderRadius: 18,
            padding: "2rem",
            boxShadow: "0 20px 60px rgba(0,0,0,0.8)",
            border: "1px solid rgba(74,179,255,0.18)",
          }}
        >
          {/* Step indicator header */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.5rem",
              marginBottom: "0.5rem",
            }}
          >
            <span
              style={{
                fontSize: "0.7rem",
                fontWeight: 600,
                color: "#4ab3ff",
                textTransform: "uppercase",
                letterSpacing: "0.05em",
              }}
            >
              {t(lang, "register.stepIndicator")}
            </span>
            <span style={{ color: "#4a5568", fontSize: "0.7rem" }}>—</span>
            <span
              style={{
                fontSize: "0.7rem",
                color: "#8fa0b7",
                textTransform: "uppercase",
                letterSpacing: "0.05em",
              }}
            >
              {t(lang, "register.stepTitle")}
            </span>
          </div>

          <h2
            style={{
              fontSize: "1.6rem",
              margin: 0,
              marginBottom: "0.5rem",
              color: "#e9f4ff",
            }}
          >
            {t(lang, "register.createAccount")}
          </h2>

          {/* Progress bar */}
          <div
            style={{
              height: 4,
              borderRadius: 999,
              background: "rgba(255,255,255,0.06)",
              overflow: "hidden",
              marginBottom: "0.75rem",
            }}
          >
            <div
              style={{
                width: "20%",
                height: "100%",
                background:
                  "linear-gradient(90deg, #4ab3ff 0%, #7af0ff 100%)",
              }}
            />
          </div>

          {/* Step note info box */}
          <div
            style={{
              background: "rgba(74, 179, 255, 0.06)",
              border: "1px solid rgba(74, 179, 255, 0.15)",
              borderRadius: 8,
              padding: "0.6rem 0.75rem",
              marginBottom: "1.25rem",
              fontSize: "0.75rem",
              color: "#9ab0c5",
              lineHeight: 1.5,
            }}
          >
            {t(lang, "register.stepNote")}
          </div>

          {/* Trust mini-reassurance */}
          <div
            style={{
              background: "rgba(74, 179, 255, 0.04)",
              border: "1px solid rgba(74, 179, 255, 0.15)",
              borderRadius: 8,
              padding: "0.75rem",
              marginBottom: "1.25rem",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "0.4rem", marginBottom: "0.35rem" }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#4ab3ff" strokeWidth="2">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              </svg>
              <span style={{ fontSize: "0.8rem", fontWeight: 600, color: "#a8c8e8" }}>
                {t(lang, "register.trustMiniTitle")}
              </span>
            </div>
            <p style={{ margin: 0, fontSize: "0.75rem", color: "#7a8fa3", lineHeight: 1.45 }}>
              {t(lang, "register.trustMiniBody")}
            </p>
          </div>

          {/* Feedback messages */}
          {error && (
            <div
              style={{
                background: "rgba(255, 76, 76, 0.1)",
                border: "1px solid rgba(255, 76, 76, 0.4)",
                borderRadius: 8,
                padding: "0.5rem 0.75rem",
                marginBottom: "0.75rem",
                fontSize: "0.8rem",
                color: "#ff9b9b",
              }}
            >
              {error}
            </div>
          )}

          {success && (
            <div
              style={{
                background: "rgba(74, 179, 255, 0.1)",
                border: "1px solid rgba(74, 179, 255, 0.5)",
                borderRadius: 8,
                padding: "0.5rem 0.75rem",
                marginBottom: "0.75rem",
                fontSize: "0.8rem",
                color: "#c9ecff",
              }}
            >
              {success}
            </div>
          )}

          {/* Form */}
          <form onSubmit={handleSubmit}>
            {/* Email */}
            <label
              htmlFor="email"
              style={{
                display: "block",
                fontSize: "0.85rem",
                marginBottom: "0.3rem",
              }}
            >
              {t(lang, "register.email")}
            </label>
            <input
              id="email"
              type="email"
              required
              placeholder={t(lang, "register.emailPlaceholder")}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              style={{
                width: "100%",
                padding: "0.6rem 0.8rem",
                borderRadius: 10,
                border: "1px solid rgba(255,255,255,0.08)",
                background: "rgba(8, 12, 32, 0.9)",
                color: "#e5f4ff",
                fontSize: "0.9rem",
                marginBottom: "1rem",
                outline: "none",
              }}
            />

            {/* Password */}
            <label
              htmlFor="password"
              style={{
                display: "block",
                fontSize: "0.85rem",
                marginBottom: "0.3rem",
              }}
            >
              {t(lang, "register.password")}
            </label>
            <input
              id="password"
              type="password"
              required
              placeholder={t(lang, "register.passwordPlaceholder")}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              style={{
                width: "100%",
                padding: "0.6rem 0.8rem",
                borderRadius: 10,
                border: "1px solid rgba(255,255,255,0.08)",
                background: "rgba(8, 12, 32, 0.9)",
                color: "#e5f4ff",
                fontSize: "0.9rem",
                marginBottom: "1.4rem",
                outline: "none",
              }}
            />

            {/* Optional username */}
            <label
              htmlFor="username"
              style={{
                display: "block",
                fontSize: "0.85rem",
                marginBottom: "0.3rem",
              }}
            >
              {t(lang, "register.username")}
            </label>
            <input
              id="username"
              type="text"
              placeholder={t(lang, "register.usernamePlaceholder")}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              style={{
                width: "100%",
                padding: "0.6rem 0.8rem",
                borderRadius: 10,
                border: "1px solid rgba(255,255,255,0.08)",
                background: "rgba(8, 12, 32, 0.9)",
                color: "#e5f4ff",
                fontSize: "0.9rem",
                marginBottom: "1.4rem",
                outline: "none",
              }}
            />

            <button
              type="submit"
              disabled={loading}
              style={{
                width: "100%",
                padding: "0.85rem 1rem",
                borderRadius: 999,
                border: "none",
                fontSize: "1rem",
                fontWeight: 500,
                cursor: loading ? "not-allowed" : "pointer",
                background: loading
                  ? "linear-gradient(135deg,#5579ff,#76c3ff)"
                  : "linear-gradient(135deg,#2979ff,#3fe0ff,#2979ff)",
                color: "#ffffff",
                boxShadow: "0 12px 30px rgba(0,0,0,0.7)",
                transition: "opacity 0.15s ease",
                opacity: loading ? 0.8 : 1,
              }}
            >
              {loading ? t(lang, "register.creating") : t(lang, "register.continue")}
            </button>
          </form>

          {/* Coming Next panel */}
          <div
            style={{
              marginTop: "1.5rem",
              paddingTop: "1rem",
              borderTop: "1px solid rgba(255, 255, 255, 0.06)",
            }}
          >
            <p
              style={{
                fontSize: "0.75rem",
                fontWeight: 600,
                color: "#6b7c91",
                textTransform: "uppercase",
                letterSpacing: "0.05em",
                margin: "0 0 0.75rem",
              }}
            >
              {t(lang, "register.nextTitle")}
            </p>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
              <ComingNextItem label={t(lang, "register.nextEmailVerify")} />
              <ComingNextItem label={t(lang, "register.nextHosting")} />
              <ComingNextItem label={t(lang, "register.nextProfile")} />
              <ComingNextItem label={t(lang, "register.nextSecurity")} />
            </div>
          </div>
        </div>
      </div>
      </div>
      <LegalFooter lang={lang} />
    </div>
  );
}

// =============================================================================
// LANGUAGE MENU ITEM COMPONENT
// =============================================================================

function LangMenuItem({
  flag,
  label,
  active,
  onClick,
}: {
  flag: string;
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        display: "flex",
        alignItems: "center",
        gap: "0.5rem",
        width: "100%",
        padding: "0.6rem 0.9rem",
        background: active ? "rgba(74, 179, 255, 0.1)" : "transparent",
        border: "none",
        color: active ? "#e5f4ff" : "#9ca3af",
        fontSize: "0.85rem",
        cursor: "pointer",
        transition: "background 150ms ease, color 150ms ease",
      }}
      onMouseEnter={(e) => {
        if (!active) {
          e.currentTarget.style.background = "rgba(255,255,255,0.05)";
          e.currentTarget.style.color = "#e5f4ff";
        }
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.background = active ? "rgba(74, 179, 255, 0.1)" : "transparent";
        e.currentTarget.style.color = active ? "#e5f4ff" : "#9ca3af";
      }}
    >
      <span style={{ fontSize: "1.1rem" }}>{flag}</span>
      <span>{label}</span>
      {active && (
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="#4ab3ff"
          strokeWidth="2.5"
          style={{ marginLeft: "auto" }}
        >
          <path d="M20 6L9 17l-5-5" />
        </svg>
      )}
    </button>
  );
}

// =============================================================================
// COMING NEXT ITEM COMPONENT (visual-only, disabled)
// =============================================================================

function ComingNextItem({ label }: { label: string }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: "0.5rem",
        padding: "0.4rem 0.6rem",
        borderRadius: 6,
        background: "rgba(255, 255, 255, 0.02)",
        border: "1px solid rgba(255, 255, 255, 0.04)",
      }}
    >
      {/* Lock icon */}
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="#4a5568"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
        <path d="M7 11V7a5 5 0 0 1 10 0v4" />
      </svg>
      <span
        style={{
          fontSize: "0.8rem",
          color: "#5a6a7e",
        }}
      >
        {label}
      </span>
    </div>
  );
}
