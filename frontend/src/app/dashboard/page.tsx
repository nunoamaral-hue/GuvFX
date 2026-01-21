"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
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
 */
export default function DashboardPage() {
  const router = useRouter();
  const pathname = usePathname();

  // Derive auth state once on mount via lazy initializer (avoids setState in effect)
  // User is authenticated if EITHER access token OR refresh token exists
  const [hasToken] = useState(() => {
    if (typeof window === "undefined") return false;
    const hasAccess = !!window.localStorage.getItem("guvfx_access_token");
    const hasRefresh = !!window.localStorage.getItem("guvfx_refresh_token");
    return hasAccess || hasRefresh;
  });

  // Redirect if unauthenticated (effect only redirects, no setState)
  useEffect(() => {
    if (!hasToken) {
      const returnTo = encodeURIComponent(pathname);
      router.replace(`/login?reason=unauthenticated&returnTo=${returnTo}`);
    }
  }, [hasToken, router, pathname]);

  // Show nothing while redirect is pending
  if (!hasToken) {
    return null;
  }

  // Stable empty shell (D-04: always renders, D-09: graceful empty state)
  return (
    <AppShell>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>
          Dashboard
        </h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1.5rem" }}>
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
          <p style={{ color: "#6b7280", fontSize: "0.8rem", marginTop: "0.5rem" }}>
            Use the navigation to explore strategies, accounts, and backtests.
          </p>
        </div>
      </div>
    </AppShell>
  );
}