

"use client";

import { useEffect, useState } from "react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";

type StrategyMetric = {
  strategy_name: string;
  trades: number;
  net_pnl: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
};

export default function StrategyMetricsPage() {
  const [accountId, setAccountId] = useState<string>("13");
  const [rows, setRows] = useState<StrategyMetric[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>("");

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`https://api.guvfx.com/api/analytics/strategy-metrics/?account=${accountId}`, {
        credentials: "include",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setRows(Array.isArray(data?.strategies) ? data.strategies : []);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Failed to load strategy metrics";
      setError(msg);
      setRows([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId]);

  return (
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Strategy Metrics</h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
          Aggregated performance by strategy (derived from trade history).
        </p>

        <Card title="Controls">
          <div style={{ display: "flex", gap: "0.75rem", alignItems: "center", padding: "0.75rem" }}>
            <label style={{ fontWeight: 600 }}>Account ID</label>
            <input
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              style={{ padding: "0.5rem", borderRadius: 8, border: "1px solid rgba(148,163,184,0.6)", background: "rgba(3,7,18,0.9)", color: "#e5f4ff" }}
            />
            <Button type="button" onClick={load} disabled={loading}>
              {loading ? "Loading…" : "Refresh"}
            </Button>
            {error && <span style={{ color: "#f87171" }}>{error}</span>}
          </div>
        </Card>

        <Card title="By Strategy">
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  {["Strategy", "Trades", "Net PnL", "Win Rate"].map((h) => (
                    <th
                      key={h}
                      style={{
                        textAlign: "left",
                        padding: "0.5rem",
                        borderBottom: "1px solid rgba(255,255,255,0.1)",
                      }}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.strategy_name}>
                    <td style={{ padding: "0.5rem" }}>{r.strategy_name}</td>
                    <td style={{ padding: "0.5rem" }}>{r.trades}</td>
                    <td style={{ padding: "0.5rem" }}>{r.net_pnl}</td>
                    <td style={{ padding: "0.5rem" }}>{r.win_rate_pct}%</td>
                  </tr>
                ))}
                {!loading && rows.length === 0 && (
                  <tr>
                    <td colSpan={4} style={{ padding: "1rem", opacity: 0.75 }}>
                      No metrics yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
  );
}