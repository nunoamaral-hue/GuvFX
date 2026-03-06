"use client";

import { useLang } from "@/components/AppShell";
import { Button } from "@/components/ui/Button";
import { t } from "@/lib/i18n";
import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useRouter } from "next/navigation";

// ─────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────
type MarketCategory = "Trend" | "Breakout" | "Reversion" | "Structure" | "Patterns" | "System-grade";

type Accent = "blue" | "green" | "purple" | "yellow" | "cyan";

type MarketplaceStrategy = {
  id: string;
  name: string;
  category: MarketCategory;
  accent: Accent;
  style: string;
  execution: string;
  summary: string;
  timeframes: string[];
  pairs: string[];
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
    cyan: { bg: "rgba(34,211,238,0.14)", border: "rgba(34,211,238,0.40)", text: "#67e8f9" },
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

const badgeStyle = (): React.CSSProperties => ({
  display: "inline-flex",
  alignItems: "center",
  padding: "0.15rem 0.5rem",
  borderRadius: 999,
  border: "1px solid rgba(100,116,139,0.35)",
  background: "rgba(100,116,139,0.14)",
  color: "#94a3b8",
  fontSize: "0.7rem",
  fontWeight: 600,
  whiteSpace: "nowrap",
});

// ─────────────────────────────────────────────────────────────────────
// Seeded Marketplace Strategies
// ─────────────────────────────────────────────────────────────────────
const MARKETPLACE_SEED: MarketplaceStrategy[] = [
  {
    id: "mp-001",
    name: "London Session Box Breakout",
    category: "Breakout",
    accent: "purple",
    style: "Volatility Breakout",
    execution: "Manual review required",
    summary: "Example ruleset for Asian session range breakouts during London open. Review and test before use.",
    timeframes: ["M15", "M30"],
    pairs: ["GBPUSD", "EURUSD", "GBPJPY"],
    tags: ["Template"],
  },
  {
    id: "mp-002",
    name: "Trend EMA Crossover (HTF filter)",
    category: "Trend",
    accent: "blue",
    style: "Trend Following",
    execution: "Manual review required",
    summary: "20/50 EMA cross on M15 with H4 trend alignment. Designed to be configured and tested by the user.",
    timeframes: ["M15", "H1"],
    pairs: ["EURUSD", "USDJPY", "AUDUSD"],
    tags: ["Template"],
  },
  {
    id: "mp-003",
    name: "Bollinger Mean Reversion",
    category: "Reversion",
    accent: "green",
    style: "Mean Reversion",
    execution: "Manual review required",
    summary: "Enters on 2σ touches with RSI divergence. Example template — review and test before use.",
    timeframes: ["M5", "M15"],
    pairs: ["EURUSD", "GBPUSD", "USDCHF"],
    tags: ["Example"],
  },
  {
    id: "mp-004",
    name: "Head & Shoulders Reversal",
    category: "Patterns",
    accent: "yellow",
    style: "Chart Patterns",
    execution: "User-controlled execution",
    summary: "Automated chart pattern recognition for H&S reversals with volume confirmation. Currently in beta — review and test before use.",
    timeframes: ["H1", "H4"],
    pairs: ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
    tags: ["Beta"],
  },
  {
    id: "mp-005",
    name: "Trendline Break Pocket",
    category: "System-grade",
    accent: "cyan",
    style: "HTF Zone + Structure",
    execution: "Automation-ready",
    summary: "HTF zone + trendline break + structure shift. Fixed 2R model. Manual zones editable. Designed by Ali.",
    timeframes: ["H4"],
    pairs: ["EURUSD", "GBPUSD"],
    tags: ["Automation-ready", "Ali"],
  },
  {
    id: "mp-006",
    name: "Adaptive Liquidity Trap Scalper",
    category: "System-grade",
    accent: "purple",
    style: "Liquidity / Mean reversion",
    execution: "Automation-ready",
    summary: "Range-regime liquidity sweep + displacement + confirmation. M5 execution with M15 regime filter.",
    timeframes: ["M5", "M15"],
    pairs: ["EURUSD", "GBPUSD"],
    tags: ["Automation-ready", "ALTS"],
  },
  {
    id: "mp-007",
    name: "Structural Continuation Engine",
    category: "System-grade",
    accent: "purple",
    style: "Trend continuation",
    execution: "Automation-ready",
    summary: "H4 bias + H1 BOS + pullback + rejection continuation. H1 execution with H4 context.",
    timeframes: ["H1", "H4"],
    pairs: ["EURUSD", "GBPUSD"],
    tags: ["Automation-ready", "SCE"],
  },
  {
    id: "mp-008",
    name: "Trend Continuation Engine v1",
    category: "System-grade",
    accent: "cyan",
    style: "Trend continuation",
    execution: "Automation-ready",
    summary: "EMA50/200 trend filter + ATR pullback zone + confirmation candle. H4 execution, fixed 1.5R.",
    timeframes: ["H4"],
    pairs: ["EURUSD", "GBPUSD"],
    tags: ["Automation-ready", "TC1"],
  },
  {
    id: "mp-009",
    name: "TBP V3 Hybrid Sleeve v1",
    category: "System-grade",
    accent: "cyan",
    style: "HTF Zone + Macro Overlay",
    execution: "Automation-ready",
    summary: "CORE (TBP trendline break pocket) + SLEEVE (TC1 trend continuation on risk-on days). H4 execution.",
    timeframes: ["H4"],
    pairs: ["EURUSD", "GBPUSD"],
    tags: ["Automation-ready", "Hybrid"],
  },
];

// ─────────────────────────────────────────────────────────────────────
// Main Component
// ─────────────────────────────────────────────────────────────────────
export default function StrategyMarketplacePage() {
  const router = useRouter();
  const lang = useLang();

  const LS_DEFAULT_ACCOUNT_KEY = "guvfx_marketplace_default_account_id";

  const [search, setSearch] = useState("");
  const [activeFilter, setActiveFilter] = useState<MarketCategory | "All">("All");
  const [accounts, setAccounts] = useState<TradingAccount[]>([]);
  const [loadingAccounts, setLoadingAccounts] = useState(true);
  const [assigning, setAssigning] = useState<Record<string, boolean>>({});
  const [selectedAccount, setSelectedAccount] = useState<Record<string, number | "">>({});
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const [defaultAccountId, setDefaultAccountId] = useState<number | null>(null);
  const [alert, setAlert] = useState<string | null>(null);
  const [alertType, setAlertType] = useState<"info" | "error" | "success">("info");

  const [authChecked, setAuthChecked] = useState(false);
  const [isAuthed, setIsAuthed] = useState(false);

  // ─────────────────────────────────────────────────────────────────────
  // Auth Check (cookie auth)
  // ─────────────────────────────────────────────────────────────────────
  useEffect(() => {
    const checkAuth = async () => {
      try {
        await apiFetch("/api/auth/me/", { method: "GET" });
        setIsAuthed(true);
      } catch {
        setIsAuthed(false);
      } finally {
        setAuthChecked(true);
      }
    };

    checkAuth();
  }, []);

  // ─────────────────────────────────────────────────────────────────────
  // Fetch Accounts
  // ─────────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!authChecked || !isAuthed) {
      setLoadingAccounts(false);
      return;
    }

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
  }, [authChecked, isAuthed]);

  // Load saved default account for marketplace dropdowns
  useEffect(() => {
    if (typeof window === "undefined") return;

    const raw = window.localStorage.getItem(LS_DEFAULT_ACCOUNT_KEY);
    if (!raw) return;

    const n = Number(raw);
    if (Number.isFinite(n) && n > 0) {
      setDefaultAccountId(n);
      // Preselect this account for all seed strategies
      const map: Record<string, number> = {};
      for (const s of MARKETPLACE_SEED) map[s.id] = n;
      setSelectedAccount(map);
    }
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
      setAlert(t(lang, "marketplace.alertSelectAccount"));
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
      setAlert(t(lang, "marketplace.alertAssigned"));
      setAlertType("success");

      // Keep the selected account as the default for next time
      if (typeof window !== "undefined") {
        const v = selectedAccount[strategyId];
        if (typeof v === "number" && v > 0) {
          window.localStorage.setItem(LS_DEFAULT_ACCOUNT_KEY, String(v));
          setDefaultAccountId(v);
        }
      }
    } catch (err) {
      const e = err as { status?: number; message?: string };
      const msg = (e?.message || "").trim();

      // If backend returned an HTML 404 page (common when hitting wrong route),
      // don't dump HTML into the UI.
      const looksLikeHtml =
        msg.toLowerCase().includes("<!doctype") ||
        msg.toLowerCase().includes("<html") ||
        msg.toLowerCase().includes("<body");

      if (e?.status === 401 || msg.toLowerCase().includes("unauthorized")) {
        setAlert(t(lang, "marketplace.alertSessionExpired"));
        setAlertType("error");
        setIsAuthed(false);
        return;
      }

      if (e?.status === 404 || msg.includes("404")) {
        setAlert(t(lang, "marketplace.alertEndpointNotFound"));
        setAlertType("error");
        return;
      }

      if (looksLikeHtml) {
        setAlert(t(lang, "marketplace.alertUnexpectedResponse"));
        setAlertType("error");
        return;
      }

      setAlert(msg || t(lang, "marketplace.alertAssignFailed"));
      setAlertType("error");
    } finally {
      setAssigning({ ...assigning, [strategyId]: false });
    }
  };

  const handlePreview = () => {
    setAlert(t(lang, "marketplace.alertPreviewSoon"));
    setAlertType("info");
  };

  // ─────────────────────────────────────────────────────────────────────
  // Render
  // ─────────────────────────────────────────────────────────────────────
  return (
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        {/* Header */}
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>{t(lang, "marketplace.title")}</h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "0.5rem" }}>
          {t(lang, "marketplace.subtitle")}
        </p>
        <p style={{ fontSize: "0.75rem", color: "#64748b", marginBottom: "0.35rem" }}>
          {t(lang, "legal.microDisclaimer")}
        </p>
        <p style={{ fontSize: "0.72rem", color: "#64748b", marginBottom: "1.5rem", lineHeight: 1.5 }}>
          {t(lang, "marketplace.disclaimerLine1")}
        </p>

        {authChecked && !isAuthed && (
          <div
            style={{
              marginBottom: "1rem",
              padding: "0.75rem 1rem",
              borderRadius: 8,
              border: "1px solid rgba(239,68,68,0.35)",
              background: "rgba(239,68,68,0.08)",
              color: "#fca5a5",
              fontSize: "0.9rem",
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              gap: "0.75rem",
              flexWrap: "wrap",
            }}
          >
            <div>
              {t(lang, "marketplace.unauthMessage")}
            </div>
            <button
              type="button"
              onClick={() => router.push("/login?reason=unauthenticated")}
              style={{
                background: "rgba(59,130,246,0.18)",
                border: "1px solid rgba(59,130,246,0.40)",
                color: "#93c5fd",
                padding: "0.35rem 0.75rem",
                borderRadius: 999,
                fontSize: "0.85rem",
                fontWeight: 700,
                cursor: "pointer",
              }}
            >
              {t(lang, "marketplace.goToLogin")}
            </button>
          </div>
        )}

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
            <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", flexWrap: "wrap" }}>
              <span>{alert}</span>

              {alertType === "success" && (
                <button
                  type="button"
                  onClick={() => router.push("/strategies")}
                  style={{
                    background: "rgba(59,130,246,0.18)",
                    border: "1px solid rgba(59,130,246,0.40)",
                    color: "#93c5fd",
                    padding: "0.25rem 0.6rem",
                    borderRadius: 999,
                    fontSize: "0.78rem",
                    fontWeight: 700,
                    cursor: "pointer",
                  }}
                >
                  {t(lang, "marketplace.viewMyStrategies")}
                </button>
              )}
            </div>
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
            placeholder={t(lang, "marketplace.searchPlaceholder")}
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
            {(["All", "Trend", "Breakout", "Reversion", "Structure", "Patterns", "System-grade"] as const).map((cat) => {
              const isActive = activeFilter === cat;
              const filterKey = `marketplace.filter${cat}` as const;
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
                  {t(lang, filterKey)}
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
                  {strategy.tags?.map((tag) => (
                    <span key={tag} style={badgeStyle()}>
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
                <div style={{ fontSize: "0.75rem", color: "#64748b", marginBottom: "0.3rem" }}>{t(lang, "marketplace.pairsLabel")}</div>
                <div style={{ fontSize: "0.8rem", color: "#cbd5e1" }}>{strategy.pairs.join(", ")}</div>
                <div style={{ fontSize: "0.75rem", color: "#64748b", marginTop: "0.5rem", marginBottom: "0.3rem" }}>
                  {t(lang, "marketplace.timeframesLabel")}
                </div>
                <div style={{ fontSize: "0.8rem", color: "#cbd5e1" }}>{strategy.timeframes.join(", ")}</div>
              </div>

              {/* Template Info */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(3, 1fr)",
                  gap: "0.75rem",
                  padding: "0.75rem",
                  borderRadius: 8,
                  background: "rgba(0,0,0,0.25)",
                  marginBottom: "1rem",
                }}
              >
                <div>
                  <div style={{ fontSize: "0.65rem", color: "#64748b", marginBottom: "0.2rem" }}>
                    {t(lang, "marketplace.styleLabel")}
                  </div>
                  <div style={{ fontSize: "0.8rem", fontWeight: 600, color: "#e2e8f0" }}>
                    {strategy.style}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: "0.65rem", color: "#64748b", marginBottom: "0.2rem" }}>
                    {t(lang, "marketplace.timeframesLabel")}
                  </div>
                  <div style={{ fontSize: "0.8rem", fontWeight: 600, color: "#e2e8f0" }}>
                    {strategy.timeframes.join(", ")}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: "0.65rem", color: "#64748b", marginBottom: "0.2rem" }}>
                    {t(lang, "marketplace.executionLabel")}
                  </div>
                  <div style={{ fontSize: "0.8rem", fontWeight: 600, color: "#e2e8f0" }}>
                    {strategy.execution}
                  </div>
                </div>
              </div>

              {/* CTA Row */}
              <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                <select
                  value={selectedAccount[strategy.id] || ""}
                  onChange={(e) => {
                    const nextVal = e.target.value ? Number(e.target.value) : "";
                    setSelectedAccount({
                      ...selectedAccount,
                      [strategy.id]: nextVal,
                    });

                    if (typeof window !== "undefined") {
                      if (nextVal === "") {
                        window.localStorage.removeItem(LS_DEFAULT_ACCOUNT_KEY);
                        setDefaultAccountId(null);
                      } else {
                        window.localStorage.setItem(LS_DEFAULT_ACCOUNT_KEY, String(nextVal));
                        setDefaultAccountId(nextVal);
                      }
                    }
                  }}
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
                  <option value="">{t(lang, "marketplace.selectAccount")}</option>
                  {accounts.map((acc) => (
                    <option key={acc.id} value={acc.id}>
                      {acc.name}
                    </option>
                  ))}
                </select>
                <Button
                  variant="primary"
                  onClick={() => handleAssign(strategy.id)}
                  disabled={!isAuthed || !selectedAccount[strategy.id] || assigning[strategy.id]}
                >
                  {assigning[strategy.id] ? t(lang, "marketplace.assigning") : t(lang, "marketplace.assign")}
                </Button>
                <Button variant="secondary" onClick={handlePreview}>
                  {t(lang, "marketplace.preview")}
                </Button>
              </div>
            </div>
          ))}
        </div>

        {/* Empty State */}
        {filteredStrategies.length === 0 && (
          <div style={{ textAlign: "center", padding: "3rem 1rem", color: "#64748b" }}>
            <p style={{ fontSize: "1rem" }}>{t(lang, "marketplace.emptyTitle")}</p>
            <p style={{ fontSize: "0.85rem", marginTop: "0.5rem" }}>{t(lang, "marketplace.emptyHint")}</p>
          </div>
        )}
      </div>
  );
}