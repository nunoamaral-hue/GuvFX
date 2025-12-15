"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { Card } from "@/components/ui/Card";
import { Alert } from "@/components/ui/Alert";
import { apiFetch } from "@/lib/api";

type StrategySummary = {
  id: number;
  name: string;
  style: string | null;
  timeframe: string;
  symbol_universe: string;
  is_active: boolean;
  auto_optimize_by_ai: boolean;
  created_at: string;
  updated_at: string;
};

type DashboardStats = {
  totalStrategies: number;
  activeStrategies: number;
  totalAccounts: number;
};

export default function DashboardPage() {
  const router = useRouter();

  const [accessToken, setAccessToken] = useState<string>("");
  const [strategies, setStrategies] = useState<StrategySummary[]>([]);
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Load token from localStorage
  useEffect(() => {
    if (typeof window !== "undefined") {
      const stored = window.localStorage.getItem("guvfx_access_token");
      if (stored) {
        setAccessToken(stored);
      } else {
        // no token -> force login with message
        router.replace("/login?reason=unauthenticated");
      }
    }
  }, [router]);

  // Fetch strategies + accounts for summary
  useEffect(() => {
    if (!accessToken) return;

    const fetchData = async () => {
      setLoading(true);
      setError(null);
      try {
        const [strategyData, accountData] = await Promise.all([
          apiFetch<StrategySummary[]>(
            "/api/strategies/strategies/",
            {},
            accessToken
          ),
          apiFetch<{ id: number }[]>(
            "/api/trading/accounts/",
            {},
            accessToken
          ),
        ]);

        setStrategies(strategyData);

        const activeCount = strategyData.filter((s) => s.is_active).length;

        setStats({
          totalStrategies: strategyData.length,
          activeStrategies: activeCount,
          totalAccounts: accountData.length,
        });
      } catch (err: unknown) {
        console.error(err);
        const message =
          err instanceof Error ? err.message : "Failed to load dashboard data.";
        setError(message);
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, [accessToken]);

  return (
    <AppShell>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>
          Trading overview
        </h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
          High-level view of your strategies and accounts. Use this as your
          starting point before diving into individual strategies.
        </p>

        {error && <Alert type="error">{error}</Alert>}

        {/* Top stats */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
            gap: "1rem",
            marginBottom: "1.5rem",
          }}
        >
          <Card title="Strategies">
            {loading && !stats ? (
              <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
                Loading…
              </p>
            ) : (
              <div style={{ fontSize: "0.9rem" }}>
                <div style={{ marginBottom: "0.3rem" }}>
                  <span style={{ color: "#9ca3af" }}>Total:</span>{" "}
                  <span style={{ fontWeight: 600 }}>
                    {stats?.totalStrategies ?? 0}
                  </span>
                </div>
                <div>
                  <span style={{ color: "#9ca3af" }}>Active:</span>{" "}
                  <span style={{ fontWeight: 600, color: "#4ade80" }}>
                    {stats?.activeStrategies ?? 0}
                  </span>
                </div>
              </div>
            )}
          </Card>

          <Card title="Trading accounts">
            {loading && !stats ? (
              <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
                Loading…
              </p>
            ) : (
              <div style={{ fontSize: "0.9rem" }}>
                <div>
                  <span style={{ color: "#9ca3af" }}>Total linked accounts:</span>{" "}
                  <span style={{ fontWeight: 600 }}>
                    {stats?.totalAccounts ?? 0}
                  </span>
                </div>
                <p
                  style={{
                    fontSize: "0.78rem",
                    color: "#7c8ca4",
                    marginTop: "0.3rem",
                  }}
                >
                  Manage accounts from the Broker Accounts section.
                </p>
              </div>
            )}
          </Card>

          <Card title="Quick actions">
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "0.5rem",
                fontSize: "0.85rem",
              }}
            >
              <button
                type="button"
                onClick={() => router.push("/strategies/create")}
                style={{
                  borderRadius: 999,
                  border: "none",
                  padding: "0.45rem 0.8rem",
                  background:
                    "linear-gradient(90deg, rgba(37,99,235,1), rgba(56,189,248,1))",
                  color: "#f9fafb",
                  cursor: "pointer",
                  fontSize: "0.85rem",
                }}
              >
                + Create strategy
              </button>
              <button
                type="button"
                onClick={() => router.push("/strategies")}
                style={{
                  borderRadius: 999,
                  border: "1px solid rgba(148,163,184,0.6)",
                  padding: "0.45rem 0.8rem",
                  background: "rgba(15,23,42,0.9)",
                  color: "#e5e7eb",
                  cursor: "pointer",
                  fontSize: "0.85rem",
                }}
              >
                View all strategies
              </button>
              <button
                type="button"
                onClick={() => router.push("/backtests")}
                style={{
                  borderRadius: 999,
                  border: "1px solid rgba(148,163,184,0.6)",
                  padding: "0.45rem 0.8rem",
                  background: "rgba(15,23,42,0.9)",
                  color: "#e5e7eb",
                  cursor: "pointer",
                  fontSize: "0.85rem",
                }}
              >
                View backtests
              </button>
            </div>
          </Card>
        </div>

        {/* Strategies overview */}
        <Card title="Strategies overview">
          {loading && strategies.length === 0 && !error && (
            <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
              Loading strategies…
            </p>
          )}

          {!loading && strategies.length === 0 && !error && (
            <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
              You don’t have any strategies yet. Use the{" "}
              <span style={{ fontWeight: 600 }}>Create strategy</span> action
              above to get started.
            </p>
          )}

          {!loading && strategies.length > 0 && (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "0.4rem",
              }}
            >
              {strategies.map((s) => (
                <div
                  key={s.id}
                  style={{
                    display: "grid",
                    gridTemplateColumns:
                      "minmax(0, 2fr) minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1fr)",
                    gap: "0.3rem 1rem",
                    padding: "0.5rem 0.7rem",
                    borderRadius: 8,
                    border: "1px solid #111827",
                    background: "rgba(7,12,30,0.9)",
                    fontSize: "0.85rem",
                    cursor: "pointer",
                  }}
                  onClick={() => router.push(`/strategies/${s.id}`)}
                >
                  <div>
                    <div style={{ color: "#e5f4ff" }}>{s.name}</div>
                    <div style={{ color: "#9ca3af", fontSize: "0.8rem" }}>
                      {s.symbol_universe || "—"}
                    </div>
                  </div>
                  <div style={{ color: "#9ca3af" }}>
                    {s.style || "—"} · {s.timeframe || "—"}
                  </div>
                  <div>
                    <span
                      style={{
                        display: "inline-block",
                        padding: "0.1rem 0.5rem",
                        borderRadius: 999,
                        fontSize: "0.75rem",
                        color: s.is_active ? "#bbf7d0" : "#e5e7eb",
                        backgroundColor: s.is_active
                          ? "rgba(22,163,74,0.2)"
                          : "rgba(55,65,81,0.4)",
                      }}
                    >
                      {s.is_active ? "Active" : "Inactive"}
                    </span>
                  </div>
                  <div
                    style={{
                      fontSize: "0.78rem",
                      color: "#6b7280",
                      textAlign: "right",
                    }}
                  >
                    Updated:{" "}
                    {s.updated_at
                      ? new Date(s.updated_at).toLocaleDateString()
                      : "—"}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </AppShell>
  );
}