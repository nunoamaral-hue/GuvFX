"use client";

import { useEffect, useState, useMemo, useCallback, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useLang } from "@/components/AppShell";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { apiFetch } from "@/lib/api";
import { t } from "@/lib/i18n";
import type { TradingAccount } from "@/types/strategies";

const API_BASE = "https://api.guvfx.com";

// =============================================================================
// Types
// =============================================================================

type TradeRow = {
  ticket: string;
  symbol: string;
  side: string;
  volume: string;
  open_time: string;
  close_time: string | null;
  open_price: string;
  close_price: string | null;
  profit: string;
  commission: string;
  swap: string;
  net_pnl: string;
  magic_number: number | null;
  comment: string;
  strategy_name: string;
};

// =============================================================================
// SVG Chart Components (inline, charts-first)
// =============================================================================

/**
 * BalanceTrajectoryChart - Shows observed equity/balance trajectory over trades.
 * Compliance-safe: observational only, no performance claims.
 */
function BalanceTrajectoryChart({
  trades,
  initialBalance = 10000,
  width = 500,
  height = 180,
}: {
  trades: TradeRow[];
  initialBalance?: number;
  width?: number;
  height?: number;
}) {
  const balancePoints = useMemo(() => {
    if (trades.length === 0) return [];
    let balance = initialBalance;
    const points = [{ index: 0, balance }];
    for (let i = 0; i < trades.length; i++) {
      const netPnl = parseFloat(trades[i].net_pnl) || 0;
      balance += netPnl;
      points.push({ index: i + 1, balance });
    }
    return points;
  }, [trades, initialBalance]);

  if (balancePoints.length < 2) {
    return null;
  }

  const values = balancePoints.map((p) => p.balance);
  const minVal = Math.min(...values);
  const maxVal = Math.max(...values);
  const range = maxVal - minVal || 1;

  const padding = { top: 16, right: 12, bottom: 24, left: 50 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;

  const points = balancePoints.map((p, i) => {
    const x = padding.left + (i / (balancePoints.length - 1)) * chartWidth;
    const y = padding.top + chartHeight - ((p.balance - minVal) / range) * chartHeight;
    return { x, y };
  });

  const linePath = `M${points.map((p) => `${p.x},${p.y}`).join(" L")}`;
  const areaPath = `${linePath} L${points[points.length - 1].x},${padding.top + chartHeight} L${points[0].x},${padding.top + chartHeight} Z`;

  const yLabels = [minVal, (minVal + maxVal) / 2, maxVal];
  const endColor = values[values.length - 1] >= values[0] ? "#22c55e" : "#ef4444";

  return (
    <svg
      width="100%"
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="xMidYMid meet"
      style={{ display: "block", maxWidth: "100%" }}
    >
      {/* Grid lines */}
      {yLabels.map((val, i) => {
        const y = padding.top + chartHeight - ((val - minVal) / range) * chartHeight;
        return (
          <g key={i}>
            <line
              x1={padding.left}
              y1={y}
              x2={padding.left + chartWidth}
              y2={y}
              stroke="#1e293b"
              strokeWidth={1}
            />
            <text x={padding.left - 6} y={y + 3} textAnchor="end" fill="#64748b" fontSize="9">
              {val.toFixed(0)}
            </text>
          </g>
        );
      })}

      {/* Area fill */}
      <path d={areaPath} fill="rgba(59, 130, 246, 0.1)" />

      {/* Line */}
      <path d={linePath} fill="none" stroke="#3b82f6" strokeWidth={2} strokeLinejoin="round" />

      {/* Start/end markers */}
      <circle cx={points[0].x} cy={points[0].y} r={3} fill="#3b82f6" />
      <circle cx={points[points.length - 1].x} cy={points[points.length - 1].y} r={4} fill={endColor} />

      {/* X-axis label */}
      <text
        x={padding.left + chartWidth / 2}
        y={height - 4}
        textAnchor="middle"
        fill="#64748b"
        fontSize="9"
      >
        Trade sequence
      </text>
    </svg>
  );
}

/**
 * OutcomeDistributionChart - Shows win/loss count distribution as bars.
 * Compliance-safe: counts only, no monetary values.
 */
function OutcomeDistributionChart({
  trades,
  width = 300,
  height = 140,
}: {
  trades: TradeRow[];
  width?: number;
  height?: number;
}) {
  const { wins, losses } = useMemo(() => {
    let w = 0;
    let l = 0;
    for (const trade of trades) {
      const netPnl = parseFloat(trade.net_pnl) || 0;
      if (netPnl >= 0) w++;
      else l++;
    }
    return { wins: w, losses: l };
  }, [trades]);

  if (trades.length === 0) return null;

  const maxCount = Math.max(wins, losses, 1);
  const padding = { top: 20, right: 16, bottom: 28, left: 16 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const barWidth = chartWidth / 4;
  const gap = chartWidth / 8;

  const winHeight = (wins / maxCount) * chartHeight;
  const lossHeight = (losses / maxCount) * chartHeight;

  return (
    <svg
      width="100%"
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="xMidYMid meet"
      style={{ display: "block", maxWidth: "100%" }}
    >
      {/* Win bar */}
      <rect
        x={padding.left + gap}
        y={padding.top + chartHeight - winHeight}
        width={barWidth}
        height={winHeight}
        fill="#22c55e"
        rx={3}
      />
      <text
        x={padding.left + gap + barWidth / 2}
        y={padding.top + chartHeight - winHeight - 6}
        textAnchor="middle"
        fill="#22c55e"
        fontSize="11"
        fontWeight="500"
      >
        {wins}
      </text>
      <text
        x={padding.left + gap + barWidth / 2}
        y={height - 8}
        textAnchor="middle"
        fill="#9ca3af"
        fontSize="9"
      >
        Positive
      </text>

      {/* Loss bar */}
      <rect
        x={padding.left + gap * 2 + barWidth + gap}
        y={padding.top + chartHeight - lossHeight}
        width={barWidth}
        height={lossHeight}
        fill="#ef4444"
        rx={3}
      />
      <text
        x={padding.left + gap * 2 + barWidth + gap + barWidth / 2}
        y={padding.top + chartHeight - lossHeight - 6}
        textAnchor="middle"
        fill="#ef4444"
        fontSize="11"
        fontWeight="500"
      >
        {losses}
      </text>
      <text
        x={padding.left + gap * 2 + barWidth + gap + barWidth / 2}
        y={height - 8}
        textAnchor="middle"
        fill="#9ca3af"
        fontSize="9"
      >
        Negative
      </text>
    </svg>
  );
}

/**
 * DrawdownUnderwaterChart - Shows observed drawdown periods.
 * Compliance-safe: observational, shows underwater depth over trade sequence.
 */
function DrawdownUnderwaterChart({
  trades,
  initialBalance = 10000,
  width = 500,
  height = 100,
}: {
  trades: TradeRow[];
  initialBalance?: number;
  width?: number;
  height?: number;
}) {
  const drawdownData = useMemo(() => {
    if (trades.length === 0) return [];
    let balance = initialBalance;
    let peak = balance;
    const points: number[] = [0]; // Start with 0% drawdown

    for (const trade of trades) {
      const netPnl = parseFloat(trade.net_pnl) || 0;
      balance += netPnl;
      if (balance > peak) peak = balance;
      const dd = peak > 0 ? ((peak - balance) / peak) * 100 : 0;
      points.push(dd);
    }
    return points;
  }, [trades, initialBalance]);

  if (drawdownData.length < 2) return null;

  const maxDD = Math.max(...drawdownData, 1);
  const padding = { top: 8, right: 12, bottom: 20, left: 50 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;

  const points = drawdownData.map((dd, i) => {
    const x = padding.left + (i / (drawdownData.length - 1)) * chartWidth;
    const y = padding.top + (dd / maxDD) * chartHeight;
    return { x, y };
  });

  const areaPath = `M${padding.left},${padding.top} L${points.map((p) => `${p.x},${p.y}`).join(" L")} L${padding.left + chartWidth},${padding.top} Z`;
  const linePath = `M${points.map((p) => `${p.x},${p.y}`).join(" L")}`;

  return (
    <svg
      width="100%"
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="xMidYMid meet"
      style={{ display: "block", maxWidth: "100%" }}
    >
      {/* Zero line */}
      <line
        x1={padding.left}
        y1={padding.top}
        x2={padding.left + chartWidth}
        y2={padding.top}
        stroke="#334155"
        strokeWidth={1}
        strokeDasharray="3,3"
      />

      {/* Max DD label */}
      <text x={padding.left - 6} y={padding.top + chartHeight} textAnchor="end" fill="#64748b" fontSize="8">
        -{maxDD.toFixed(1)}%
      </text>

      {/* Area fill */}
      <path d={areaPath} fill="rgba(239, 68, 68, 0.2)" />

      {/* Line */}
      <path d={linePath} fill="none" stroke="#ef4444" strokeWidth={1.5} strokeLinejoin="round" />

      {/* Label */}
      <text x={padding.left + chartWidth / 2} y={height - 4} textAnchor="middle" fill="#64748b" fontSize="9">
        Underwater (observed)
      </text>
    </svg>
  );
}

// =============================================================================
// Main Component
// =============================================================================

export default function TradeHistoryPage() {
  const lang = useLang();
  const router = useRouter();
  const searchParams = useSearchParams();

  // Accounts for filter dropdown
  const [accounts, setAccounts] = useState<TradingAccount[]>([]);
  const [loadingAccounts, setLoadingAccounts] = useState(false);

  // Selected account
  const [selectedAccountId, setSelectedAccountId] = useState<string>("");

  // Trade data
  const [trades, setTrades] = useState<TradeRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Auto-refresh state (for demo trade flow)
  const [autoRefreshCount, setAutoRefreshCount] = useState(0);
  const autoRefreshTimerRef = useRef<NodeJS.Timeout | null>(null);
  const initialTradeCountRef = useRef<number | null>(null);

  // Fetch trades with cache-busting (no custom headers to avoid CORS preflight)
  const fetchTrades = useCallback(async (accountId: string) => {
    if (!accountId) {
      setTrades([]);
      return;
    }

    setLoading(true);
    setError(null);
    try {
      // Cache-busting via query param only (no custom headers to avoid preflight)
      const cacheBuster = Date.now();
      const res = await fetch(
        `${API_BASE}/api/analytics/trade-history/?account=${accountId}&_t=${cacheBuster}`,
        {
          credentials: "include",
          cache: "no-store",
        }
      );
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${res.statusText}`);
      }
      const data = await res.json();
      const newTrades = Array.isArray(data?.trades) ? data.trades : [];
      setTrades(newTrades);
      return newTrades;
    } catch (err) {
      // Surface helpful error message with status code if available
      const msg = err instanceof Error ? err.message : "Failed to load trade history";
      setError(msg);
      setTrades([]);
      return [];
    } finally {
      setLoading(false);
    }
  }, []);

  // Fetch accounts
  useEffect(() => {
    const loadAccounts = async () => {
      setLoadingAccounts(true);
      try {
        const data = await apiFetch<TradingAccount[]>("/api/trading/accounts/", {});
        setAccounts(data);

        // Check for account param in URL (from demo trade redirect)
        const urlAccountId = searchParams.get("account");
        if (urlAccountId) {
          setSelectedAccountId(urlAccountId);
        } else if (data.length > 0 && !selectedAccountId) {
          // Auto-select first account if available
          setSelectedAccountId(String(data[0].id));
        }
      } catch (err) {
        console.error("Failed to fetch accounts:", err);
        setAccounts([]);
      } finally {
        setLoadingAccounts(false);
      }
    };
    loadAccounts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Handle auto-refresh from URL param (for demo trade flow)
  useEffect(() => {
    const autoRefresh = searchParams.get("autorefresh");
    if (autoRefresh === "1" && selectedAccountId) {
      // Start auto-refresh sequence: fetch at 0s, 3s, 6s, 9s
      setAutoRefreshCount(0);
      initialTradeCountRef.current = null;

      const doAutoRefresh = async (attempt: number) => {
        if (attempt >= 4) {
          // Stop after 4 attempts
          setAutoRefreshCount(0);
          // Clear URL param
          const url = new URL(window.location.href);
          url.searchParams.delete("autorefresh");
          window.history.replaceState({}, "", url.toString());
          return;
        }

        setAutoRefreshCount(attempt + 1);
        const newTrades = await fetchTrades(selectedAccountId);

        // On first fetch, record initial count
        if (attempt === 0) {
          initialTradeCountRef.current = newTrades?.length ?? 0;
        }

        // Check if new trades appeared
        const currentCount = newTrades?.length ?? 0;
        const initialCount = initialTradeCountRef.current ?? 0;
        if (currentCount > initialCount && attempt > 0) {
          // New trades found, stop refreshing
          setAutoRefreshCount(0);
          const url = new URL(window.location.href);
          url.searchParams.delete("autorefresh");
          window.history.replaceState({}, "", url.toString());
          return;
        }

        // Schedule next attempt
        autoRefreshTimerRef.current = setTimeout(() => doAutoRefresh(attempt + 1), 3000);
      };

      doAutoRefresh(0);

      return () => {
        if (autoRefreshTimerRef.current) {
          clearTimeout(autoRefreshTimerRef.current);
        }
      };
    }
  }, [searchParams, selectedAccountId, fetchTrades]);

  // Fetch trades when account changes (normal flow)
  useEffect(() => {
    // Skip if auto-refresh is active (it handles fetching)
    const autoRefresh = searchParams.get("autorefresh");
    if (autoRefresh === "1") return;

    fetchTrades(selectedAccountId);
  }, [selectedAccountId, fetchTrades, searchParams]);

  // Compute observed statistics (counts only, no monetary values emphasized)
  const stats = useMemo(() => {
    if (trades.length === 0) return null;

    let wins = 0;
    let losses = 0;
    let currentStreak = 0;
    let maxLossStreak = 0;
    let balance = 10000;
    let peak = balance;
    let maxDrawdownPct = 0;

    for (const trade of trades) {
      const netPnl = parseFloat(trade.net_pnl) || 0;
      if (netPnl >= 0) {
        wins++;
        currentStreak = 0;
      } else {
        losses++;
        currentStreak++;
        maxLossStreak = Math.max(maxLossStreak, currentStreak);
      }
      balance += netPnl;
      if (balance > peak) peak = balance;
      const dd = peak > 0 ? ((peak - balance) / peak) * 100 : 0;
      maxDrawdownPct = Math.max(maxDrawdownPct, dd);
    }

    const hitRate = trades.length > 0 ? (wins / trades.length) * 100 : 0;

    return {
      totalTrades: trades.length,
      wins,
      losses,
      observedHitRate: hitRate,
      longestLossStreak: maxLossStreak,
      maxDrawdownObserved: maxDrawdownPct,
    };
  }, [trades]);

  const hasData = trades.length > 0;

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      {/* Header */}
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem", color: "#f0f6ff" }}>
        {t(lang, "tradeHistory.title")}
      </h1>
      <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "0.5rem" }}>
        {t(lang, "tradeHistory.subtitle")}
      </p>
      <p style={{ fontSize: "0.75rem", color: "#64748b", marginBottom: "0.35rem" }}>
        {t(lang, "legal.microDisclaimer")}
      </p>
      <p
        style={{
          fontSize: "0.72rem",
          color: "#64748b",
          marginBottom: "1rem",
          lineHeight: 1.5,
        }}
      >
        {t(lang, "tradeHistory.disclaimerLine1")}
      </p>

      {/* Filters */}
      <Card>
        <div
          style={{
            display: "flex",
            gap: "1rem",
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          <div>
            <label
              style={{
                display: "block",
                fontSize: "0.8rem",
                color: "#9ca3af",
                marginBottom: "0.25rem",
              }}
            >
              {t(lang, "tradeHistory.filterAccountLabel")}
            </label>
            <select
              value={selectedAccountId}
              onChange={(e) => setSelectedAccountId(e.target.value)}
              disabled={loadingAccounts || accounts.length === 0}
              style={{
                padding: "0.5rem 0.75rem",
                borderRadius: 6,
                border: "1px solid rgba(148,163,184,0.5)",
                background: "rgba(3,7,18,0.9)",
                color: "#e5f4ff",
                fontSize: "0.9rem",
                minWidth: 200,
              }}
            >
              {accounts.length === 0 && (
                <option value="">{t(lang, "tradeHistory.noAccountsOption")}</option>
              )}
              {accounts.map((acc) => (
                <option key={acc.id} value={acc.id}>
                  {acc.name} ({acc.broker_name})
                </option>
              ))}
            </select>
          </div>

          <Button
            variant="secondary"
            onClick={() => fetchTrades(selectedAccountId)}
            disabled={loading || !selectedAccountId}
            style={{ marginTop: "1.25rem" }}
          >
            {loading
              ? autoRefreshCount > 0
                ? `${t(lang, "tradeHistory.refreshing")} (${autoRefreshCount}/4)...`
                : t(lang, "tradeHistory.refreshing")
              : t(lang, "tradeHistory.refresh")}
          </Button>

          {error && (
            <span style={{ color: "#f87171", fontSize: "0.85rem", marginTop: "1.25rem" }}>
              {error}
            </span>
          )}
        </div>
      </Card>

      {/* Empty state */}
      {!loading && !hasData && (
        <Card>
          <div style={{ textAlign: "center", padding: "2rem 1rem" }}>
            <h3
              style={{
                fontSize: "1.1rem",
                color: "#e5f4ff",
                marginBottom: "0.5rem",
              }}
            >
              {t(lang, "tradeHistory.emptyTitle")}
            </h3>
            <p
              style={{
                fontSize: "0.88rem",
                color: "#9ca3af",
                marginBottom: "1.25rem",
                maxWidth: 420,
                marginLeft: "auto",
                marginRight: "auto",
              }}
            >
              {t(lang, "tradeHistory.emptyBody")}
            </p>
            <div
              style={{
                display: "flex",
                gap: "0.75rem",
                justifyContent: "center",
                flexWrap: "wrap",
              }}
            >
              <Button variant="primary" onClick={() => router.push("/accounts")}>
                {t(lang, "tradeHistory.ctaLinkAccount")}
              </Button>
              <Button variant="secondary" onClick={() => router.push("/trading/live-trading")}>
                {t(lang, "tradeHistory.ctaLiveTrading")}
              </Button>
            </div>
          </div>
        </Card>
      )}

      {/* Charts Section */}
      {hasData && (
        <Card
          title={t(lang, "tradeHistory.sectionChartsTitle")}
          subtitle={t(lang, "tradeHistory.sectionChartsSubtitle")}
        >
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr",
              gap: "1.5rem",
            }}
          >
            {/* Balance trajectory chart */}
            <div>
              <h4
                style={{
                  fontSize: "0.85rem",
                  color: "#b7c5dd",
                  marginBottom: "0.5rem",
                }}
              >
                {t(lang, "tradeHistory.chartEquityTitle")}
              </h4>
              <div
                style={{
                  background: "rgba(15,23,42,0.5)",
                  borderRadius: 8,
                  padding: "0.75rem",
                  border: "1px solid rgba(148,163,184,0.15)",
                }}
              >
                <BalanceTrajectoryChart trades={trades} />
              </div>
            </div>

            {/* Outcome distribution chart */}
            <div>
              <h4
                style={{
                  fontSize: "0.85rem",
                  color: "#b7c5dd",
                  marginBottom: "0.5rem",
                }}
              >
                {t(lang, "tradeHistory.chartOutcomesTitle")}
              </h4>
              <div
                style={{
                  background: "rgba(15,23,42,0.5)",
                  borderRadius: 8,
                  padding: "0.75rem",
                  border: "1px solid rgba(148,163,184,0.15)",
                }}
              >
                <OutcomeDistributionChart trades={trades} />
              </div>
            </div>
          </div>

          {/* Drawdown chart (full width) */}
          <div style={{ marginTop: "1rem" }}>
            <h4
              style={{
                fontSize: "0.85rem",
                color: "#b7c5dd",
                marginBottom: "0.5rem",
              }}
            >
              {t(lang, "tradeHistory.chartDrawdownTitle")}
            </h4>
            <div
              style={{
                background: "rgba(15,23,42,0.5)",
                borderRadius: 8,
                padding: "0.75rem",
                border: "1px solid rgba(148,163,184,0.15)",
              }}
            >
              <DrawdownUnderwaterChart trades={trades} />
            </div>
          </div>
        </Card>
      )}

      {/* Observed Statistics */}
      {hasData && stats && (
        <Card
          title={t(lang, "tradeHistory.sectionDetailsTitle")}
          subtitle={t(lang, "tradeHistory.sectionDetailsSubtitle")}
        >
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
              gap: "1rem",
            }}
          >
            <div>
              <div style={{ fontSize: "0.78rem", color: "#9ca3af" }}>
                {t(lang, "tradeHistory.statTrades")}
              </div>
              <div style={{ fontSize: "1.1rem", color: "#e5f4ff", fontWeight: 500 }}>
                {stats.totalTrades}
              </div>
            </div>
            <div>
              <div style={{ fontSize: "0.78rem", color: "#9ca3af" }}>
                {t(lang, "tradeHistory.statObservedHitRate")}
              </div>
              <div style={{ fontSize: "1.1rem", color: "#e5f4ff", fontWeight: 500 }}>
                {stats.observedHitRate.toFixed(1)}%
              </div>
            </div>
            <div>
              <div style={{ fontSize: "0.78rem", color: "#9ca3af" }}>
                {t(lang, "tradeHistory.statLongestLossStreak")}
              </div>
              <div style={{ fontSize: "1.1rem", color: "#e5f4ff", fontWeight: 500 }}>
                {stats.longestLossStreak}
              </div>
            </div>
            <div>
              <div style={{ fontSize: "0.78rem", color: "#9ca3af" }}>
                {t(lang, "tradeHistory.statMaxDrawdown")}
              </div>
              <div style={{ fontSize: "1.1rem", color: "#ef4444", fontWeight: 500 }}>
                -{stats.maxDrawdownObserved.toFixed(1)}%
              </div>
            </div>
          </div>
        </Card>
      )}

      {/* Trades Table */}
      {hasData && (
        <Card
          title={t(lang, "tradeHistory.sectionTradesTitle")}
          subtitle={t(lang, "tradeHistory.sectionTradesSubtitle")}
        >
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  {[
                    t(lang, "tradeHistory.colTime"),
                    t(lang, "tradeHistory.colTicket"),
                    t(lang, "tradeHistory.colSymbol"),
                    t(lang, "tradeHistory.colSide"),
                    t(lang, "tradeHistory.colVolume"),
                    t(lang, "tradeHistory.colOutcome"),
                    t(lang, "tradeHistory.colStrategy"),
                  ].map((h) => (
                    <th
                      key={h}
                      style={{
                        textAlign: "left",
                        padding: "0.5rem 0.75rem",
                        borderBottom: "1px solid rgba(255,255,255,0.1)",
                        fontSize: "0.8rem",
                        color: "#9ca3af",
                        fontWeight: 500,
                      }}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {trades.map((r) => {
                  const netPnl = parseFloat(r.net_pnl) || 0;
                  const isPositive = netPnl >= 0;
                  return (
                    <tr
                      key={r.ticket}
                      style={{
                        borderBottom: "1px solid rgba(255,255,255,0.05)",
                      }}
                    >
                      <td style={{ padding: "0.5rem 0.75rem", fontSize: "0.85rem", color: "#b7c5dd" }}>
                        {r.close_time || r.open_time}
                      </td>
                      <td style={{ padding: "0.5rem 0.75rem", fontSize: "0.85rem", color: "#e5f4ff" }}>
                        {r.ticket}
                      </td>
                      <td style={{ padding: "0.5rem 0.75rem", fontSize: "0.85rem", color: "#e5f4ff" }}>
                        {r.symbol}
                      </td>
                      <td style={{ padding: "0.5rem 0.75rem" }}>
                        <Badge color={r.side === "BUY" ? "blue" : "gray"}>
                          {r.side}
                        </Badge>
                      </td>
                      <td style={{ padding: "0.5rem 0.75rem", fontSize: "0.85rem", color: "#b7c5dd" }}>
                        {r.volume}
                      </td>
                      <td style={{ padding: "0.5rem 0.75rem" }}>
                        <span
                          style={{
                            color: isPositive ? "#22c55e" : "#ef4444",
                            fontWeight: 500,
                            fontSize: "0.85rem",
                          }}
                        >
                          {isPositive ? "+" : ""}
                          {netPnl.toFixed(2)}
                        </span>
                      </td>
                      <td style={{ padding: "0.5rem 0.75rem", fontSize: "0.85rem", color: "#9ca3af" }}>
                        {r.strategy_name || "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* Loading state */}
      {loading && (
        <Card>
          <p style={{ fontSize: "0.9rem", color: "#9ca3af", textAlign: "center", padding: "2rem" }}>
            {t(lang, "tradeHistory.loading")}
          </p>
        </Card>
      )}
    </div>
  );
}
