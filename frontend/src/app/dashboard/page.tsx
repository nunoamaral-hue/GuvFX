"use client";

import { useEffect, useState, useCallback } from "react";
import { usePathname } from "next/navigation";
import Link from "next/link";
import { AppShell } from "@/components/AppShell";

/**
 * Phase 1: Dashboard / Overview
 *
 * Scope: Routing, auth gating, redirect logic, stable shell only.
 * NO data fetching, NO KPIs, NO metrics.
 *
 * References:
 * - D-04: Navigation Contract (page always renders, no errors)
 * - D-09: Empty/first-time states handled gracefully
 *
 * AUTH BEHAVIOR (Updated):
 * - localStorage tokens are checked as an INFORMATIONAL signal only.
 * - If tokens are missing, we show a non-blocking banner (no redirect).
 * - The shell always renders to avoid redirect loops on live (cookie-based auth).
 * - This keeps the dashboard investor-stable and consistent with /accounts behavior.
 */
export default function DashboardPage() {
  const pathname = usePathname();

  // null = still checking, true = has token, false = no token
  // This is INFORMATIONAL ONLY — not used for redirects.
  const [hasLocalToken, setHasLocalToken] = useState<boolean | null>(null);

  // Helper to check tokens (memoized for use in listeners)
  const checkTokens = useCallback(() => {
    if (typeof window === "undefined") return false;
    const hasAccess = !!window.localStorage.getItem("guvfx_access_token");
    const hasRefresh = !!window.localStorage.getItem("guvfx_refresh_token");
    return hasAccess || hasRefresh;
  }, []);

  // Check token state after hydration
  useEffect(() => {
    // Use setTimeout to avoid React state-update-during-render warnings
    const timerId = setTimeout(() => {
      setHasLocalToken(checkTokens());
    }, 0);

    // Listen for storage changes (e.g., login/logout in another tab)
    const handleStorage = () => {
      setTimeout(() => {
        setHasLocalToken(checkTokens());
      }, 0);
    };
    window.addEventListener("storage", handleStorage);

    return () => {
      clearTimeout(timerId);
      window.removeEventListener("storage", handleStorage);
    };
  }, [checkTokens]);

  // REMOVED: Hard redirect to /login when tokens are missing.
  // Live auth may be cookie-based, so localStorage absence is not authoritative.
  // The shell always renders to avoid redirect loops.

  // Build the returnTo URL for the login link (informational banner)
  const returnTo = encodeURIComponent(pathname);

  // Always render AppShell + content (investor-stable)
  return (
    <AppShell>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        {/* Non-blocking auth banner (only shown if localStorage tokens are missing) */}
        {hasLocalToken === false && (
          <div
            style={{
              padding: "0.75rem 1rem",
              marginBottom: "1.25rem",
              borderRadius: 8,
              border: "1px solid rgba(251, 191, 36, 0.4)",
              background: "rgba(251, 191, 36, 0.08)",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              flexWrap: "wrap",
              gap: "0.75rem",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              {/* Warning icon */}
              <svg
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                stroke="#fbbf24"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                <line x1="12" y1="9" x2="12" y2="13" />
                <line x1="12" y1="17" x2="12.01" y2="17" />
              </svg>
              <span style={{ fontSize: "0.85rem", color: "#fbbf24" }}>
                You may not be logged in. Some features may be unavailable.
              </span>
            </div>
            <Link
              href={`/login?returnTo=${returnTo}`}
              style={{
                fontSize: "0.8rem",
                fontWeight: 500,
                color: "#e5f4ff",
                padding: "0.4rem 0.85rem",
                borderRadius: 6,
                background: "rgba(251, 191, 36, 0.2)",
                border: "1px solid rgba(251, 191, 36, 0.4)",
                textDecoration: "none",
                whiteSpace: "nowrap",
                transition: "background 150ms ease",
              }}
            >
              Log in
            </Link>
          </div>
        )}

        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Dashboard</h1>
        <p
          style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1.5rem" }}
        >
          Welcome to GuvFX. This is your trading overview.
        </p>

        {/* Stable placeholder content - Phase 1 only */}
        <div
          style={{
            padding: "2rem",
            borderRadius: 12,
            border: "1px solid #1f2937",
            background: "rgba(15, 23, 42, 0.5)",
            textAlign: "center",
          }}
        >
          <p style={{ color: "#9ca3af", fontSize: "0.9rem", margin: 0 }}>
            Your dashboard overview will appear here.
          </p>
          <p
            style={{ color: "#6b7280", fontSize: "0.8rem", marginTop: "0.5rem" }}
          >
            Use the navigation to explore strategies, accounts, and backtests.
          </p>
        </div>
      </div>
    </AppShell>
  );
}
