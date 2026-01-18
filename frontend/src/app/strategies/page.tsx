"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { apiFetch } from "@/lib/api";
import { AppShell } from "@/components/AppShell";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";

type Strategy = {
  id: number;
  name: string;
  description: string;
  style: string | null;
  symbol_universe: string;
  timeframe: string;
  risk_per_trade_pct: string | null;
  max_drawdown_pct: string | null;
  magic_number: number | null;
  is_active: boolean;
  entry_logic: string;
  exit_logic: string;
  notes: string;
  ma_fast_period: number | null;
  ma_slow_period: number | null;
  ma_type: string | null;
  auto_optimize_by_ai: boolean;
  created_at: string;
};

export default function StrategiesListPage() {
  const glassCardStyle: React.CSSProperties = {
    border: "1px solid rgba(255,255,255,0.10)",
    borderRadius: 14,
    background:
      "linear-gradient(180deg, rgba(10,16,35,0.72), rgba(6,10,25,0.85))",
    boxShadow: "0 10px 30px rgba(0,0,0,0.45)",
  };

  const rowStyle: React.CSSProperties = {
    border: "1px solid rgba(255,255,255,0.10)",
    borderRadius: 12,
    padding: "0.9rem 1rem",
    background: "rgba(7, 12, 30, 0.9)",
  };
  const [accessToken, setAccessToken] = useState("");
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionBusyId, setActionBusyId] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    if (typeof window !== "undefined") {
      const stored = window.localStorage.getItem("guvfx_access_token");
      if (stored) {
        setAccessToken(stored);
      }
    }
  }, []);

  useEffect(() => {
    

    const fetchStrategies = async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await apiFetch<Strategy[]>(
          "/api/strategies/strategies/",
          {});
        setStrategies(data);
      } catch (err: unknown) {
        console.error(err);
        const message =
          err instanceof Error ? err.message : "Failed to load strategies.";
        setError(message);
      } finally {
        setLoading(false);
      }
    };

    fetchStrategies();
  }, [accessToken]);

  return (
    <AppShell>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>
          My Strategies
        </h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
          View and analyze your strategies, then dive into AI-assisted insights.
        </p>

        {error && <Alert type="error">{error}</Alert>}
        {actionError && <Alert type="error">{actionError}</Alert>}

        <div style={{ ...glassCardStyle, padding: "1rem 1rem 1.1rem", marginBottom: "1rem" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <div style={{ fontWeight: 700, color: "#e5f4ff", fontSize: "1.05rem" }}>Strategies</div>
            <Link href="/strategies/create" style={{ textDecoration: "none" }}>
              <Button variant="primary">Create strategy</Button>
            </Link>
          </div>
          <div style={{ marginTop: 6, fontSize: "0.85rem", color: "#9ca3af" }}>
            Manage your strategies and toggle them on/off.
          </div>
        </div>

        {!accessToken && (
          <p style={{ fontStyle: "italic", fontSize: "0.9rem", color: "#9ca3af" }}>
            Please log in to view your strategies.
          </p>
        )}

        {loading && <p>Loading strategies...</p>}

        {!loading && strategies.length === 0 && accessToken && !error && (
          <p style={{ fontSize: "0.9rem" }}>
            No strategies found yet. Create one from the Builder then come back
            here.
          </p>
        )}

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))",
            gap: "1rem",
            marginTop: "0.75rem",
          }}
        >
          {strategies.map((strategy) => (
            <div
              key={strategy.id}
              style={{
                ...rowStyle,
                position: "relative",
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: "0.25rem",
                }}
              >
                <h3
                  style={{
                    fontSize: "1.05rem",
                    margin: 0,
                    color: "#f1f5ff",
                  }}
                >
                  {strategy.name}{" "}
                  <span
                    style={{
                      fontSize: "0.8rem",
                      fontWeight: 400,
                      color: "#8897b2",
                      marginLeft: 8,
                    }}
                  >
                    #{strategy.id}
                  </span>
                </h3>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <Badge color={strategy.is_active ? "green" : "gray"}>
                    {strategy.is_active ? "Active" : "Inactive"}
                  </Badge>

                  <select
                    aria-label="Strategy actions"
                    defaultValue=""
                    disabled={!accessToken || actionBusyId === strategy.id}
                    onChange={async (e) => {
                      const action = e.target.value;
                      e.target.value = "";
                      if (!action) return;

                      setActionError(null);
                      setActionBusyId(strategy.id);

                      try {
                        if (action === "toggle") {
                          const updated = await apiFetch<Strategy>(
                            `/api/strategies/strategies/${strategy.id}/`,
                            {
                              method: "PATCH",
                              body: JSON.stringify({ is_active: !strategy.is_active }),
                            }
                          );
                          setStrategies((prev) =>
                            prev.map((s) => (s.id === strategy.id ? updated : s))
                          );
                          return;
                        }

                        if (action === "delete") {
                          const ok = window.confirm(
                            `Delete strategy "${strategy.name}"? This cannot be undone.`
                          );
                          if (!ok) return;

                          await apiFetch(`/api/strategies/strategies/${strategy.id}/`, {
                            method: "DELETE",
                          });
                          setStrategies((prev) => prev.filter((s) => s.id !== strategy.id));
                          return;
                        }
                      } catch (err: unknown) {
                        console.error(err);
                        const message =
                          err instanceof Error ? err.message : "Action failed.";
                        setActionError(message);
                      } finally {
                        setActionBusyId(null);
                      }
                    }}
                    style={{
                      padding: "0.35rem 0.5rem",
                      borderRadius: 10,
                      border: "1px solid rgba(255,255,255,0.12)",
                      background: "rgba(10,16,35,0.55)",
                      color: "#cbd5f5",
                      fontSize: "0.8rem",
                      outline: "none",
                    }}
                  >
                    <option value="">Actions</option>
                    <option value="toggle">{strategy.is_active ? "Deactivate" : "Activate"}</option>
                    <option value="delete">Delete…</option>
                  </select>
                </div>
              </div>
              <p
                style={{
                  fontSize: "0.9rem",
                  margin: "0.2rem 0 0.3rem 0",
                  color: "#d0e1ff",
                }}
              >
                {strategy.description || (
                  <span style={{ color: "#7c8ca4" }}>No description</span>
                )}
              </p>
              <p
                style={{
                  fontSize: "0.8rem",
                  color: "#8fa0b7",
                  margin: 0,
                }}
              >
                <strong>Symbols:</strong> {strategy.symbol_universe || "—"}{" "}
                &nbsp;|&nbsp;
                <strong>Timeframe:</strong> {strategy.timeframe || "—"}
              </p>
              <p
                style={{
                  fontSize: "0.75rem",
                  color: "#6d7a92",
                  marginTop: "0.2rem",
                }}
              >
                Created: {new Date(strategy.created_at).toLocaleString()}
              </p>
              <div style={{ marginTop: "0.75rem" }}>
                <Link
                  href={`/strategies/${strategy.id}`}
                  style={{
                    fontSize: "0.85rem",
                    color: "#4ab3ff",
                    textDecoration: "none",
                  }}
                >
                  View details & AI suggestions →
                </Link>
              </div>
            </div>
          ))}
        </div>
      </div>
    </AppShell>
  );
}
