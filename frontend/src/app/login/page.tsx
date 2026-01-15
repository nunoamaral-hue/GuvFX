"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

export default function LoginPage() {
  const router = useRouter();
  
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [infoMessage, setInfoMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  useEffect(() => {
    // Avoid next/navigation useSearchParams build requirement by reading from location directly.
    const reason = new URLSearchParams(window.location.search).get("reason");

    if (reason === "expired" || reason === "token_expired") {
      setInfoMessage("Your token has expired, please login again.");
    } else if (reason === "unauthenticated") {
      setInfoMessage("Please log in to continue.");
    } else if (reason === "logged_out") {
      setInfoMessage("You have been logged out.");
    } else {
      setInfoMessage(null);
    }
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSuccess(null);

    if (!email || !password) {
      setError("Please enter your email and password.");
      return;
    }

    setLoading(true);
    try {
      const body = {
        email,
        username: email,
        password,
      };

      const res = await fetch("https://api.guvfx.com/api/auth/cookie/login/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(body),
      });

      if (!res.ok) {
        const t = await res.text();
        throw new Error(t || "Login failed. Please check your credentials.");
      }

      setSuccess("Logged in successfully. Redirecting…");
      // Small delay to show message, then go to strategies list
      setTimeout(() => {
        router.push("/strategies");
      }, 700);
    } catch (err: unknown) {
      console.error(err);
      const message =
        err instanceof Error
          ? err.message
          : "Login failed. Please check your credentials.";
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
        background:
          "radial-gradient(circle at 0 0, #12263f 0, #050816 40%, #050816 100%)",
        color: "#e5f4ff",
        fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
      }}
    >
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
            Welcome back to
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
            Log in to manage strategies, review backtests, and get AI-powered
            guidance on your trading.
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
              onClick={() => {
                const el = document.getElementById("login-panel");
                if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
              }}
            >
              Log in
            </button>
            <button
              style={{
                padding: "0.9rem 1.8rem",
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
              Go to Sign up
            </button>
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
          id="login-panel"
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
            Log in
          </h2>
          <p
            style={{
              fontSize: "0.8rem",
              color: "#8fa0b7",
              margin: 0,
              marginBottom: "1.2rem",
            }}
          >
            Welcome back – enter your GuvFX credentials.
          </p>

          {/* Progress bar (full for login) */}
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
                width: "100%",
                height: "100%",
                background:
                  "linear-gradient(90deg, #4ab3ff 0%, #7af0ff 100%)",
              }}
            />
          </div>

          {/* Messages */}
          {infoMessage && (
            <div
              style={{
                background: "rgba(74, 179, 255, 0.08)",
                border: "1px solid rgba(74, 179, 255, 0.4)",
                borderRadius: 8,
                padding: "0.5rem 0.75rem",
                marginBottom: "0.75rem",
                fontSize: "0.8rem",
                color: "#c9ecff",
              }}
            >
              {infoMessage}
            </div>
          )}

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
              Email
            </label>
            <input
              id="email"
              type="email"
              required
              placeholder="Email"
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
              Password
            </label>
            <input
              id="password"
              type="password"
              required
              placeholder="Your password"
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
              {loading ? "Logging in..." : "Continue"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
