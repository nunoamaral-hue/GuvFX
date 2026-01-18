"use client";

import { AppShell } from "@/components/AppShell";
import { Button } from "@/components/ui/Button";
import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/lib/api";

// ─────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────
type MarketCategory = "Trend" | "Breakout" | "Reversion" | "Structure" | "Patterns";

type Accent = "blue" | "green" | "purple" | "yellow";

type MarketplaceStrategy = {
  id: string;
  name: string;
  category: MarketCategory;
  accent: Accent;
  risk: "Low" | "Medium" | "High";
  summary: string;
  timeframes: string[];
  pairs: string[];
  backtest_window?: string;
  win_rate?: number;
  avg_r?: number;
  max_dd?: number;
  tags?: string[];
};

type TradingAccount = {
  id: number;
  name: string;
  broker_name?: string;
  account_number?: string;
};

// ─────────────────────────────────────────────────────────────────────
// Styling Helpers
// ─────────────────────────────────────────────────────────────────────
const accentPill = (accent: Accent) => {
  const map = {
    blue: { bg: "rgba(59,130,246,0.16)", border: "rgba(59,130,246,0.35)", text: "#93c5fd" },
    green: { bg: "rgba(34,197,94,0.14)", border: "rgba(34,197,94,0.35)", text: "#86efac" },
    purple: { bg: "rgba(168,85,247,0.14)", border: "rgba(168,85,247,0.35)", text: "#d8b4fe" },
    yellow: { bg: "rgba(250,204,21,0.14)", border: "rgba(250,204,21,0.40)", text: "#fde047" },
  } as const;
  return map[accent];
};

const glassCardStyle: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.10)",
  borderRadius: 14,
  background: "linear-gradient(180deg, rgba(10,16,35,0.72), rgba(6,10,25,0.85))",
  boxShadow: "0 10px 30px rgba(0,0,0,0.45)",
};

const pillStyle = (accent: Accent): React.CSSProperties => {
  const a = accentPill(accent);
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    padding: "0.18rem 0.55rem",
    borderRadius: 999,
    border: `1px solid ${a.border}`,
    background: a.bg,
    color: a.text,
    fontSize: "0.75rem",
    fontWeight: 600,
    whiteSpace: "nowrap",
  };
};

const badgeStyle = (type: "low" | "medium" | "high" | "default"): React.CSSProperties => {
  const styles = {
    low: {
      bg: "rgba(34,197,94,0.14)",
      border: "rgba(34,197,94,0.35)",
      text: "#86efac",
    },
    medium: {
      bg: "rgba(168,85,247,0.14)",
      border: "rgba(168,85,247,0.35)",
      text: "#d8b4fe",
    },
    high: {
      bg: "rgba(250,204,21,0.14)",
      border: "rgba(250,204,21,0.40)",
      text: "#fde047",
    },
    default: {
      bg: "rgba(100,116,139,0.14)",
      border: "rgba(100,116,139,0.35)",
      text: "#94a3b8",
    },
  };
  const s = styles[type];
  return {
    display: "inline-flex",
    alignItems: "center",
    padding: "0.15rem 0.5rem",
    borderRadius: 999,
    border: `1px solid ${s.border}`,
    background: s.bg,
    color: s.text,
    fontSize: "0.7rem",
    fontWeight: 600,
    whiteSpace: "nowrap",
  };
};

