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
const ACCOUNT_STORAGE_KEY = "guvfx_trade_history_account";

// =============================================================================
// Types
// =============================================================================

// Round-trip row (BUY+SELL paired)
type RoundTripRow = {
  open_time: string;
  close_time: string | null;
  symbol: string;
  volume: string;
  open_price: string | null;
  close_price: string | null;
  net_pnl: string;
  net_pnl_money?: number; // Numeric for currency formatting
  legs: [string, string]; // [buy_ticket, sell_ticket]
  buy_ticket: string;
  sell_ticket: string;
  comment: string;
  strategy_name: string;
  // New UI-friendly fields
  trade_closed?: string;
  trade_numbers?: string;
  direction?: string;
  // Breakdown for debugging
  buy_profit?: string;
  sell_profit?: string;
  total_commission?: string;
  total_swap?: string;
};

// Balance series point (from backend)
type BalancePoint = {
  index: number;
  trade_closed: string | null;
  net_pnl_money: number;
  balance_after_trade: number;
};

// Observed statistics from backend
type ObservedStats = {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  longest_loss_streak: number;
  max_drawdown_pct: number;
  net_pnl_total: number;
  opening_balance?: number;
  closing_balance?: number;
};

// API response type for roundtrip mode
type TradeHistoryResponse = {
  account_id: number;
  mode: string;
  count: number;
  trades: (RoundTripRow | DealRow)[];
  // MT5 account info
  mt5_balance_current?: number | null;
  mt5_equity_current?: number | null;
  currency?: string;
  // Balance trajectory
  opening_balance_used?: number;
  opening_balance_source?: string;
  balance_series?: BalancePoint[];
  observed_stats?: ObservedStats;
};

