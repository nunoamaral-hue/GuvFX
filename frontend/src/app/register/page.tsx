"use client";

import { useState } from "react";
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

  const toggleLang = () => {
    const next: Lang = lang === "en" ? "ja" : "en";
    persistLang(next);
    setLangState(next);
  };

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

  const scrollToForm = () => {
    const el = document.getElementById("create-account");
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      const emailInput = document.getElementById("email");
      if (emailInput instanceof HTMLInputElement) {
        emailInput.focus();
      }
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

          <div style={{ marginTop: "2.5rem", display: "flex", gap: "1rem" }}>
            <button
              style={{
                padding: "0.9rem 2.4rem",
                borderRadius: 999,
                border: "none",
                fontSize: "1rem",
                fontWeight: 500,
                cursor: "pointer",
                background:
                  "linear-gradient(135deg, #2979ff 0%, #3fe0ff 50%, #2979ff 100%)",
                color: "#ffffff",
                boxShadow: "0 12px 30px rgba(0, 0, 0, 0.5)",
              }}
              onClick={scrollToForm}
            >
              {t(lang, "register.getStarted")}
            </button>
            <button
              onClick={toggleLang}
              style={{
                padding: "0.9rem 1.4rem",
                borderRadius: 999,
                border: "1px solid rgba(255,255,255,0.18)",
                fontSize: "1rem",
                fontWeight: 500,
                cursor: "pointer",
                background: "transparent",
                color: "#c2d5ff",
              }}
            >
              {lang === "en" ? "日本語" : "EN"}
            </button>
          </div>

          <div
            style={{
              marginTop: "1.5rem",
              fontSize: "0.9rem",
              color: "#7e8ea5",
              display: "flex",
              alignItems: "center",
              gap: "1rem",
            }}
          >
            <span
              style={{ cursor: "pointer" }}
              onClick={() => router.push("/login")}
            >
              {t(lang, "register.login")}
            </span>
            <span style={{ opacity: 0.4 }}>|</span>
            <span style={{ cursor: "pointer" }} onClick={scrollToForm}>
              {t(lang, "register.signUp")}
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
          <p
            style={{
              fontSize: "0.8rem",
              color: "#8fa0b7",
              margin: 0,
              marginBottom: "1.2rem",
            }}
          >
            {t(lang, "register.step")}
          </p>

          {/* Progress bar */}
          <div
            style={{
              height: 4,
              borderRadius: 999,
              background: "rgba(255,255,255,0.06)",
              overflow: "hidden",
              marginBottom: "1.5rem",
            }}
          >
            <div
              style={{
                width: "8.5%",
                height: "100%",
                background:
                  "linear-gradient(90deg, #4ab3ff 0%, #7af0ff 100%)",
              }}
            />
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
        </div>
      </div>
      </div>
      <LegalFooter lang={lang} />
    </div>
  );
}
