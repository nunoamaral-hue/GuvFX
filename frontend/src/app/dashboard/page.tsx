"use client";

import { useEffect, useState, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import Link from "next/link";
import { AppShell } from "@/components/AppShell";

/**
 * Dashboard v1 — Control Center → Insight
 *
 * Real data dashboard with:
 * - System status (API + session state)
 * - Accounts overview (fetched from API)
 * - Quick actions
 *
 * AUTH BEHAVIOR (Investor-Grade):
 * - Best-effort session probe via /api/auth/me/ on mount.
 * - 200 response = authenticated, fetch accounts.
 * - 401 response = unauthenticated, show login prompts.
 * - Network/CORS/other error = show soft "unavailable" state.
 * - NEVER redirects. Shell always renders to avoid redirect loops.
 * - Does NOT use apiFetch to avoid its built-in 401 redirect behavior.
 */

const API_BASE = "https://api.guvfx.com";

// =============================================================================
// TYPES
// =============================================================================

type SessionState =
  | "checking"
  | "authenticated"
  | "unauthenticated"
  | "unavailable";

type AccountsState =
  | "idle"
  | "loading"
  | "loaded"
  | "unauthorized"
  | "error";

type TradingAccount = {
  id: number;
  name: string;
  broker_name?: string;
  server_name?: string;
  account_number: string;
  is_active?: boolean;
  is_demo?: boolean;
};

// =============================================================================
// INLINE ICONS
// =============================================================================

function CheckCircleIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
      <polyline points="22 4 12 14.01 9 11.01" />
    </svg>
  );
}

function XCircleIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="10" />
      <line x1="15" y1="9" x2="9" y2="15" />
      <line x1="9" y1="9" x2="15" y2="15" />
    </svg>
  );
}

function AlertCircleIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="8" x2="12" y2="12" />
      <line x1="12" y1="16" x2="12.01" y2="16" />
    </svg>
  );
}

function UserIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}

function ZapIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
    </svg>
  );
}

function GridIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <rect x="3" y="3" width="7" height="7" />
      <rect x="14" y="3" width="7" height="7" />
      <rect x="14" y="14" width="7" height="7" />
      <rect x="3" y="14" width="7" height="7" />
    </svg>
  );
}

function ServerIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <rect x="2" y="2" width="20" height="8" rx="2" ry="2" />
      <rect x="2" y="14" width="20" height="8" rx="2" ry="2" />
      <line x1="6" y1="6" x2="6.01" y2="6" />
      <line x1="6" y1="18" x2="6.01" y2="18" />
    </svg>
  );
}

function ActivityIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
    </svg>
  );
}

// =============================================================================
// COMPONENT: Status Badge
// =============================================================================

type StatusBadgeProps = {
  status: "good" | "warning" | "error" | "neutral";
  label: string;
};

function StatusBadge({ status, label }: StatusBadgeProps) {
  const colors = {
    good: { bg: "rgba(34, 197, 94, 0.15)", border: "rgba(34, 197, 94, 0.4)", text: "#22c55e" },
    warning: { bg: "rgba(251, 191, 36, 0.15)", border: "rgba(251, 191, 36, 0.4)", text: "#fbbf24" },
    error: { bg: "rgba(239, 68, 68, 0.15)", border: "rgba(239, 68, 68, 0.4)", text: "#ef4444" },
    neutral: { bg: "rgba(148, 163, 184, 0.1)", border: "rgba(148, 163, 184, 0.3)", text: "#94a3b8" },
  };

  const c = colors[status];

  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "0.35rem",
        padding: "0.25rem 0.6rem",
        borderRadius: 6,
        fontSize: "0.75rem",
        fontWeight: 500,
        background: c.bg,
        border: `1px solid ${c.border}`,
        color: c.text,
      }}
    >
      {status === "good" && <CheckCircleIcon />}
      {status === "warning" && <AlertCircleIcon />}
      {status === "error" && <XCircleIcon />}
      {status === "neutral" && <AlertCircleIcon />}
      {label}
    </span>
  );
}

// =============================================================================
// COMPONENT: Dashboard Card
// =============================================================================

type CardProps = {
  title: string;
  icon?: ReactNode;
  children: ReactNode;
};

function Card({ title, icon, children }: CardProps) {
  return (
    <div
      style={{
        padding: "1.25rem",
        borderRadius: 12,
        border: "1px solid rgba(255, 255, 255, 0.08)",
        background: "rgba(15, 23, 42, 0.5)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
          marginBottom: "1rem",
          color: "#e5f4ff",
          fontSize: "0.95rem",
          fontWeight: 600,
        }}
      >
        {icon && <span style={{ color: "#64748b" }}>{icon}</span>}
        {title}
      </div>
      {children}
    </div>
  );
}