// ─────────────────────────────────────────────────────────────────────
// Seeded Marketplace Strategies
// ─────────────────────────────────────────────────────────────────────
const MARKETPLACE_SEED: MarketplaceStrategy[] = [
  {
    id: "mp-001",
    name: "London Session Box Breakout",
    category: "Breakout",
    accent: "purple",
    risk: "Medium",
    summary: "Trades Asian session range breakouts during London open with volatility confirmation.",
    timeframes: ["M15", "M30"],
    pairs: ["GBPUSD", "EURUSD", "GBPJPY"],
    backtest_window: "12m",
    win_rate: 58,
    avg_r: 0.42,
    max_dd: 9.2,
    tags: ["Verified"],
  },
  {
    id: "mp-002",
    name: "Trend EMA Crossover (HTF filter)",
    category: "Trend",
    accent: "blue",
    risk: "Low",
    summary: "20/50 EMA cross on M15 with H4 trend alignment. Designed for steady equity curve.",
    timeframes: ["M15", "H1"],
    pairs: ["EURUSD", "USDJPY", "AUDUSD"],
    backtest_window: "18m",
    win_rate: 54,
    avg_r: 0.35,
    max_dd: 6.8,
    tags: ["Verified"],
  },
  {
    id: "mp-003",
    name: "Bollinger Mean Reversion",
    category: "Reversion",
    accent: "green",
    risk: "Medium",
    summary: "Enters on 2σ touches with RSI divergence, targeting middle band. Works best in ranging markets.",
    timeframes: ["M5", "M15"],
    pairs: ["EURUSD", "GBPUSD", "USDCHF"],
    backtest_window: "12m",
    win_rate: 62,
    avg_r: 0.28,
    max_dd: 11.5,
    tags: ["Verified"],
  },
  {
    id: "mp-004",
    name: "Head & Shoulders Reversal (Beta)",
    category: "Patterns",
    accent: "yellow",
    risk: "High",
    summary: "Automated chart pattern recognition for H&S reversals with volume confirmation. Currently in beta testing.",
    timeframes: ["H1", "H4"],
    pairs: ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
    backtest_window: "6m",
    win_rate: 48,
    avg_r: 0.65,
    max_dd: 15.3,
    tags: ["Beta"],
  },
];

