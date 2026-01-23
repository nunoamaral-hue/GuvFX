"use client";

import { useEffect, useState, useCallback } from "react";
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

  // null = still checking, true = has token, false = no token
  const [authState, setAuthState] = useState<boolean | null>(null);

  // Helper to check tokens (memoized for use in listeners)
  const checkAuth = useCallback(() => {
    const hasAccess = !!window.localStorage.getItem("guvfx_access_token");
    const hasRefresh = !!window.localStorage.getItem("guvfx_refresh_token");
    return hasAccess || hasRefresh;
  }, []);

  // Check auth state after hydration using setTimeout to satisfy eslint
  useEffect(() => {
    const timerId = setTimeout(() => {
      setAuthState(checkAuth());
    }, 0);

    // Listen for storage changes (e.g., login/logout in another tab)
    const handleStorage = () => {
      setTimeout(() => {
        setAuthState(checkAuth());
      }, 0);
    };
    window.addEventListener("storage", handleStorage);

    return () => {
      clearTimeout(timerId);
      window.removeEventListener("storage", handleStorage);
    };
  }, [checkAuth]);

  // Redirect if unauthenticated (only after auth check completes)
  useEffect(() => {
    if (authState === false) {
      const returnTo = encodeURIComponent(pathname);
      router.replace(`/login?reason=unauthenticated&returnTo=${returnTo}`);
    }
  }, [authState, router, pathname]);

  // Show nothing while checking auth or redirect is pending
  if (authState !== true) {
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