// =============================================================================
// COMPONENT: Quick Action Button
// =============================================================================

type QuickActionProps = {
  href: string;
  icon: ReactNode;
  label: string;
};

function QuickAction({ href, icon, label }: QuickActionProps) {
  return (
    <Link
      href={href}
      style={{
        display: "flex",
        alignItems: "center",
        gap: "0.5rem",
        padding: "0.6rem 0.9rem",
        borderRadius: 8,
        border: "1px solid rgba(255, 255, 255, 0.1)",
        background: "rgba(255, 255, 255, 0.03)",
        color: "#e5f4ff",
        fontSize: "0.85rem",
        textDecoration: "none",
        transition: "background 150ms ease, border-color 150ms ease",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.background = "rgba(255, 255, 255, 0.06)";
        e.currentTarget.style.borderColor = "rgba(255, 255, 255, 0.15)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.background = "rgba(255, 255, 255, 0.03)";
        e.currentTarget.style.borderColor = "rgba(255, 255, 255, 0.1)";
      }}
    >
      <span style={{ color: "#64748b" }}>{icon}</span>
      {label}
    </Link>
  );
}

// =============================================================================
// MAIN COMPONENT
// =============================================================================

export default function DashboardPage() {
  const pathname = usePathname();

  // Session state from best-effort probe
  const [sessionState, setSessionState] = useState<SessionState>("checking");

  // Accounts state
  const [accountsState, setAccountsState] = useState<AccountsState>("idle");
  const [accounts, setAccounts] = useState<TradingAccount[]>([]);

  // Best-effort session probe on mount
  useEffect(() => {
    let cancelled = false;

    const probeSession = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/auth/me/`, {
          method: "GET",
          credentials: "include",
        });

        if (cancelled) return;

        if (res.status === 200) {
          setSessionState("authenticated");
        } else if (res.status === 401) {
          setSessionState("unauthenticated");
        } else {
          setSessionState("unavailable");
        }
      } catch {
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

  // Fetch accounts when authenticated
  useEffect(() => {
    if (sessionState !== "authenticated") {
      return;
    }

    let cancelled = false;

    const fetchAccounts = async () => {
      // Set loading state inside async function to satisfy lint
      if (!cancelled) {
        setAccountsState("loading");
      }

      try {
        const res = await fetch(`${API_BASE}/api/trading/accounts/`, {
          method: "GET",
          credentials: "include",
        });

        if (cancelled) return;

        if (res.status === 200) {
          const data = await res.json();
          setAccounts(Array.isArray(data) ? data : []);
          setAccountsState("loaded");
        } else if (res.status === 401 || res.status === 403) {
          setAccountsState("unauthorized");
        } else {
          setAccountsState("error");
        }
      } catch {
        if (!cancelled) {
          setAccountsState("error");
        }
      }
    };

    fetchAccounts();

    return () => {
      cancelled = true;
    };
  }, [sessionState]);

  // Build the returnTo URL for the login link
  const returnTo = encodeURIComponent(pathname);

  // Derive statuses for display
  const apiStatus: "good" | "warning" | "error" | "neutral" =
    sessionState === "checking"
      ? "neutral"
      : sessionState === "unavailable"
        ? "error"
        : "good";

  const sessionStatus: "good" | "warning" | "error" | "neutral" =
    sessionState === "checking"
      ? "neutral"
      : sessionState === "authenticated"
        ? "good"
        : sessionState === "unauthenticated"
          ? "warning"
          : "error";

  // Always render AppShell + content (investor-stable, no redirects)
  return (
    <AppShell>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        {/* Auth banner for unauthenticated users */}
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
              <AlertCircleIcon />
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
              }}
            >
              Log in
            </Link>
          </div>
        )}

        {/* Header */}
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Dashboard</h1>
        <p style={{ fontSize: "0.9rem", color: "#94a3b8", marginBottom: "1.5rem" }}>
          Unified trading intelligence across accounts and strategies.
        </p>

        {/* Responsive grid: 2 columns on desktop, stacked on mobile */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
            gap: "1rem",
          }}
        >
          {/* System Status Card */}
          <Card title="System Status" icon={<ServerIcon />}>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                }}
              >
                <span style={{ fontSize: "0.85rem", color: "#94a3b8" }}>API</span>
                <StatusBadge
                  status={apiStatus}
                  label={
                    sessionState === "checking"
                      ? "Checking..."
                      : sessionState === "unavailable"
                        ? "Unavailable"
                        : "Online"
                  }
                />
              </div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                }}
              >
                <span style={{ fontSize: "0.85rem", color: "#94a3b8" }}>Session</span>
                <StatusBadge
                  status={sessionStatus}
                  label={
                    sessionState === "checking"
                      ? "Checking..."
                      : sessionState === "authenticated"
                        ? "Authenticated"
                        : sessionState === "unauthenticated"
                          ? "Login required"
                          : "Unknown"
                  }
                />
              </div>
            </div>
          </Card>

          {/* Quick Actions Card */}
          <Card title="Quick Actions" icon={<ZapIcon />}>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
              <QuickAction href="/accounts" icon={<PlusIcon />} label="Link Account" />
              <QuickAction href="/strategies/create" icon={<ZapIcon />} label="Create Strategy" />
              <QuickAction href="/strategies/marketplace" icon={<GridIcon />} label="Explore Marketplace" />
            </div>
          </Card>

          {/* Signals Card - account summary metrics */}
          <Card title="Signals" icon={<ActivityIcon />}>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                }}
              >
                <span style={{ fontSize: "0.85rem", color: "#94a3b8" }}>Accounts linked</span>
                <span style={{ fontSize: "0.85rem", fontWeight: 500, color: "#e5f4ff" }}>
                  {accountsState === "loaded" ? accounts.length : "—"}
                </span>
              </div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                }}
              >
                <span style={{ fontSize: "0.85rem", color: "#94a3b8" }}>Active accounts</span>
                <span style={{ fontSize: "0.85rem", fontWeight: 500, color: "#e5f4ff" }}>
                  {accountsState === "loaded"
                    ? accounts.some((a) => a.is_active !== undefined)
                      ? accounts.filter((a) => a.is_active === true).length
                      : "—"
                    : "—"}
                </span>
              </div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                }}
              >
                <span style={{ fontSize: "0.85rem", color: "#94a3b8" }}>Demo accounts</span>
                <span style={{ fontSize: "0.85rem", fontWeight: 500, color: "#e5f4ff" }}>
                  {accountsState === "loaded"
                    ? accounts.some((a) => a.is_demo !== undefined)
                      ? accounts.filter((a) => a.is_demo === true).length
                      : "—"
                    : "—"}
                </span>
              </div>
            </div>
          </Card>

          {/* Accounts Card - spans full width on larger screens */}
          <div style={{ gridColumn: "1 / -1" }}>
            <Card title="Trading Accounts" icon={<UserIcon />}>
              {/* Loading state - skeleton placeholder */}
              {(accountsState === "idle" || accountsState === "loading") &&
                sessionState === "authenticated" && (
                  <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                    {[1, 2].map((i) => (
                      <div
                        key={i}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          padding: "0.6rem 0.75rem",
                          borderRadius: 8,
                          background: "rgba(255, 255, 255, 0.02)",
                          border: "1px solid rgba(255, 255, 255, 0.04)",
                        }}
                      >
                        <div style={{ minWidth: 0, flex: 1 }}>
                          <div
                            style={{
                              height: 14,
                              width: "40%",
                              background: "rgba(255, 255, 255, 0.06)",
                              borderRadius: 4,
                              marginBottom: 6,
                              animation: "pulse 1.5s ease-in-out infinite",
                            }}
                          />
                          <div
                            style={{
                              height: 10,
                              width: "60%",
                              background: "rgba(255, 255, 255, 0.04)",
                              borderRadius: 3,
                              animation: "pulse 1.5s ease-in-out infinite",
                              animationDelay: "0.2s",
                            }}
                          />
                        </div>
                        <div
                          style={{
                            height: 22,
                            width: 60,
                            background: "rgba(255, 255, 255, 0.04)",
                            borderRadius: 6,
                            animation: "pulse 1.5s ease-in-out infinite",
                            animationDelay: "0.4s",
                          }}
                        />
                      </div>
                    ))}
                    <style>{`
                      @keyframes pulse {
                        0%, 100% { opacity: 1; }
                        50% { opacity: 0.4; }
                      }
                    `}</style>
                  </div>
                )}

              {/* Unauthorized state */}
              {(accountsState === "unauthorized" ||
                (sessionState === "unauthenticated" && accountsState === "idle")) && (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    flexWrap: "wrap",
                    gap: "0.75rem",
                  }}
                >
                  <span style={{ color: "#94a3b8", fontSize: "0.85rem" }}>
                    Login required to view accounts.
                  </span>
                  <Link
                    href={`/login?returnTo=${returnTo}`}
                    style={{
                      fontSize: "0.8rem",
                      fontWeight: 500,
                      color: "#3b82f6",
                      textDecoration: "none",
                    }}
                  >
                    Sign in →
                  </Link>
                </div>
              )}

              {/* Session unavailable state */}
              {sessionState === "unavailable" && accountsState === "idle" && (
                <div style={{ color: "#94a3b8", fontSize: "0.85rem" }}>
                  Unable to load accounts right now.
                </div>
              )}

              {/* Error state */}
              {accountsState === "error" && (
                <div style={{ color: "#ef4444", fontSize: "0.85rem" }}>
                  Unable to load accounts right now.
                </div>
              )}

              {/* Loaded state - empty */}
              {accountsState === "loaded" && accounts.length === 0 && (
                <div
                  style={{
                    padding: "1.5rem",
                    textAlign: "center",
                    borderRadius: 8,
                    background: "rgba(255, 255, 255, 0.02)",
                    border: "1px dashed rgba(255, 255, 255, 0.1)",
                  }}
                >
                  <div style={{ marginBottom: "0.5rem" }}>
                    <UserIcon />
                  </div>
                  <div style={{ color: "#e5f4ff", fontSize: "0.9rem", fontWeight: 500, marginBottom: "0.35rem" }}>
                    No trading accounts linked
                  </div>
                  <p style={{ color: "#64748b", fontSize: "0.8rem", marginBottom: "1rem", lineHeight: 1.5 }}>
                    Connect your first broker account to start tracking performance and deploying strategies.
                  </p>
                  <Link
                    href="/accounts"
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "0.35rem",
                      fontSize: "0.8rem",
                      fontWeight: 500,
                      color: "#e5f4ff",
                      padding: "0.5rem 1rem",
                      borderRadius: 6,
                      background: "rgba(59, 130, 246, 0.15)",
                      border: "1px solid rgba(59, 130, 246, 0.3)",
                      textDecoration: "none",
                    }}
                  >
                    <PlusIcon /> Link Account
                  </Link>
                </div>
              )}

              {/* Loaded state - with accounts */}
              {accountsState === "loaded" && accounts.length > 0 && (
                <div>
                  {/* Summary */}
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      marginBottom: "0.75rem",
                    }}
                  >
                    <span style={{ color: "#94a3b8", fontSize: "0.85rem" }}>
                      {accounts.length} account{accounts.length !== 1 ? "s" : ""} linked
                    </span>
                    <Link
                      href="/accounts"
                      style={{
                        fontSize: "0.8rem",
                        fontWeight: 500,
                        color: "#3b82f6",
                        textDecoration: "none",
                      }}
                    >
                      Manage →
                    </Link>
                  </div>

                  {/* Account list (up to 3) */}
                  <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                    {accounts.slice(0, 3).map((acc) => (
                      <div
                        key={acc.id}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          padding: "0.6rem 0.75rem",
                          borderRadius: 8,
                          background: acc.is_active
                            ? "rgba(34, 197, 94, 0.06)"
                            : "rgba(255, 255, 255, 0.03)",
                          border: acc.is_active
                            ? "1px solid rgba(34, 197, 94, 0.2)"
                            : "1px solid rgba(255, 255, 255, 0.06)",
                        }}
                      >
                        <div style={{ minWidth: 0 }}>
                          <div
                            style={{
                              fontSize: "0.85rem",
                              fontWeight: 500,
                              color: "#e5f4ff",
                              whiteSpace: "nowrap",
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                            }}
                          >
                            {acc.name}
                          </div>
                          <div
                            style={{
                              fontSize: "0.75rem",
                              color: "#64748b",
                              marginTop: "0.15rem",
                            }}
                          >
                            {acc.server_name || acc.broker_name || "—"} · {acc.account_number}
                          </div>
                        </div>
                        {acc.is_active !== undefined && (
                          <StatusBadge
                            status={acc.is_active ? "good" : "neutral"}
                            label={acc.is_active ? "Active" : "Inactive"}
                          />
                        )}
                      </div>
                    ))}

                    {/* Show "and X more" if there are more than 3 */}
                    {accounts.length > 3 && (
                      <div
                        style={{
                          fontSize: "0.8rem",
                          color: "#64748b",
                          textAlign: "center",
                          paddingTop: "0.25rem",
                        }}
                      >
                        and {accounts.length - 3} more...
                      </div>
                    )}
                  </div>
                </div>
              )}
            </Card>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
