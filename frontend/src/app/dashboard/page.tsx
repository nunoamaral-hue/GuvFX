"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import Link from "next/link";
import { AppShell } from "@/components/AppShell";

/**
 * Phase 1: Dashboard / Overview
 *
 * Scope: Routing, auth gating, stable shell only.
 * NO data fetching, NO KPIs, NO metrics.
 *
 * References:
 * - D-04: Navigation Contract (page always renders, no errors)
 * - D-09: Empty/first-time states handled gracefully
 *
 * AUTH BEHAVIOR (Investor-Grade):
 * - Best-effort session probe via /api/auth/me/ on mount.
 * - 200 response = authenticated, hide banner.
 * - 401 response = unauthenticated, show login banner.
 * - Network/CORS/other error = show soft "session unavailable" banner (not "not logged in").
 * - NEVER redirects. Shell always renders to avoid redirect loops.
 * - Does NOT use apiFetch to avoid its built-in 401 redirect behavior.
 */

// Auth session states
type SessionState =
  | "checking"      // Initial state, probe in progress
  | "authenticated" // 200 from /api/auth/me/
  | "unauthenticated" // 401 from /api/auth/me/
  | "unavailable";  // Network error, CORS, or other failure

export default function DashboardPage() {
  const pathname = usePathname();

  // Session state from best-effort probe
  const [sessionState, setSessionState] = useState<SessionState>("checking");

  // Best-effort session probe on mount
  useEffect(() => {
    let cancelled = false;

    const probeSession = async () => {
      try {
        // Use raw fetch to avoid apiFetch's built-in 401 redirect behavior.
        // We want to handle 401 gracefully without redirecting.
        const res = await fetch("/api/auth/me/", {
          method: "GET",
          credentials: "include", // Include cookies for session auth
        });

        if (cancelled) return;

        if (res.status === 200) {
          setSessionState("authenticated");
        } else if (res.status === 401) {
          setSessionState("unauthenticated");
        } else {
          // Unexpected status (403, 500, etc.) — treat as unavailable
          setSessionState("unavailable");
        }
      } catch {
        // Network error, CORS block, or other failure
        if (!cancelled) {
          setSessionState("unavailable");
        }
      }
    };

    probeSession();

    return () => {
      cancelled = true;
    };
  }, []);

  // Build the returnTo URL for the login link
  const returnTo = encodeURIComponent(pathname);

  // Always render AppShell + content (investor-stable, no redirects)
  return (
    <AppShell>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        {/* Auth banner: only shown for unauthenticated or unavailable states */}
        {sessionState === "unauthenticated" && (
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
                You are not logged in. Please sign in to access all features.
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

        {sessionState === "unavailable" && (
          <div
            style={{
              padding: "0.75rem 1rem",
              marginBottom: "1.25rem",
              borderRadius: 8,
              border: "1px solid rgba(148, 163, 184, 0.3)",
              background: "rgba(148, 163, 184, 0.05)",
              display: "flex",
              alignItems: "center",
              gap: "0.5rem",
            }}
          >
            {/* Info icon */}
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="#94a3b8"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="16" x2="12" y2="12" />
              <line x1="12" y1="8" x2="12.01" y2="8" />
            </svg>
            <span style={{ fontSize: "0.85rem", color: "#94a3b8" }}>
              Session status unavailable. Some features may require login.
            </span>
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
