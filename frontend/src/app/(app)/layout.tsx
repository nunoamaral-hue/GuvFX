"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { detectLang, t } from "@/lib/i18n";

/**
 * App Layout — Wraps all authenticated routes in AppShell.
 *
 * Routes under (app)/ are the main application surface:
 *   /dashboard, /accounts, /strategies/*, /backtests/*, /charts,
 *   /profile, /trading/*, /analytics/*, /admin/*
 *
 * AppShell provides: sidebar navigation, header bar, auth context,
 * language context, and the legal footer.
 *
 * Individual page files should NOT import or wrap with AppShell.
 *
 * AuthGate: Before rendering any app content, probes /api/auth/me/
 * via cookie session. On 401/403 the user is bounced to /login
 * with history replacement (no back-button access to protected page).
 */

// =============================================================================
// AUTH GATE — blocks rendering until cookie-session is verified
// =============================================================================

type AuthState = "checking" | "ok" | "error";

const API_BASE = "https://api.guvfx.com";

function isLocalhostEnv(): boolean {
  if (typeof window === "undefined") return false;
  const h = window.location.hostname;
  return h === "localhost" || h === "127.0.0.1" || h === "0.0.0.0";
}

function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  // Lazy initial state: skip auth probe on localhost (dev) — no backend available.
  // This avoids calling setState synchronously inside the effect (lint: set-state-in-effect).
  const [authState, setAuthState] = useState<AuthState>(() =>
    isLocalhostEnv() ? "ok" : "checking"
  );

  useEffect(() => {
    // On localhost we initialised as "ok" — nothing to do.
    if (isLocalhostEnv()) return;

    let cancelled = false;

    async function checkAuth() {
      try {
        const res = await fetch(`${API_BASE}/api/auth/me/`, {
          method: "GET",
          credentials: "include",
        });

        if (cancelled) return;

        if (res.ok) {
          setAuthState("ok");
        } else if (res.status === 401 || res.status === 403) {
          // Not authenticated — bounce to login with history replacement.
          // Uses pathname at call time via closure.
          const returnTo = encodeURIComponent(pathname);
          window.location.replace(`/login?reason=unauthenticated&returnTo=${returnTo}`);
        } else {
          // Unexpected error — show fallback, don't redirect
          setAuthState("error");
        }
      } catch {
        if (!cancelled) {
          // Network error — show fallback with login link
          setAuthState("error");
        }
      }
    }

    checkAuth();

    return () => {
      cancelled = true;
    };
  // Auth check runs once on mount. Subsequent navigations within (app)/
  // routes don't need re-checking because the session cookie persists.
  // If the session expires mid-use, apiFetch's 401 handler will redirect.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // -------------------------------------------------------------------------
  // Loading skeleton — shown while auth probe is in flight
  // -------------------------------------------------------------------------
  if (authState === "checking") {
    return (
      <div
        style={{
          minHeight: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background:
            "radial-gradient(circle at top left, #0b1020, #050713 55%, #030612 100%)",
          color: "#6b7280",
          fontSize: "0.9rem",
        }}
      >
        <div style={{ textAlign: "center" }}>
          <div
            style={{
              width: 32,
              height: 32,
              border: "3px solid rgba(255,255,255,0.1)",
              borderTopColor: "#3b82f6",
              borderRadius: "50%",
              animation: "authspin 0.8s linear infinite",
              margin: "0 auto 1rem auto",
            }}
          />
          <style>{`@keyframes authspin { to { transform: rotate(360deg); } }`}</style>
          <span>Verifying session...</span>
        </div>
      </div>
    );
  }

  // -------------------------------------------------------------------------
  // Error fallback — network failure / unexpected backend error
  // -------------------------------------------------------------------------
  if (authState === "error") {
    const lang = detectLang();
    return (
      <div
        style={{
          minHeight: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background:
            "radial-gradient(circle at top left, #0b1020, #050713 55%, #030612 100%)",
          color: "#e5f4ff",
          fontSize: "0.9rem",
        }}
      >
        <div
          style={{
            textAlign: "center",
            maxWidth: 420,
            padding: "2rem",
            background: "rgba(15,23,42,0.85)",
            border: "1px solid rgba(255,255,255,0.1)",
            borderRadius: 12,
          }}
        >
          <div style={{ fontSize: "1.1rem", fontWeight: 600, marginBottom: "0.75rem" }}>
            {t(lang, "auth.sessionError")}
          </div>
          <p style={{ color: "#9ca3af", lineHeight: 1.5, marginBottom: "1.25rem" }}>
            {t(lang, "auth.sessionErrorBody")}
          </p>
          <a
            href="/login"
            style={{
              display: "inline-block",
              padding: "0.6rem 1.5rem",
              background: "linear-gradient(135deg, #1d4ed8, #22c1c3)",
              color: "#fff",
              borderRadius: 8,
              textDecoration: "none",
              fontWeight: 500,
              fontSize: "0.85rem",
            }}
          >
            {t(lang, "auth.goToLogin")}
          </a>
        </div>
      </div>
    );
  }

  // -------------------------------------------------------------------------
  // Authenticated — render app content
  // -------------------------------------------------------------------------
  return <>{children}</>;
}

// =============================================================================
// LAYOUT — AuthGate wraps AppShell wraps page content
// =============================================================================

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGate>
      <AppShell>{children}</AppShell>
    </AuthGate>
  );
}
