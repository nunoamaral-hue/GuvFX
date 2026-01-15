"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { apiFetch } from "@/lib/api";
import { AppShell } from "@/components/AppShell";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
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
  const [accessToken, setAccessToken] = useState("");
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

        <Card title="Strategies">
          {!accessToken && (
            <p style={{ fontStyle: "italic", fontSize: "0.9rem" }}>
              
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
              display: "flex",
              flexDirection: "column",
              gap: "0.75rem",
              marginTop: "0.5rem",
            }}
          >
            {strategies.map((strategy) => (
              <div
                key={strategy.id}
                style={{
                  border: "1px solid #222838",
                  borderRadius: 8,
                  padding: "0.75rem 1rem",
                  background: "rgba(7, 12, 30, 0.9)",
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
                  <Badge color={strategy.is_active ? "green" : "gray"}>
                    {strategy.is_active ? "Active" : "Inactive"}
                  </Badge>
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
                <div style={{ marginTop: "0.5rem" }}>
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
        </Card>
      </div>
    </AppShell>
  );
}