// ─────────────────────────────────────────────────────────────────────
// Main Component
// ─────────────────────────────────────────────────────────────────────
export default function StrategyMarketplacePage() {
  const [search, setSearch] = useState("");
  const [activeFilter, setActiveFilter] = useState<MarketCategory | "All">("All");
  const [accounts, setAccounts] = useState<TradingAccount[]>([]);
  const [loadingAccounts, setLoadingAccounts] = useState(true);
  const [assigning, setAssigning] = useState<Record<string, boolean>>({});
  const [selectedAccount, setSelectedAccount] = useState<Record<string, number | "">>({});
  const [alert, setAlert] = useState<string | null>(null);
  const [alertType, setAlertType] = useState<"info" | "error" | "success">("info");

  // ─────────────────────────────────────────────────────────────────────
  // Fetch Accounts
  // ─────────────────────────────────────────────────────────────────────
  useEffect(() => {
    const fetchAccounts = async () => {
      try {
        // Try primary endpoint
        const data = await apiFetch<TradingAccount[]>("/api/trading/accounts/");
        setAccounts(data);
      } catch {
        // Fallback endpoint
        try {
          const data = await apiFetch<TradingAccount[]>("/api/trading/trading-accounts/");
          setAccounts(data);
        } catch {
          // Quietly fail - marketplace should still render
          setAccounts([]);
        }
      } finally {
        setLoadingAccounts(false);
      }
    };
    fetchAccounts();
  }, []);

  // ─────────────────────────────────────────────────────────────────────
  // Filtered Strategies
  // ─────────────────────────────────────────────────────────────────────
  const filteredStrategies = useMemo(() => {
    let result = MARKETPLACE_SEED;

    // Category filter
    if (activeFilter !== "All") {
      result = result.filter((s) => s.category === activeFilter);
    }

    // Search filter
    if (search.trim()) {
      const q = search.toLowerCase();
      result = result.filter(
        (s) =>
          s.name.toLowerCase().includes(q) ||
          s.summary.toLowerCase().includes(q) ||
          s.pairs.some((p) => p.toLowerCase().includes(q))
      );
    }

    return result;
  }, [search, activeFilter]);

  // ─────────────────────────────────────────────────────────────────────
  // Assign Handler
  // ─────────────────────────────────────────────────────────────────────
  const handleAssign = async (strategyId: string) => {
    const accountId = selectedAccount[strategyId];
    if (!accountId) {
      setAlert("Please select an account first");
      setAlertType("error");
      return;
    }

    setAssigning({ ...assigning, [strategyId]: true });

    try {
      await apiFetch("/api/strategies/strategies/marketplace/assign/", {
        method: "POST",
        body: JSON.stringify({
          marketplace_strategy_id: strategyId,
          account_id: accountId,
        }),
      });
      setAlert(null); // Clear any previous errors
      setAlert("Assigned successfully.");
      setAlertType("success");
    } catch (err) {
      const e = err as { status?: number; message?: string };
      const msg = (e?.message || "").trim();

      // If backend returned an HTML 404 page (common when hitting wrong route),
      // don't dump HTML into the UI.
      const looksLikeHtml =
        msg.toLowerCase().includes("<!doctype") ||
        msg.toLowerCase().includes("<html") ||
        msg.toLowerCase().includes("<body");

      if (e?.status === 404 || msg.includes("404")) {
        setAlert("Marketplace assign endpoint not found. This usually means the frontend is calling the wrong URL or the server is not yet deployed with the endpoint.");
        setAlertType("error");
        return;
      }

      if (looksLikeHtml) {
        setAlert("Assignment failed (server returned an unexpected HTML response). Please refresh and try again.");
        setAlertType("error");
        return;
      }

      setAlert(msg || "Assignment failed");
      setAlertType("error");
    } finally {
      setAssigning({ ...assigning, [strategyId]: false });
    }
  };

  const handlePreview = () => {
    setAlert("Preview coming soon");
    setAlertType("info");
  };

  // ─────────────────────────────────────────────────────────────────────
  // Risk Badge Type
  // ─────────────────────────────────────────────────────────────────────
  const riskBadgeType = (risk: string): "low" | "medium" | "high" => {
    if (risk === "Low") return "low";
    if (risk === "High") return "high";
    return "medium";
  };

  // ─────────────────────────────────────────────────────────────────────
  // Render
  // ─────────────────────────────────────────────────────────────────────
  return (
    <AppShell>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        {/* Header */}
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Strategy Marketplace</h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1.5rem" }}>
          Browse and deploy pre-built strategies to your trading accounts.
        </p>

        {/* Alert */}
        {alert && (
          <div
            style={{
              marginBottom: "1rem",
              padding: "0.75rem 1rem",
              borderRadius: 8,
              border: `1px solid ${
                alertType === "error"
                  ? "rgba(239,68,68,0.4)"
                  : alertType === "success"
                  ? "rgba(34,197,94,0.4)"
                  : "rgba(59,130,246,0.4)"
              }`,
              background: `${
                alertType === "error"
                  ? "rgba(239,68,68,0.1)"
                  : alertType === "success"
                  ? "rgba(34,197,94,0.1)"
                  : "rgba(59,130,246,0.1)"
              }`,
              color: `${
                alertType === "error" ? "#fca5a5" : alertType === "success" ? "#86efac" : "#93c5fd"
              }`,
              fontSize: "0.875rem",
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            <span>{alert}</span>
            <button
              onClick={() => setAlert(null)}
              style={{
                background: "none",
                border: "none",
                color: "inherit",
                cursor: "pointer",
                fontSize: "1.25rem",
                lineHeight: 1,
                padding: "0 0.25rem",
              }}
            >
              ×
            </button>
          </div>
        )}

        {/* Search + Filters */}
        <div style={{ marginBottom: "1.5rem" }}>
          <input
            type="text"
            placeholder="Search strategies, pairs..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{
              width: "100%",
              padding: "0.65rem 1rem",
              borderRadius: 10,
              border: "1px solid rgba(255,255,255,0.12)",
              background: "rgba(10,16,35,0.6)",
              color: "#e2e8f0",
              fontSize: "0.9rem",
              marginBottom: "1rem",
            }}
          />

          <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
            {(["All", "Trend", "Breakout", "Reversion", "Structure", "Patterns"] as const).map((cat) => {
              const isActive = activeFilter === cat;
              return (
                <button
                  key={cat}
                  onClick={() => setActiveFilter(cat)}
                  style={{
                    padding: "0.4rem 0.9rem",
                    borderRadius: 999,
                    border: isActive ? "1px solid rgba(59,130,246,0.5)" : "1px solid rgba(255,255,255,0.15)",
                    background: isActive ? "rgba(59,130,246,0.2)" : "rgba(10,16,35,0.4)",
                    color: isActive ? "#93c5fd" : "#b7c5dd",
                    fontSize: "0.8rem",
                    fontWeight: 600,
                    cursor: "pointer",
                    transition: "all 0.2s",
                  }}
                >
                  {cat}
                </button>
              );
            })}
          </div>
        </div>

        {/* Strategy Cards Grid */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
            gap: "1.25rem",
          }}
        >
          {filteredStrategies.map((strategy) => (
            <div key={strategy.id} style={{ ...glassCardStyle, padding: "1.25rem" }}>
              {/* Header: Category + Tags */}
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.75rem" }}>
                <span style={pillStyle(strategy.accent)}>{strategy.category}</span>
                <div style={{ display: "flex", gap: "0.4rem" }}>
                  <span style={badgeStyle(riskBadgeType(strategy.risk))}>{strategy.risk}</span>
                  {strategy.tags?.map((tag) => (
                    <span key={tag} style={badgeStyle("default")}>
                      {tag}
                    </span>
                  ))}
                </div>
              </div>

              {/* Name */}
              <h3 style={{ fontSize: "1.1rem", fontWeight: 600, marginBottom: "0.5rem", color: "#e2e8f0" }}>
                {strategy.name}
              </h3>

              {/* Summary */}
              <p style={{ fontSize: "0.85rem", color: "#94a3b8", marginBottom: "1rem", lineHeight: 1.5 }}>
                {strategy.summary}
              </p>

              {/* Pairs + Timeframes */}
              <div style={{ marginBottom: "1rem" }}>
                <div style={{ fontSize: "0.75rem", color: "#64748b", marginBottom: "0.3rem" }}>Pairs</div>
                <div style={{ fontSize: "0.8rem", color: "#cbd5e1" }}>{strategy.pairs.join(", ")}</div>
                <div style={{ fontSize: "0.75rem", color: "#64748b", marginTop: "0.5rem", marginBottom: "0.3rem" }}>
                  Timeframes
                </div>
                <div style={{ fontSize: "0.8rem", color: "#cbd5e1" }}>{strategy.timeframes.join(", ")}</div>
              </div>

              {/* Stats Strip */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(4, 1fr)",
                  gap: "0.75rem",
                  padding: "0.75rem",
                  borderRadius: 8,
                  background: "rgba(0,0,0,0.25)",
                  marginBottom: "1rem",
                }}
              >
                <div>
                  <div style={{ fontSize: "0.65rem", color: "#64748b", marginBottom: "0.2rem" }}>Win Rate</div>
                  <div style={{ fontSize: "0.9rem", fontWeight: 600, color: "#e2e8f0" }}>
                    {strategy.win_rate !== undefined ? `${strategy.win_rate}%` : "—"}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: "0.65rem", color: "#64748b", marginBottom: "0.2rem" }}>Avg R</div>
                  <div style={{ fontSize: "0.9rem", fontWeight: 600, color: "#e2e8f0" }}>
                    {strategy.avg_r !== undefined ? strategy.avg_r.toFixed(2) : "—"}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: "0.65rem", color: "#64748b", marginBottom: "0.2rem" }}>Max DD</div>
                  <div style={{ fontSize: "0.9rem", fontWeight: 600, color: "#e2e8f0" }}>
                    {strategy.max_dd !== undefined ? `${strategy.max_dd}%` : "—"}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: "0.65rem", color: "#64748b", marginBottom: "0.2rem" }}>Window</div>
                  <div style={{ fontSize: "0.9rem", fontWeight: 600, color: "#e2e8f0" }}>
                    {strategy.backtest_window || "—"}
                  </div>
                </div>
              </div>

              {/* CTA Row */}
              <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                <select
                  value={selectedAccount[strategy.id] || ""}
                  onChange={(e) =>
                    setSelectedAccount({
                      ...selectedAccount,
                      [strategy.id]: e.target.value ? Number(e.target.value) : "",
                    })
                  }
                  disabled={loadingAccounts}
                  style={{
                    flex: 1,
                    padding: "0.5rem",
                    borderRadius: 8,
                    border: "1px solid rgba(255,255,255,0.15)",
                    background: "rgba(10,16,35,0.6)",
                    color: "#e2e8f0",
                    fontSize: "0.85rem",
                  }}
                >
                  <option value="">Select account</option>
                  {accounts.map((acc) => (
                    <option key={acc.id} value={acc.id}>
                      {acc.name}
                    </option>
                  ))}
                </select>
                <Button
                  variant="primary"
                  onClick={() => handleAssign(strategy.id)}
                  disabled={!selectedAccount[strategy.id] || assigning[strategy.id]}
                >
                  {assigning[strategy.id] ? "Assigning..." : "Assign"}
                </Button>
                <Button variant="secondary" onClick={handlePreview}>
                  Preview
                </Button>
              </div>
            </div>
          ))}
        </div>

        {/* Empty State */}
        {filteredStrategies.length === 0 && (
          <div style={{ textAlign: "center", padding: "3rem 1rem", color: "#64748b" }}>
            <p style={{ fontSize: "1rem" }}>No strategies match your filters.</p>
            <p style={{ fontSize: "0.85rem", marginTop: "0.5rem" }}>Try adjusting your search or category filter.</p>
          </div>
        )}
      </div>
    </AppShell>
  );
}