// Legacy deal row (individual BUY or SELL)
type DealRow = {
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

// Daily PnL row from /api/analytics/daily-pnl/
type DailyPnlRow = {
  date: string;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  net_pnl: number;
  gross_profit: number;
  gross_loss: number;
};

type DailyPnlResponse = {
  account_id: number;
  strategy_id: number | null;
  mode: string;
  days: number;
  series: DailyPnlRow[];
  totals: {
    trades: number;
    wins: number;
    losses: number;
    win_rate: number;
    net_pnl: number;
    gross_profit: number;
    gross_loss: number;
  };
};

// Union type for either mode
type TradeRow = RoundTripRow | DealRow;

// Type guard to check if it's a round-trip
function isRoundTrip(row: TradeRow): row is RoundTripRow {
  return "legs" in row && Array.isArray((row as RoundTripRow).legs);
}

// =============================================================================
// Chart Helpers
// =============================================================================

/** Format money values for Y-axis readability */
function formatMoneyAxis(v: number) {
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${(v / 1_000).toFixed(2)}K`;
  return v.toFixed(2);
}

/** Clamp a number between min and max */
function clamp(n: number, a: number, b: number) {
  return Math.max(a, Math.min(b, n));
}

// =============================================================================
// Chart Components
// =============================================================================

/**
 * BalanceTrajectoryChart - Shows observed equity/balance trajectory over trades.
 * Interactive SVG with hover tooltips and MT5 reference line.
 * Compliance-safe: observational only, no performance claims.
 */
function BalanceTrajectoryChart({
  balanceSeries,
  mt5BalanceCurrent,
  currency = "USD",
  width = 500,
  height = 200,
}: {
  balanceSeries: BalancePoint[];
  mt5BalanceCurrent?: number | null;
  currency?: string;
  width?: number;
  height?: number;
}) {
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);

  if (!balanceSeries || balanceSeries.length === 0) {
    return (
      <div style={{ height, display: "flex", alignItems: "center", justifyContent: "center", color: "#64748b" }}>
        No balance data available
      </div>
    );
  }

  // Extract balance values
  const values = balanceSeries.map((p) => p.balance_after_trade);

  // Include MT5 balance in range if available for proper scaling
  const allValues = mt5BalanceCurrent !== null && mt5BalanceCurrent !== undefined
    ? [...values, mt5BalanceCurrent]
    : values;
  const rangeMin = Math.min(...allValues);
  const rangeMax = Math.max(...allValues);

  // Add padding to Y-axis for readability (15% padding on each side, min 0.5)
  const range = Math.max(0.01, rangeMax - rangeMin);
  const pad = Math.max(0.5, range * 0.15);
  const yMin = rangeMin - pad;
  const yMax = rangeMax + pad;
  const yRange = yMax - yMin;

  const padding = { top: 20, right: 70, bottom: 30, left: 70 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;

  // Map data points to SVG coordinates
  const points = balanceSeries.map((p, i) => ({
    x: padding.left + (i / Math.max(1, balanceSeries.length - 1)) * chartWidth,
    y: padding.top + chartHeight - ((p.balance_after_trade - yMin) / yRange) * chartHeight,
    data: p,
  }));

  // Create line path
  const linePath = `M${points.map((p) => `${p.x},${p.y}`).join(" L")}`;

  // Create area fill path
  const areaPath = `${linePath} L${points[points.length - 1].x},${padding.top + chartHeight} L${points[0].x},${padding.top + chartHeight} Z`;

  // Generate Y-axis ticks (5 ticks for better readability)
  const yTicks = Array.from({ length: 5 }, (_, i) => {
    const value = yMin + (yRange * i) / 4;
    const y = padding.top + chartHeight - (i / 4) * chartHeight;
    return { value, y };
  });

  // MT5 reference line position
  const mt5RefY = mt5BalanceCurrent !== null && mt5BalanceCurrent !== undefined
    ? padding.top + chartHeight - ((mt5BalanceCurrent - yMin) / yRange) * chartHeight
    : null;

  // Line color based on trend
  const endColor = values[values.length - 1] >= values[0] ? "#22c55e" : "#ef4444";
  const lineColor = "#22d3ee";

  // Hovered point data
  const hoveredPoint = hoveredIndex !== null ? points[hoveredIndex] : null;

  return (
    <svg
      width="100%"
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="xMidYMid meet"
      style={{ display: "block", maxWidth: "100%" }}
    >
      {/* Grid lines */}
      {yTicks.map((tick, i) => (
        <g key={i}>
          <line
            x1={padding.left}
            y1={tick.y}
            x2={padding.left + chartWidth}
            y2={tick.y}
            stroke="#1e293b"
            strokeWidth={1}
            strokeDasharray="3 3"
          />
          <text
            x={padding.left - 8}
            y={tick.y + 4}
            textAnchor="end"
            fill="#64748b"
            fontSize="10"
          >
            {formatMoneyAxis(tick.value)}
          </text>
        </g>
      ))}

      {/* MT5 Balance reference line (dashed) */}
      {mt5RefY !== null && (
        <g>
          <line
            x1={padding.left}
            y1={mt5RefY}
            x2={padding.left + chartWidth}
            y2={mt5RefY}
            stroke="#38bdf8"
            strokeWidth={1.5}
            strokeDasharray="4 4"
          />
          <text
            x={padding.left + chartWidth + 5}
            y={mt5RefY + 4}
            textAnchor="start"
            fill="#38bdf8"
            fontSize="9"
          >
            MT5 {formatMoneyAxis(mt5BalanceCurrent!)} {currency}
          </text>
        </g>
      )}

      {/* Area fill */}
      <path d={areaPath} fill="rgba(34, 211, 238, 0.1)" />

      {/* Line */}
      <path
        d={linePath}
        fill="none"
        stroke={lineColor}
        strokeWidth={2}
        strokeLinejoin="round"
      />

      {/* Data points (circles) */}
      {points.map((p, i) => (
        <circle
          key={i}
          cx={p.x}
          cy={p.y}
          r={hoveredIndex === i ? 6 : 4}
          fill={i === points.length - 1 ? endColor : lineColor}
          stroke="#0f172a"
          strokeWidth={1}
          style={{ cursor: "pointer" }}
          onMouseEnter={() => setHoveredIndex(i)}
          onMouseLeave={() => setHoveredIndex(null)}
        />
      ))}

      {/* X-axis label */}
      <text
        x={padding.left + chartWidth / 2}
        y={height - 6}
        textAnchor="middle"
        fill="#64748b"
        fontSize="10"
      >
        Trade sequence (#{1} → #{balanceSeries.length})
      </text>

      {/* Tooltip */}
      {hoveredPoint && (() => {
        // Calculate tooltip position with clamping to stay within bounds
        const tooltipWidth = 160;
        const tooltipHeight = 72;
        const tooltipX = clamp(hoveredPoint.x + 10, 5, width - tooltipWidth - 5);
        const tooltipY = clamp(hoveredPoint.y - tooltipHeight - 5, 5, height - tooltipHeight - 5);
        // Format trade_closed for display (show date/time portion)
        const tradeClosed = hoveredPoint.data.trade_closed
          ? hoveredPoint.data.trade_closed.replace("T", " ").slice(0, 19)
          : `Trade #${hoveredPoint.data.index + 1}`;
        return (
          <g>
            {/* Tooltip background */}
            <rect
              x={tooltipX}
              y={tooltipY}
              width={tooltipWidth}
              height={tooltipHeight}
              rx={6}
              fill="#0f172a"
              stroke="#1e293b"
              strokeWidth={1}
            />
            {/* Trade Closed */}
            <text
              x={tooltipX + 8}
              y={tooltipY + 16}
              fill="#94a3b8"
              fontSize="9"
            >
              Closed: {tradeClosed}
            </text>
            {/* Balance */}
            <text
              x={tooltipX + 8}
              y={tooltipY + 34}
              fill="#e5f4ff"
              fontSize="10"
              fontWeight="500"
            >
              Balance: {hoveredPoint.data.balance_after_trade.toFixed(2)} {currency}
            </text>
            {/* PnL */}
            <text
              x={tooltipX + 8}
              y={tooltipY + 52}
              fill={hoveredPoint.data.net_pnl_money >= 0 ? "#22c55e" : "#ef4444"}
              fontSize="10"
            >
              PnL: {hoveredPoint.data.net_pnl_money >= 0 ? "+" : ""}{hoveredPoint.data.net_pnl_money.toFixed(2)} {currency}
            </text>
          </g>
        );
      })()}
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

  // MT5 account data from backend
  const [mt5BalanceCurrent, setMt5BalanceCurrent] = useState<number | null>(null);
  const [currency, setCurrency] = useState<string>("USD");
  const [balanceSeries, setBalanceSeries] = useState<BalancePoint[]>([]);
  const [observedStats, setObservedStats] = useState<ObservedStats | null>(null);

  // Stage filter (LIVE / TEST / ALL)
  const [stageFilter, setStageFilter] = useState<"ALL" | "LIVE" | "TEST">("ALL");

  // Daily PnL state
  const [dailyPnl, setDailyPnl] = useState<DailyPnlResponse | null>(null);
  const [dailyPnlDays, setDailyPnlDays] = useState(30);
  const [loadingDailyPnl, setLoadingDailyPnl] = useState(false);

  // Auto-refresh state (for demo trade flow)
  const [autoRefreshCount, setAutoRefreshCount] = useState(0);
  const autoRefreshTimerRef = useRef<NodeJS.Timeout | null>(null);
  const initialTradeCountRef = useRef<number | null>(null);

  // Fetch trades with cache-busting (no custom headers to avoid CORS preflight)
  // Uses mode=roundtrip by default to show completed round-trips (BUY+SELL paired)
  const fetchTrades = useCallback(async (accountId: string) => {
    if (!accountId) {
      setTrades([]);
      setMt5BalanceCurrent(null);
      setBalanceSeries([]);
      setObservedStats(null);
      return;
    }

    setLoading(true);
    setError(null);
    try {
      // Cache-busting via query param only (no custom headers to avoid preflight)
      // Use mode=roundtrip to get paired BUY+SELL as single rows
      const cacheBuster = Date.now();
      const res = await fetch(
        `${API_BASE}/api/analytics/trade-history/?account=${accountId}&mode=roundtrip&stage=${stageFilter}&_t=${cacheBuster}`,
        {
          credentials: "include",
          cache: "no-store",
        }
      );
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${res.statusText}`);
      }
      const data: TradeHistoryResponse = await res.json();
      const newTrades = Array.isArray(data?.trades) ? data.trades : [];
      setTrades(newTrades);

      // Set MT5 balance and currency from backend
      setMt5BalanceCurrent(data.mt5_balance_current ?? null);
      setCurrency(data.currency || "USD");

      // Set balance series and observed stats from backend
      setBalanceSeries(data.balance_series || []);
      setObservedStats(data.observed_stats || null);

      return newTrades;
    } catch (err) {
      // Surface helpful error message with status code if available
      const msg = err instanceof Error ? err.message : "Failed to load trade history";
      setError(msg);
      setTrades([]);
      setMt5BalanceCurrent(null);
      setBalanceSeries([]);
      setObservedStats(null);
      return [];
    } finally {
      setLoading(false);
    }
  }, [stageFilter]);

  // Fetch accounts
  useEffect(() => {
    const loadAccounts = async () => {
      setLoadingAccounts(true);
      try {
        const data = await apiFetch<TradingAccount[]>("/api/trading/accounts/", {});
        setAccounts(data);

        // Check for account param in URL (from demo trade redirect) - highest priority
        const urlAccountId = searchParams.get("account");
        if (urlAccountId) {
          setSelectedAccountId(urlAccountId);
          // Save to localStorage for next visit
          try {
            localStorage.setItem(ACCOUNT_STORAGE_KEY, urlAccountId);
          } catch {
            // Ignore storage errors
          }
        } else {
          // Check localStorage for last used account
          let savedAccountId: string | null = null;
          try {
            savedAccountId = localStorage.getItem(ACCOUNT_STORAGE_KEY);
          } catch {
            // Ignore storage errors
          }

          // Use saved account if it exists in the list, otherwise use first account
          if (savedAccountId && data.some((acc) => String(acc.id) === savedAccountId)) {
            setSelectedAccountId(savedAccountId);
          } else if (data.length > 0 && !selectedAccountId) {
            // Auto-select first account if available
            setSelectedAccountId(String(data[0].id));
          }
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

  // Fetch daily PnL when account or days changes
  useEffect(() => {
    if (!selectedAccountId) {
      setDailyPnl(null);
      return;
    }
    let cancelled = false;
    const fetchDailyPnl = async () => {
      setLoadingDailyPnl(true);
      try {
        const res = await fetch(
          `${API_BASE}/api/analytics/daily-pnl/?account_id=${selectedAccountId}&days=${dailyPnlDays}&stage=${stageFilter}`,
          { credentials: "include", cache: "no-store" }
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: DailyPnlResponse = await res.json();
        if (!cancelled) setDailyPnl(data);
      } catch {
        if (!cancelled) setDailyPnl(null);
      } finally {
        if (!cancelled) setLoadingDailyPnl(false);
      }
    };
    fetchDailyPnl();
    return () => { cancelled = true; };
  }, [selectedAccountId, dailyPnlDays, stageFilter]);

  // Use observed statistics from backend, with local fallback
  const stats = useMemo(() => {
    // Use backend stats if available
    if (observedStats) {
      return {
        totalTrades: observedStats.total_trades,
        wins: observedStats.wins,
        losses: observedStats.losses,
        observedHitRate: observedStats.win_rate_pct,
        longestLossStreak: observedStats.longest_loss_streak,
        maxDrawdownObserved: observedStats.max_drawdown_pct,
        netPnlTotal: observedStats.net_pnl_total,
      };
    }

    // Fallback: compute locally from trades
    if (trades.length === 0) return null;

    let wins = 0;
    let losses = 0;
    let currentStreak = 0;
    let maxLossStreak = 0;
    let balance = 10000;
    let peak = balance;
    let maxDrawdownPct = 0;
    let netPnlTotal = 0;

    for (const trade of trades) {
      const netPnl = parseFloat(trade.net_pnl) || 0;
      netPnlTotal += netPnl;
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
      netPnlTotal,
    };
  }, [trades, observedStats]);

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
              onChange={(e) => {
                const newAccountId = e.target.value;
                setSelectedAccountId(newAccountId);
                // Save to localStorage for persistence
                try {
                  localStorage.setItem(ACCOUNT_STORAGE_KEY, newAccountId);
                } catch {
                  // Ignore storage errors
                }
              }}
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

          {/* Stage filter */}
          <div style={{ marginTop: "1.25rem" }}>
            <span style={{ fontSize: "0.78rem", color: "#9ca3af", marginRight: "0.4rem" }}>Stage:</span>
            {(["ALL", "LIVE", "TEST"] as const).map((s) => (
              <button
                key={s}
                onClick={() => setStageFilter(s)}
                style={{
                  padding: "0.25rem 0.5rem",
                  marginRight: "0.25rem",
                  borderRadius: 5,
                  border: stageFilter === s ? "1px solid #60a5fa" : "1px solid rgba(148,163,184,0.25)",
                  background: stageFilter === s ? "rgba(96,165,250,0.15)" : "transparent",
                  color: stageFilter === s ? "#60a5fa" : "#9ca3af",
                  fontSize: "0.78rem",
                  cursor: "pointer",
                }}
              >
                {s}
              </button>
            ))}
          </div>

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
                <BalanceTrajectoryChart
                  balanceSeries={balanceSeries}
                  mt5BalanceCurrent={mt5BalanceCurrent}
                  currency={currency}
                  height={200}
                />
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
            {/* MT5 Balance - show if available */}
            {mt5BalanceCurrent !== null && (
              <div>
                <div style={{ fontSize: "0.78rem", color: "#9ca3af" }}>
                  {t(lang, "tradeHistory.statMT5Balance")}
                </div>
                <div style={{ fontSize: "1.1rem", color: "#60a5fa", fontWeight: 500 }}>
                  {mt5BalanceCurrent.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} {currency}
                </div>
              </div>
            )}
            {/* Net P&L (observed) */}
            {stats.netPnlTotal !== undefined && (
              <div>
                <div style={{ fontSize: "0.78rem", color: "#9ca3af" }}>
                  {t(lang, "tradeHistory.statNetPnL")}
                </div>
                <div
                  style={{
                    fontSize: "1.1rem",
                    color: stats.netPnlTotal >= 0 ? "#22c55e" : "#ef4444",
                    fontWeight: 500,
                  }}
                >
                  {stats.netPnlTotal >= 0 ? "+" : ""}
                  {stats.netPnlTotal.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} {currency}
                </div>
              </div>
            )}
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

      {/* Daily PnL & Win Rate (Observed) */}
      {hasData && (
        <Card
          title="Daily PnL & Win Rate (Observed)"
          subtitle="Aggregated by UTC close date. Not investment advice."
        >
          {/* Days selector */}
          <div style={{ display: "flex", gap: "0.5rem", marginBottom: "1rem", alignItems: "center" }}>
            <span style={{ fontSize: "0.8rem", color: "#9ca3af" }}>Period:</span>
            {[7, 14, 30, 90].map((d) => (
              <button
                key={d}
                onClick={() => setDailyPnlDays(d)}
                style={{
                  padding: "0.25rem 0.6rem",
                  borderRadius: 6,
                  border: dailyPnlDays === d ? "1px solid #60a5fa" : "1px solid rgba(148,163,184,0.25)",
                  background: dailyPnlDays === d ? "rgba(96,165,250,0.15)" : "transparent",
                  color: dailyPnlDays === d ? "#60a5fa" : "#9ca3af",
                  fontSize: "0.8rem",
                  cursor: "pointer",
                }}
              >
                {d}d
              </button>
            ))}
          </div>

          {loadingDailyPnl && (
            <p style={{ fontSize: "0.85rem", color: "#9ca3af" }}>Loading daily data…</p>
          )}

          {!loadingDailyPnl && dailyPnl && dailyPnl.series.length > 0 && (
            <>
              {/* Totals row */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))",
                  gap: "0.75rem",
                  marginBottom: "1rem",
                  padding: "0.75rem",
                  borderRadius: 8,
                  background: "rgba(15,23,42,0.5)",
                  border: "1px solid rgba(148,163,184,0.15)",
                }}
              >
                <div>
                  <div style={{ fontSize: "0.72rem", color: "#9ca3af" }}>Net PnL</div>
                  <div style={{
                    fontSize: "1rem", fontWeight: 500,
                    color: dailyPnl.totals.net_pnl >= 0 ? "#22c55e" : "#ef4444",
                  }}>
                    {dailyPnl.totals.net_pnl >= 0 ? "+" : ""}{dailyPnl.totals.net_pnl.toFixed(2)} {currency}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: "0.72rem", color: "#9ca3af" }}>Trades</div>
                  <div style={{ fontSize: "1rem", color: "#e5f4ff", fontWeight: 500 }}>{dailyPnl.totals.trades}</div>
                </div>
                <div>
                  <div style={{ fontSize: "0.72rem", color: "#9ca3af" }}>Win Rate</div>
                  <div style={{ fontSize: "1rem", color: "#e5f4ff", fontWeight: 500 }}>{dailyPnl.totals.win_rate.toFixed(1)}%</div>
                </div>
                <div>
                  <div style={{ fontSize: "0.72rem", color: "#9ca3af" }}>W / L</div>
                  <div style={{ fontSize: "1rem", color: "#e5f4ff", fontWeight: 500 }}>
                    <span style={{ color: "#22c55e" }}>{dailyPnl.totals.wins}</span>
                    {" / "}
                    <span style={{ color: "#ef4444" }}>{dailyPnl.totals.losses}</span>
                  </div>
                </div>
              </div>

              {/* Daily table — last 7 rows */}
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse" }}>
                  <thead>
                    <tr>
                      {["Date", "Trades", "W", "L", "Win %", "Net PnL"].map((h) => (
                        <th
                          key={h}
                          style={{
                            textAlign: h === "Net PnL" ? "right" : "left",
                            padding: "0.4rem 0.6rem",
                            borderBottom: "1px solid rgba(255,255,255,0.1)",
                            fontSize: "0.75rem",
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
                    {dailyPnl.series.slice(-7).reverse().map((row) => (
                      <tr key={row.date} style={{ borderBottom: "1px solid rgba(255,255,255,0.05)" }}>
                        <td style={{ padding: "0.4rem 0.6rem", fontSize: "0.82rem", color: "#b7c5dd" }}>
                          {row.date}
                        </td>
                        <td style={{ padding: "0.4rem 0.6rem", fontSize: "0.82rem", color: "#e5f4ff" }}>
                          {row.trades}
                        </td>
                        <td style={{ padding: "0.4rem 0.6rem", fontSize: "0.82rem", color: "#22c55e" }}>
                          {row.wins}
                        </td>
                        <td style={{ padding: "0.4rem 0.6rem", fontSize: "0.82rem", color: "#ef4444" }}>
                          {row.losses}
                        </td>
                        <td style={{ padding: "0.4rem 0.6rem", fontSize: "0.82rem", color: "#e5f4ff" }}>
                          {row.win_rate.toFixed(0)}%
                        </td>
                        <td style={{
                          padding: "0.4rem 0.6rem", fontSize: "0.82rem", textAlign: "right", fontWeight: 500,
                          color: row.net_pnl >= 0 ? "#22c55e" : "#ef4444",
                        }}>
                          {row.net_pnl >= 0 ? "+" : ""}{row.net_pnl.toFixed(2)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {!loadingDailyPnl && (!dailyPnl || dailyPnl.series.length === 0) && (
            <p style={{ fontSize: "0.85rem", color: "#64748b", textAlign: "center", padding: "1rem" }}>
              No daily data for this period.
            </p>
          )}
        </Card>
      )}

      {/* Trades Table (Round-trips) */}
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
                    t(lang, "tradeHistory.colTradeClosed"),
                    t(lang, "tradeHistory.colTradeNumbers"),
                    t(lang, "tradeHistory.colSymbol"),
                    t(lang, "tradeHistory.colDirection"),
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
                  const rt = isRoundTrip(r);

                  // Generate unique row key based on type
                  const rowKey = rt
                    ? `rt-${r.buy_ticket}-${r.sell_ticket}`
                    : `dl-${(r as DealRow).ticket}`;

                  // Format close time for display
                  const tradeClosed = rt
                    ? r.trade_closed || r.close_time || r.open_time
                    : (r as DealRow).close_time || (r as DealRow).open_time;

                  // Format trade numbers
                  const tradeNumbers = rt
                    ? r.trade_numbers || `${r.buy_ticket} → ${r.sell_ticket}`
                    : (r as DealRow).ticket;

                  // Direction: BUY for round-trips, or the actual side for deals
                  const direction = rt ? (r.direction || "BUY") : (r as DealRow).side;

                  return (
                    <tr
                      key={rowKey}
                      style={{
                        borderBottom: "1px solid rgba(255,255,255,0.05)",
                      }}
                    >
                      {/* Trade Closed */}
                      <td style={{ padding: "0.5rem 0.75rem", fontSize: "0.85rem", color: "#b7c5dd" }}>
                        {tradeClosed}
                      </td>
                      {/* Trade Numbers */}
                      <td style={{ padding: "0.5rem 0.75rem", fontSize: "0.85rem", color: "#e5f4ff" }}>
                        {rt ? (
                          <span title={`BUY: ${r.buy_ticket}, SELL: ${r.sell_ticket}`}>
                            <span style={{ color: "#60a5fa" }}>{r.buy_ticket}</span>
                            <span style={{ color: "#64748b", margin: "0 0.25rem" }}>→</span>
                            <span style={{ color: "#94a3b8" }}>{r.sell_ticket}</span>
                          </span>
                        ) : (
                          tradeNumbers
                        )}
                      </td>
                      {/* Symbol */}
                      <td style={{ padding: "0.5rem 0.75rem", fontSize: "0.85rem", color: "#e5f4ff" }}>
                        {r.symbol}
                      </td>
                      {/* Buy/Sell Direction */}
                      <td style={{ padding: "0.5rem 0.75rem" }}>
                        <Badge color={direction === "BUY" ? "blue" : "gray"}>
                          {direction}
                        </Badge>
                      </td>
                      {/* Volume */}
                      <td style={{ padding: "0.5rem 0.75rem", fontSize: "0.85rem", color: "#b7c5dd" }}>
                        {r.volume}
                      </td>
                      {/* Outcome (P&L with currency) */}
                      <td style={{ padding: "0.5rem 0.75rem" }}>
                        <span
                          style={{
                            color: isPositive ? "#22c55e" : "#ef4444",
                            fontWeight: 500,
                            fontSize: "0.85rem",
                          }}
                        >
                          {isPositive ? "+" : ""}
                          {netPnl.toFixed(2)} {currency}
                        </span>
                      </td>
                      {/* Strategy */}
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
