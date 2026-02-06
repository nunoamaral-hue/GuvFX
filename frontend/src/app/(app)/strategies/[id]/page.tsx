"use client";

// Helper types and functions for safe property access (no-explicit-any)
type AnyRecord = Record<string, unknown>;

const asRecord = (v: unknown): AnyRecord => (v && typeof v === "object" ? (v as AnyRecord) : {});

const readString = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);

import type { CSSProperties } from "react";
import { useEffect, useState } from "react";
import { useRouter, useParams } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Alert } from "@/components/ui/Alert";
import type { BacktestConfig, BacktestRun, EquityPoint } from "@/types/backtests";

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
  // NEW fields:
  ma_fast_period: number | null;
  ma_slow_period: number | null;
  ma_type: string | null;
  auto_optimize_by_ai: boolean;
  created_at: string;
  updated_at: string;
};

type StrategyPerformanceSummary = {
  sampleSize: number;
  totalTrades: number;
  avgWinRatePct: number | null;
  avgRMultiple: number | null;
  avgNetProfit: number | null;
  worstDrawdown: number | null;
};

const computePerformanceSummary = (
  runs: BacktestRun[]
): StrategyPerformanceSummary | null => {
  const withMetrics = runs.filter((r) => r.metrics);
  if (withMetrics.length === 0) return null;

  let totalTrades = 0;
  let winRateSum = 0;
  let winRateCount = 0;
  let rrSum = 0;
  let rrCount = 0;
  let netProfitSum = 0;
  let netProfitCount = 0;
  let worstDrawdown: number | null = null;

  for (const run of withMetrics) {
    const m = run.metrics!;
    if (typeof m.total_trades === "number") {
      totalTrades += m.total_trades;
    }
    if (typeof m.win_rate_pct === "number") {
      winRateSum += m.win_rate_pct;
      winRateCount += 1;
    }
    if (typeof m.avg_rr === "number") {
      rrSum += m.avg_rr;
      rrCount += 1;
    }
    if (typeof m.net_profit === "number") {
      netProfitSum += m.net_profit;
      netProfitCount += 1;
    }
    if (typeof m.max_drawdown === "number") {
      if (worstDrawdown === null || m.max_drawdown < worstDrawdown) {
        worstDrawdown = m.max_drawdown;
      }
    }
  }

  return {
    sampleSize: withMetrics.length,
    totalTrades,
    avgWinRatePct: winRateCount > 0 ? winRateSum / winRateCount : null,
    avgRMultiple: rrCount > 0 ? rrSum / rrCount : null,
    avgNetProfit: netProfitCount > 0 ? netProfitSum / netProfitCount : null,
    worstDrawdown,
  };
};

const metricColor = (value: number | null | undefined, badIfPositive = false): string => {
  if (value === null || value === undefined) return "#e5e7eb";
  const v = Number(value);
  if (Number.isNaN(v)) return "#e5e7eb";
  const score = badIfPositive ? -v : v;
  if (score > 0) return "#4ade80";
  if (score < 0) return "#f97373";
  return "#e5e7eb";
};

const getLatestRunWithEquity = (runs: BacktestRun[]): BacktestRun | null => {
  for (const run of runs) {
    const curve = run.metrics?.equity_curve;
    if (Array.isArray(curve) && curve.length > 0) {
      return run;
    }
  }
  return null;
};

type EquitySparklineProps = {
  points: number[];
  width?: number;
  height?: number;
};

const EquitySparkline = ({
  points,
  width = 140,
  height = 40,
}: EquitySparklineProps) => {
  if (!points || points.length === 0) {
    return (
      <div style={{ fontSize: "0.8rem", color: "#9ca3af" }}>No equity data</div>
    );
  }

  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const stepX = points.length > 1 ? width / (points.length - 1) : 0;

  const coords = points
    .map((value, index) => {
      const x = stepX * index;
      const y = height - ((value - min) / range) * height;
      return `${x},${y}`;
    })
    .join(" ");

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
    >
      <polyline
        fill="none"
        stroke="#38bdf8"
        strokeWidth="2"
        points={coords}
      />
    </svg>
  );
};

type StrategyInsights = {
  strategy_id: number;
  summary: string;
  recommendations: string[];
  risk_assessment?: string;
  notes?: string;
};

type TradingAccount = {
  id: number;
  name: string;
  broker_name: string;
  account_number: string;
  is_demo: boolean;
  is_active: boolean;
  created_at: string;
  updated_at: string;
};

type StrategyAssignment = {
  id: number;
  strategy: number;
  strategy_name: string;
  account: number;
  account_name: string;
  broker_name: string;
  is_active: boolean;
  risk_per_trade_override_pct: number | string | null;
  created_at: string;
};

type StrategyChangeLog = {
  id: number;
  source: string;
  changed_by_email: string | null;
  created_at: string;
  before_settings: Record<string, unknown> | null;
  after_settings: Record<string, unknown> | null;
};

type StrategyHasTrades = {
  strategy_id: number;
  strategy_name: string;
  magic_number: number | null;
  canonical_id: number;
  has_trades: boolean;
  trade_count: number;
};

// Execution jobs for this strategy (MT5 actions)

type StrategyExecutionJob = {
  id: number;
  job_type: string;
  status: string;
  account: number;
  strategy: number | null;
  created_at: string;
  result?: Record<string, unknown>;
  payload?: Record<string, unknown>;
};

// Helper to extract a user-friendly error message from API errors (esp. DRF/duplicate magic_number)
const formatApiErrorMessage = (err: unknown): string => {
  // apiFetch may throw Error(message), or something else.
  if (err instanceof Error) {
    const msg = err.message || "";

    // If the message looks like JSON, try to extract field errors.
    const trimmed = msg.trim();
    if ((trimmed.startsWith("{") && trimmed.endsWith("}")) || (trimmed.startsWith("[") && trimmed.endsWith("]"))) {
      try {
        const parsed = JSON.parse(trimmed) as unknown;
        // DRF typical: { field: ["..."] } or { detail: "..." }
        if (parsed && typeof parsed === "object") {
          const obj = parsed as Record<string, unknown>;

          if (typeof obj.detail === "string") return obj.detail;

          const mn = obj.magic_number;
          if (mn !== undefined) {
            if (Array.isArray(mn) && typeof mn[0] === "string") return mn[0];
            if (typeof mn === "string") return mn;
          }

          // Fallback: stringify first field
          for (const k of Object.keys(obj)) {
            const v = obj[k];
            if (Array.isArray(v) && typeof v[0] === "string") return `${k}: ${v[0]}`;
            if (typeof v === "string") return `${k}: ${v}`;
          }
        }
      } catch {
        // ignore JSON parse failures
      }
    }

    // Common backend text cases
    if (msg.toLowerCase().includes("magic number") && msg.toLowerCase().includes("already")) {
      return msg;
    }

    return msg || "Request failed.";
  }

  return "Request failed.";
};

export default function StrategyDetailPage() {
  const params = useParams();
  const strategyId = Number(params?.id);
  const router = useRouter();

  const [strategy, setStrategy] = useState<Strategy | null>(null);
  const [accessToken, setAccessToken] = useState<string>("");
  const [insights, setInsights] = useState<StrategyInsights | null>(null);
  const [insightsLoading, setInsightsLoading] = useState(false);
  const [insightsError, setInsightsError] = useState<string | null>(null);
  const [recentRuns, setRecentRuns] = useState<BacktestRun[]>([]);
  const [recentRunsLoading, setRecentRunsLoading] = useState(false);
  const [recentRunsError, setRecentRunsError] = useState<string | null>(null);
  const [perfSummary, setPerfSummary] =
    useState<StrategyPerformanceSummary | null>(null);
  const [latestEquityRun, setLatestEquityRun] = useState<BacktestRun | null>(
    null
  );

  const latestEquityPoints: number[] = (() => {
    const curve = latestEquityRun?.metrics?.equity_curve;
    if (!Array.isArray(curve) || curve.length === 0) {
      return [];
    }
    return curve.map((point: number | EquityPoint) => {
      if (typeof point === "number") return point;
      if (point && typeof point === "object" && typeof point.equity === "number") {
        return point.equity;
      }
      return 0;
    });
  })();

  const [btSymbol, setBtSymbol] = useState("");
  const [btTimeframe, setBtTimeframe] = useState("");
  const [btDateFrom, setBtDateFrom] = useState("");
  const [btDateTo, setBtDateTo] = useState("");
  const [btInitialBalance, setBtInitialBalance] = useState("10000");
  const [btLaunching, setBtLaunching] = useState(false);
  const [btError, setBtError] = useState<string | null>(null);

  const [loadingStrategy, setLoadingStrategy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // accounts + assignments
  const [accounts, setAccounts] = useState<TradingAccount[]>([]);
  const [assignments, setAssignments] = useState<StrategyAssignment[]>([]);
  const [loadingLinks, setLoadingLinks] = useState(false);
  const [linkMessage, setLinkMessage] = useState<string | null>(null);
  const [selectedAccountId, setSelectedAccountId] = useState<number | "">("");
  const [linking, setLinking] = useState(false);
  const [removingId, setRemovingId] = useState<number | null>(null);
  const [overrideDrafts, setOverrideDrafts] = useState<Record<number, string>>({});
  const [savingOverrideId, setSavingOverrideId] = useState<number | null>(null);
  const [togglingId, setTogglingId] = useState<number | null>(null); // NEW
  const [testTradeLoadingId, setTestTradeLoadingId] = useState<number | null>(null);

  const [changeLogs, setChangeLogs] = useState<StrategyChangeLog[]>([]);
  const [loadingLogs, setLoadingLogs] = useState(false);
  const [hasTradesInfo, setHasTradesInfo] = useState<StrategyHasTrades | null>(null);

  // Execution jobs state
  const [execJobs, setExecJobs] = useState<StrategyExecutionJob[]>([]);
  const [execJobsLoading, setExecJobsLoading] = useState(false);
  const [execJobsError, setExecJobsError] = useState<string | null>(null);
  useEffect(() => {
    if (!accessToken || !strategy) return;

    const fetchExecJobs = async () => {
      setExecJobsLoading(true);
      setExecJobsError(null);
      try {
        const jobs = await apiFetch<StrategyExecutionJob[]>(
          `/api/execution/jobs/?strategy=${strategy.id}`,
          {});

        const sorted = [...jobs].sort((a, b) => {
          const aTime = Date.parse(a.created_at);
          const bTime = Date.parse(b.created_at);
          return bTime - aTime;
        });

        setExecJobs(sorted.slice(0, 10));
      } catch (err: unknown) {
        console.error(err);
        const msg =
          err instanceof Error
            ? err.message
            : "Failed to load execution jobs for this strategy.";
        setExecJobsError(msg);
      } finally {
        setExecJobsLoading(false);
      }
    };

    fetchExecJobs();
  }, [accessToken, strategy]);

  // editable settings
  // Fetch change logs
  useEffect(() => {
    if (!accessToken || !strategyId) return;

    const fetchLogs = async () => {
      setLoadingLogs(true);
      try {
        const logs = await apiFetch<StrategyChangeLog[]>(
          `/api/strategies/changes/?strategy=${strategyId}`,
          {});
        setChangeLogs(logs);
      } catch (err) {
        console.error("Failed to fetch change logs:", err);
        // history is nice-to-have; we keep this silent
      } finally {
        setLoadingLogs(false);
      }
    };

    fetchLogs();
  }, [accessToken, strategyId]);
  // Fetch whether this strategy already has attributed trades (locks magic number)
  useEffect(() => {
    if (!accessToken || !strategyId) return;

    const fetchHasTrades = async () => {
      try {
        const info = await apiFetch<StrategyHasTrades>(
          `/api/analytics/strategy-has-trades/?strategy=${strategyId}`,
          {}
        );
        setHasTradesInfo(info);
      } catch (err) {
        console.error("Failed to fetch strategy has-trades:", err);
        setHasTradesInfo(null);
      }
    };

    fetchHasTrades();
  }, [accessToken, strategyId]);
  const [tfEdit, setTfEdit] = useState("");
  const [symbolsEdit, setSymbolsEdit] = useState("");
  const [magicEdit, setMagicEdit] = useState<string>("");
  const [maFastEdit, setMaFastEdit] = useState<string>("");
  const [maSlowEdit, setMaSlowEdit] = useState<string>("");
  const [maTypeEdit, setMaTypeEdit] = useState<string>("");
  const [autoAiEdit, setAutoAiEdit] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [settingsMessage, setSettingsMessage] = useState<string | null>(null);
  const [autoTuneLoading, setAutoTuneLoading] = useState(false);

  const labelStyle: CSSProperties = {
    color: "#8fa0b7",
    fontSize: "0.88rem",
    marginRight: 4,
  };

  const valueStyle: CSSProperties = {
    color: "#e9f4ff",
    fontSize: "0.9rem",
  };

  // NEW: keep local text fields for per-account risk overrides in sync with data
  const syncAssignmentOverrides = (list: StrategyAssignment[]) => {
    const next: Record<number, string> = {};
    for (const a of list) {
      if (
        a.risk_per_trade_override_pct !== null &&
        a.risk_per_trade_override_pct !== undefined &&
        a.risk_per_trade_override_pct !== ""
      ) {
        next[a.id] = String(a.risk_per_trade_override_pct);
      } else {
        next[a.id] = "";
      }
    }
    setOverrideDrafts(next);
  };

  const handleOverrideChange = (id: number, value: string) => {
    setOverrideDrafts((prev) => ({
      ...prev,
      [id]: value,
    }));
  };

  // Load token from localStorage
  useEffect(() => {
    if (typeof window !== "undefined") {
      const stored = window.localStorage.getItem("guvfx_access_token");
      if (stored) {
        setAccessToken(stored);
      }
    }
  }, []);

  useEffect(() => {
    setInsights(null);
    setInsightsError(null);
  }, [strategyId]);

  // Fetch strategy details
  useEffect(() => {
    if (!strategyId || !accessToken) return;

    const fetchStrategy = async () => {
      setLoadingStrategy(true);
      setError(null);
      try {
        const data = await apiFetch<Strategy>(
          `/api/strategies/strategies/${strategyId}/`,
          {});
        setStrategy(data);

        // initialise edit fields
        setTfEdit(data.timeframe || "");
        setSymbolsEdit(data.symbol_universe || "");
        setMagicEdit(data.magic_number != null ? String(data.magic_number) : "");
        setMaFastEdit(
          typeof data.ma_fast_period === "number"
            ? String(data.ma_fast_period)
            : ""
        );
        setMaSlowEdit(
          typeof data.ma_slow_period === "number"
            ? String(data.ma_slow_period)
            : ""
        );
        setMaTypeEdit(data.ma_type || "");
        setAutoAiEdit(data.auto_optimize_by_ai);
      } catch (err: unknown) {
        console.error(err);
        setError(
          err instanceof Error ? err.message : "Failed to load strategy."
        );
      } finally {
        setLoadingStrategy(false);
      }
    };

    fetchStrategy();
  }, [strategyId, accessToken]);

  useEffect(() => {
    if (!strategy) return;
    const symbols = (strategy.symbol_universe || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    setBtSymbol(symbols[0] || "");
    setBtTimeframe(strategy.timeframe || "");
    const now = new Date();
    const end = now.toISOString().slice(0, 10);
    const startDate = new Date(now);
    startDate.setFullYear(startDate.getFullYear() - 1);
    const start = startDate.toISOString().slice(0, 10);
    setBtDateFrom(start);
    setBtDateTo(end);
    setBtInitialBalance("10000");
    setMagicEdit(strategy.magic_number != null ? String(strategy.magic_number) : "");
  }, [strategy]);

  // Fetch accounts + assignments
  useEffect(() => {
    if (!accessToken || !strategyId) return;

    const fetchLinks = async () => {
      setLoadingLinks(true);
      setLinkMessage(null);
      try {
        const [accs, assigns] = await Promise.all([
          apiFetch<TradingAccount[]>("/api/trading/accounts/", {}),
          apiFetch<StrategyAssignment[]>(
            "/api/strategies/assignments/",
            {}),
        ]);

        setAccounts(accs);
        const filtered = assigns.filter(
          (a) => a.strategy === strategyId
        );
        setAssignments(filtered);
        syncAssignmentOverrides(filtered);
      } catch (err: unknown) {
        console.error(err);
        setError((prev) => prev ?? "Failed to load linked accounts.");
      } finally {
        setLoadingLinks(false);
      }
    };

    fetchLinks();
  }, [accessToken, strategyId]);

  useEffect(() => {
    if (!accessToken || !strategy) return;

    const fetchRecentRuns = async () => {
      setRecentRunsLoading(true);
      setRecentRunsError(null);
      try {
        const runs = await apiFetch<BacktestRun[]>(
          `/api/backtests/runs/?strategy=${strategy.id}`,
          {});
        const sorted = [...runs].sort((a, b) => {
          const aTime = a.started_at ? Date.parse(a.started_at) : 0;
          const bTime = b.started_at ? Date.parse(b.started_at) : 0;
          return bTime - aTime;
        });
        const latest = sorted.slice(0, 5);
        setRecentRuns(latest);
        setPerfSummary(computePerformanceSummary(latest));
        setLatestEquityRun(getLatestRunWithEquity(latest));
      } catch (err: unknown) {
        console.error(err);
        const message =
          err instanceof Error
            ? err.message
            : "Failed to load recent backtests.";
        setRecentRunsError(message);
        setPerfSummary(null);
        setLatestEquityRun(null);
      } finally {
        setRecentRunsLoading(false);
      }
    };

    fetchRecentRuns();
  }, [accessToken, strategy]);

  const handleFetchInsights = async () => {
    if (!accessToken || !strategy) {
      setInsightsError("No strategy or token available.");
      return;
    }

    setInsightsLoading(true);
    setInsightsError(null);
    try {
      const payload = { strategy_id: strategy.id, max_runs: 5 };
      const data = await apiFetch<StrategyInsights>(
        "/api/ai/strategy-insights/",
        {
          method: "POST",
          body: JSON.stringify(payload),
        }
);
      setInsights(data);
    } catch (err: unknown) {
      console.error(err);
      const message =
        err instanceof Error ? err.message : "Failed to fetch AI insights.";
      setInsightsError(message);
    } finally {
      setInsightsLoading(false);
    }
  };

  const handleLaunchBacktest = async (e: React.FormEvent) => {
    e.preventDefault();
    setBtError(null);
    if (!accessToken || !strategy) {
      setBtError("No strategy or token available.");
      return;
    }
    if (!btSymbol || !btTimeframe || !btDateFrom || !btDateTo) {
      setBtError("Please fill symbol, timeframe, and date range.");
      return;
    }

    setBtLaunching(true);
    try {
      const configPayload: Partial<BacktestConfig> = {
        name: `${btTimeframe} ${btSymbol} ${btDateFrom}–${btDateTo}`,
        strategy: strategy.id,
        symbol: btSymbol,
        timeframe: btTimeframe,
        date_from: btDateFrom,
        date_to: btDateTo,
        initial_balance: Number(btInitialBalance) || 10000,
      };

      const config = await apiFetch<BacktestConfig>(
        "/api/backtests/configs/",
        {
          method: "POST",
          body: JSON.stringify(configPayload),
        }
);

      const run = await apiFetch<BacktestRun>(
        "/api/backtests/runs/",
        {
          method: "POST",
          body: JSON.stringify({ config: config.id }),
        }
);

      router.push(`/backtests/${run.id}`);
    } catch (err: unknown) {
      console.error(err);
      const msg =
        err instanceof Error ? err.message : "Failed to launch backtest.";
      setBtError(msg);
    } finally {
      setBtLaunching(false);
    }
  };

  const handleLinkAccount = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!accessToken) {
      setError("");
      return;
    }
    if (!selectedAccountId) {
      setLinkMessage("Please select an account to link.");
      return;
    }

    setLinking(true);
    setLinkMessage(null);
    try {
      const body = {
        strategy: strategyId,
        account: selectedAccountId,
        is_active: true,
      };

      await apiFetch<StrategyAssignment>(
        "/api/strategies/assignments/",
        {
          method: "POST",
          body: JSON.stringify(body),
        }
);

      setLinkMessage("Account linked to strategy.");
      setSelectedAccountId("");

      // refresh assignments
      const assigns = await apiFetch<StrategyAssignment[]>(
        "/api/strategies/assignments/",
        {});
      const filtered = assigns.filter(
        (a) => a.strategy === strategyId
      );
      setAssignments(filtered);
      syncAssignmentOverrides(filtered);
    } catch (err: unknown) {
      console.error(err);
      setError(
        err instanceof Error ? err.message : "Failed to link account."
      );
    } finally {
      setLinking(false);
    }
  };

  const handleRemoveLink = async (assignmentId: number) => {
    
    setRemovingId(assignmentId);
    setLinkMessage(null);

    try {
      await apiFetch<void>(
        `/api/strategies/assignments/${assignmentId}/`,
        { method: "DELETE" });

      setAssignments((prev) => {
        const updated = prev.filter((a) => a.id !== assignmentId);
        setOverrideDrafts((drafts) => {
          const copy = { ...drafts };
          delete copy[assignmentId];
          return copy;
        });
        return updated;
      });
      setLinkMessage("Link removed.");
    } catch (err: unknown) {
      console.error(err);
      setError(
        err instanceof Error ? err.message : "Failed to remove link."
      );
    } finally {
      setRemovingId(null);
    }
  };

  // NEW: save per-account risk override for a specific assignment
  const handleSaveOverride = async (assignmentId: number) => {
    if (!accessToken) {
      setError("");
      return;
    }

    const raw = overrideDrafts[assignmentId] ?? "";
    const trimmed = raw.trim();

    let value: number | null = null;
    if (trimmed !== "") {
      const parsed = Number(trimmed);
      if (Number.isNaN(parsed) || parsed < 0 || parsed > 100) {
        setError("Enter a valid risk percentage between 0 and 100.");
        return;
      }
      value = parsed;
    }

    setError(null);
    setLinkMessage(null);
    setSavingOverrideId(assignmentId);

    try {
      const body = { risk_per_trade_override_pct: value };
      const updated = await apiFetch<StrategyAssignment>(
        `/api/strategies/assignments/${assignmentId}/`,
        {
          method: "PATCH",
          body: JSON.stringify(body),
        }
);

      setAssignments((prev) => {
        const next = prev.map((a) => (a.id === assignmentId ? updated : a));
        syncAssignmentOverrides(next);
        return next;
      });

      setLinkMessage("Risk override updated.");
    } catch (err: unknown) {
      console.error(err);
      setError(
        err instanceof Error ? err.message : "Failed to update risk override."
      );
    } finally {
      setSavingOverrideId(null);
    }
  };

  // NEW: enable/disable a link without deleting it
  const handleToggleAssignmentActive = async (
    assignmentId: number,
    currentActive: boolean
  ) => {
    if (!accessToken) {
      setError("");
      return;
    }

    setTogglingId(assignmentId);
    setError(null);
    setLinkMessage(null);

    try {
      const body = { is_active: !currentActive };
      const updated = await apiFetch<StrategyAssignment>(
        `/api/strategies/assignments/${assignmentId}/`,
        {
          method: "PATCH",
          body: JSON.stringify(body),
        }
);

      setAssignments((prev) => {
        const next = prev.map((a) => (a.id === assignmentId ? updated : a));
        syncAssignmentOverrides(next);
        return next;
      });

      setLinkMessage(!currentActive ? "Link enabled." : "Link paused.");
    } catch (err: unknown) {
      console.error(err);
      setError(
        err instanceof Error
          ? err.message
          : "Failed to update link status."
      );
    } finally {
      setTogglingId(null);
    }
  };

  // NEW: queue a dev/test OPEN_TRADE job for a specific assignment
  const handleOpenTestTrade = async (assignment: StrategyAssignment) => {
    if (!accessToken) {
      setError("");
      return;
    }
    if (!strategy) {
      setError("Strategy not loaded yet.");
      return;
    }

    setTestTradeLoadingId(assignment.id);
    setError(null);
    setLinkMessage(null);

    try {
      // Derive a symbol from the strategy universe (fall back to EURUSD)
      const symbols = (strategy.symbol_universe || "")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const symbol = symbols[0] || "EURUSD";

      // Use strategy timeframe or fall back to H1
      const timeframe = strategy.timeframe || "H1";

      // Use the most specific risk value if provided; otherwise let backend fall back
      const rawOverride = assignment.risk_per_trade_override_pct;
      const riskPct =
        rawOverride !== null && rawOverride !== "" && rawOverride !== undefined
          ? Number(rawOverride)
          : strategy.risk_per_trade_pct != null
          ? Number(strategy.risk_per_trade_pct)
          : undefined;

      const body: Record<string, unknown> = {
        account: assignment.account,
        strategy: strategy.id,
        symbol,
        direction: "BUY", // dev default
        timeframe,
        entry_type: "MARKET",
        sl_price: 0, // dev dummy; worker is in dummy mode
        tp_price: null,
        // Strategy attribution for MT5 deal comments (read by analytics ingestion)
        // Prefer magic_number if set, else fall back to strategy.id
        comment: `guvfx:sid=${strategy.magic_number ?? strategy.id};name=${strategy.name}`,
      };

      if (riskPct !== undefined && !Number.isNaN(riskPct)) {
        body.risk_per_trade_pct = riskPct;
      }

      await apiFetch<unknown>(
        "/api/execution/open-trade/",
        {
          method: "POST",
          body: JSON.stringify(body),
        }
);

      setLinkMessage(
        `Test trade job queued for ${symbol} on account #${assignment.account}.`
      );

      // Optional: refresh execution jobs for this strategy so the new job appears
      try {
        const jobs = await apiFetch<StrategyExecutionJob[]>(
          `/api/execution/jobs/?strategy=${strategy.id}`,
          {});
        const sorted = [...jobs].sort((a, b) => {
          const aTime = Date.parse(a.created_at);
          const bTime = Date.parse(b.created_at);
          return bTime - aTime;
        });
        setExecJobs(sorted.slice(0, 10));
      } catch (refreshErr) {
        console.error(
          "Failed to refresh execution jobs after test trade:",
          refreshErr
        );
        // non-fatal – polling/next reload will catch up
      }
    } catch (err: unknown) {
      console.error(err);
      setError(
        err instanceof Error ? err.message : "Failed to queue test trade job."
      );
    } finally {
      setTestTradeLoadingId(null);
    }
  };

  const handleSaveSettings = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!accessToken) {
      setError("");
      return;
    }

    setSavingSettings(true);
    setSettingsMessage(null);
    setError(null);

    if (hasTradesInfo?.has_trades) {
      const currentMagic =
        strategy?.magic_number != null ? String(strategy.magic_number) : "";
      if ((magicEdit || "").trim() !== currentMagic.trim()) {
        throw new Error(
          "Magic number is locked because trades already exist for this strategy."
        );
      }
    }

    try {
      const body: Partial<Strategy> = {
        timeframe: tfEdit,
        symbol_universe: symbolsEdit,
        magic_number: null,
        auto_optimize_by_ai: autoAiEdit,
      };

      // Magic number: optional integer
      const magicTrim = magicEdit.trim();
      if (magicTrim === "") {
        body.magic_number = null;
      } else {
        const magicParsed = Number(magicTrim);
        if (!Number.isInteger(magicParsed) || magicParsed < 0) {
          throw new Error("Magic number must be a non-negative integer (or blank). ");
        }
        body.magic_number = magicParsed;
      }

      if (maFastEdit) body.ma_fast_period = Number(maFastEdit);
      else body.ma_fast_period = null;

      if (maSlowEdit) body.ma_slow_period = Number(maSlowEdit);
      else body.ma_slow_period = null;

      if (maTypeEdit) body.ma_type = maTypeEdit;
      else body.ma_type = "";

      const updated = await apiFetch<Strategy>(
        `/api/strategies/strategies/${strategyId}/`,
        {
          method: "PATCH",
          body: JSON.stringify(body),
        }
);

      setStrategy(updated);
      setSettingsMessage("Strategy settings updated.");
      try {
        const info = await apiFetch<StrategyHasTrades>(
          `/api/analytics/strategy-has-trades/?strategy=${strategyId}`,
          {}
        );
        setHasTradesInfo(info);
      } catch {
        // ignore
      }
    } catch (err: unknown) {
      console.error(err);
      setError(formatApiErrorMessage(err) || "Failed to update strategy settings.");
    } finally {
      setSavingSettings(false);
    }
  };

  const handleAutoTune = async () => {
    if (!accessToken) {
      setError("");
      return;
    }

    setAutoTuneLoading(true);
    setError(null);
    setSettingsMessage(null);

    try {
      const data = await apiFetch<{
        applied_settings: {
          timeframe?: string;
          symbol_universe?: string;
          ma_fast_period?: number;
          ma_slow_period?: number;
          ma_type?: string;
        } | null;
      }>(
        `/api/strategies/strategies/${strategyId}/auto-tune/`,
        { method: "POST" });

      // Always refresh the strategy from backend to reflect any changes
      const updated = await apiFetch<Strategy>(
        `/api/strategies/strategies/${strategyId}/`,
        {});
      setStrategy(updated);

      // Sync editable fields with updated strategy
      setTfEdit(updated.timeframe || "");
      setSymbolsEdit(updated.symbol_universe || "");
      setMagicEdit(updated.magic_number != null ? String(updated.magic_number) : "");
      setMaFastEdit(
        typeof updated.ma_fast_period === "number"
          ? String(updated.ma_fast_period)
          : ""
      );
      setMaSlowEdit(
        typeof updated.ma_slow_period === "number"
          ? String(updated.ma_slow_period)
          : ""
      );
      setMaTypeEdit(updated.ma_type || "");
      setAutoAiEdit(updated.auto_optimize_by_ai);

      if (data.applied_settings) {
        setSettingsMessage(
          "Auto-tune complete. AI recommendations have been applied to this strategy."
        );
      } else {
        setSettingsMessage(
          "Auto-tune completed. No changes were applied (auto optimization may be disabled)."
        );
      }
    } catch (err: unknown) {
      console.error(err);
      setError(
        err instanceof Error ? err.message : "Failed to auto-tune strategy."
      );
    } finally {
      setAutoTuneLoading(false);
    }
  };

  // Guard for invalid strategyId (must come after all hooks)
  if (Number.isNaN(strategyId)) {
    return (
        <div style={{ maxWidth: 900, margin: "0 auto" }}>
          <Alert type="error">Invalid strategy ID.</Alert>
        </div>
    );
  }

  return (
      <div style={{ maxWidth: 900, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>
          Strategy Detail
        </h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
          Inspect your strategy, edit its parameters, link it to accounts, and
          request AI-powered suggestions.
        </p>

        {error && <Alert type="error">{error}</Alert>}
        {linkMessage && <Alert type="info">{linkMessage}</Alert>}
        {settingsMessage && <Alert type="info">{settingsMessage}</Alert>}

        {/* Strategy overview */}
        <Card title={strategy ? strategy.name : `Strategy #${strategyId}`}>
          {!accessToken && (
            <p style={{ fontStyle: "italic", fontSize: "0.9rem" }}>
              
            </p>
          )}

          {loadingStrategy && <p>Loading strategy...</p>}

          {strategy && (
            <div style={{ fontSize: "0.95rem" }}>
              {/* Header row: name + status badge */}
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: "0.7rem",
                }}
              >
                <h2
                  style={{
                    fontSize: "1.4rem",
                    margin: 0,
                    color: "#f1f5ff",
                  }}
                >
                  {strategy.name}
                </h2>
                <Badge color={strategy.is_active ? "green" : "gray"}>
                  {strategy.is_active ? "Active" : "Inactive"}
                </Badge>
              </div>

              {/* Description */}
              <p style={{ marginBottom: "0.6rem" }}>
                <span style={labelStyle}>Description:</span>
                <span style={valueStyle}>
                  {strategy.description || (
                    <span style={{ color: "#7c8ca4" }}>No description</span>
                  )}
                </span>
              </p>

              {/* Key info grid */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
                  gap: "0.4rem 1.5rem",
                }}
              >
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Style:</span>
                  <span style={valueStyle}>{strategy.style || "—"}</span>
                </p>
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Symbols:</span>
                  <span style={valueStyle}>
                    {strategy.symbol_universe || "—"}
                  </span>
                </p>
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Timeframe:</span>
                  <span style={valueStyle}>{strategy.timeframe || "—"}</span>
                </p>
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Risk per trade (%):</span>
                  <span style={valueStyle}>
                    {strategy.risk_per_trade_pct ?? "—"}
                  </span>
                </p>
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Magic number:</span>
                  <span style={valueStyle}>
                    {strategy.magic_number ?? "—"}
                  </span>
                </p>
              </div>

              {/* Timestamps */}
              <p
                style={{
                  fontSize: "0.78rem",
                  color: "#7c8ca4",
                  marginTop: "0.6rem",
                }}
              >
                Created:{" "}
                <span style={{ color: "#c9def7" }}>
                  {new Date(strategy.created_at).toLocaleString()}
                </span>
              </p>
            </div>
          )}
        </Card>

        <Card
          title="Performance summary"
          subtitle="Based on the most recent backtests for this strategy"
        >
          {recentRunsLoading && (
            <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
              Loading performance…
            </p>
          )}

          {!recentRunsLoading && !perfSummary && (
            <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
              No metrics available yet. Run a backtest to see performance.
            </p>
          )}

          {!recentRunsLoading && perfSummary && (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
                gap: "0.75rem 1.25rem",
              }}
            >
              <div>
                <div
                  style={{
                    fontSize: "0.8rem",
                    color: "#9ca3af",
                  }}
                  title="Number of backtest runs included in this summary"
                >
                  Sample
                </div>
                <div
                  style={{
                    fontSize: "1rem",
                    color: "#e5f4ff",
                    fontWeight: 500,
                  }}
                >
                  {perfSummary.sampleSize} runs
                </div>
                <div style={{ fontSize: "0.8rem", color: "#9ca3af" }}>
                  {perfSummary.totalTrades} trades total
                </div>
              </div>

              <div>
                <div
                  style={{ fontSize: "0.8rem", color: "#9ca3af" }}
                  title="Average observed hit rate across the included test runs"
                >
                  Observed hit rate
                </div>
                <div
                  style={{
                    fontSize: "1rem",
                    fontWeight: 500,
                    color: metricColor(perfSummary.avgWinRatePct),
                  }}
                >
                  {perfSummary.avgWinRatePct != null
                    ? `${perfSummary.avgWinRatePct.toFixed(1)}%`
                    : "—"}
                </div>
                <div style={{ fontSize: "0.8rem", color: "#9ca3af" }}>
                  Average across runs
                </div>
              </div>

              <div>
                <div
                  style={{ fontSize: "0.8rem", color: "#9ca3af" }}
                  title="Average R-multiple (profit divided by initial risk) across all trades"
                >
                  Avg R
                </div>
                <div
                  style={{
                    fontSize: "1rem",
                    fontWeight: 500,
                    color: metricColor(perfSummary.avgRMultiple),
                  }}
                >
                  {perfSummary.avgRMultiple != null
                    ? `${perfSummary.avgRMultiple.toFixed(2)} R`
                    : "—"}
                </div>
                <div style={{ fontSize: "0.8rem", color: "#9ca3af" }}>
                  Average R-multiple per trade
                </div>
              </div>

              <div>
                <div
                  style={{ fontSize: "0.8rem", color: "#9ca3af" }}
                  title="Average net profit or loss per backtest run"
                >
                  Net P&amp;L
                </div>
                <div
                  style={{
                    fontSize: "1rem",
                    fontWeight: 500,
                    color: metricColor(perfSummary.avgNetProfit),
                  }}
                >
                  {perfSummary.avgNetProfit != null
                    ? `${
                        perfSummary.avgNetProfit >= 0 ? "+" : ""
                      }${perfSummary.avgNetProfit.toFixed(2)}`
                    : "—"}
                </div>
                <div style={{ fontSize: "0.8rem", color: "#9ca3af" }}>
                  Average per run
                </div>
              </div>

              <div>
                <div
                  style={{ fontSize: "0.8rem", color: "#9ca3af" }}
                  title="Worst maximum drawdown observed across any included backtest"
                >
                  Worst drawdown
                </div>
                <div
                  style={{
                    fontSize: "1rem",
                    fontWeight: 500,
                    color: metricColor(perfSummary.worstDrawdown, true),
                  }}
                >
                  {perfSummary.worstDrawdown != null
                    ? `${perfSummary.worstDrawdown.toFixed(2)}`
                    : "—"}
                </div>
                <div style={{ fontSize: "0.8rem", color: "#9ca3af" }}>
                  Worst (max) drawdown across runs
                </div>
              </div>

              <div>
                <div
                  style={{ fontSize: "0.8rem", color: "#9ca3af" }}
                  title={
                    latestEquityRun
                      ? `Equity curve from the most recent backtest run (#${latestEquityRun.id})`
                      : "Equity curve for the most recent backtest run"
                  }
                >
                  Latest equity curve
                </div>
                <div
                  style={{
                    marginTop: "0.25rem",
                    padding: "0.25rem 0.5rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.4)",
                    background: "rgba(15,23,42,0.75)",
                  }}
                >
                  <EquitySparkline points={latestEquityPoints} />
                </div>
                <div
                  style={{
                    fontSize: "0.75rem",
                    color: "#6b7280",
                    marginTop: "0.25rem",
                  }}
                >
                  Latest run only
                </div>
              </div>
            </div>
          )}
        </Card>

        {/* Editable settings */}
        <Card
          title="Strategy Settings"
          subtitle="Manually adjust core parameters or allow AI to optimize them for you."
        >
          <form onSubmit={handleSaveSettings}>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
                gap: "0.75rem 1.5rem",
              }}
            >
              <div>
                <label
                  htmlFor="tf-edit"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Timeframe
                </label>
                <input
                  id="tf-edit"
                  type="text"
                  value={tfEdit}
                  onChange={(e) => setTfEdit(e.target.value)}
                  placeholder="e.g. H4"
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                  disabled={autoAiEdit}
                />
              </div>

              <div>
                <label
                  htmlFor="symbols-edit"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Symbols (comma-separated)
                </label>
                <input
                  id="symbols-edit"
                  type="text"
                  value={symbolsEdit}
                  onChange={(e) => setSymbolsEdit(e.target.value)}
                  placeholder="e.g. EURUSD,GBPUSD"
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                  disabled={autoAiEdit}
                />
              </div>

              <div>
                <label
                  htmlFor="magic-edit"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Magic number (optional)
                </label>
                <input
                  id="magic-edit"
                  type="number"
                  min={0}
                  step={1}
                  value={magicEdit}
                  onChange={(e) => setMagicEdit(e.target.value)}
                  placeholder="e.g. 12345"
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                  disabled={Boolean(hasTradesInfo?.has_trades)}
                />
                <p style={{ fontSize: "0.78rem", color: "#7c8ca4", marginTop: "0.25rem" }}>
                  Used for MT5 trade attribution. Set this to match the EA magic number for this strategy.
                </p>
                {hasTradesInfo?.has_trades && (
                  <div style={{ marginTop: "0.35rem" }}>
                    <Badge color="gray">Magic number locked</Badge>
                    <div
                      style={{
                        fontSize: "0.78rem",
                        color: "#9ca3af",
                        marginTop: "0.25rem",
                      }}
                    >
                      This strategy already has {hasTradesInfo.trade_count} attributed trade(s). To keep analytics consistent, the magic number cannot be changed.
                    </div>
                  </div>
                )}
                <p style={{ fontSize: "0.78rem", color: "#7c8ca4", marginTop: "0.25rem" }}>
                  Attribution tag: <span style={{ color: "#cbd5f5" }}>guvfx:sid={magicEdit.trim() || "&lt;magic&gt;"};name={strategy?.name || "&lt;strategy&gt;"}</span>
                </p>
              </div>

              <div>
                <label
                  htmlFor="ma-fast-edit"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Fast MA period
                </label>
                <input
                  id="ma-fast-edit"
                  type="number"
                  min={1}
                  value={maFastEdit}
                  onChange={(e) => setMaFastEdit(e.target.value)}
                  placeholder="e.g. 20"
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                  disabled={autoAiEdit}
                />
              </div>

              <div>
                <label
                  htmlFor="ma-slow-edit"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Slow MA period
                </label>
                <input
                  id="ma-slow-edit"
                  type="number"
                  min={1}
                  value={maSlowEdit}
                  onChange={(e) => setMaSlowEdit(e.target.value)}
                  placeholder="e.g. 50"
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                  disabled={autoAiEdit}
                />
              </div>

              <div>
                <label
                  htmlFor="ma-type-edit"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Moving average type
                </label>
                <select
                  id="ma-type-edit"
                  value={maTypeEdit}
                  onChange={(e) => setMaTypeEdit(e.target.value)}
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                  disabled={autoAiEdit}
                >
                  <option value="">Not specified</option>
                  <option value="SMA">Simple MA (SMA)</option>
                  <option value="EMA">Exponential MA (EMA)</option>
                  <option value="WMA">Weighted MA (WMA)</option>
                </select>
              </div>

              <div>
                <label
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  AI optimization
                </label>
                <label
                  style={{
                    fontSize: "0.85rem",
                    color: "#e5f4ff",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 8,
                    cursor: "pointer",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={autoAiEdit}
                    onChange={(e) => setAutoAiEdit(e.target.checked)}
                    style={{ cursor: "pointer" }}
                  />
                  Let AI manage parameters automatically
                </label>
                <p
                  style={{
                    fontSize: "0.78rem",
                    color: "#7c8ca4",
                    marginTop: "0.25rem",
                  }}
                >
                  When enabled, GuvFX will allow AI to tune timeframe, symbols, and
                  moving average settings based on backtests.
                </p>
                {strategy?.auto_optimize_by_ai && strategy?.updated_at && (
                  <p
                    style={{
                      fontSize: "0.78rem",
                      color: "#9ca3af",
                      marginTop: "0.25rem",
                    }}
                  >
                    Last AI tune:{" "}
                    <span style={{ color: "#e5f4ff" }}>
                      {new Date(strategy.updated_at).toLocaleString()}
                    </span>
                  </p>
                )}
              </div>
            </div>

            <div
              style={{
                marginTop: "1rem",
                display: "flex",
                justifyContent: "flex-end",
                gap: "0.5rem",
                flexWrap: "wrap",
              }}
            >
              <Button
                type="button"
                variant="secondary"
                onClick={handleAutoTune}
                disabled={autoTuneLoading || !accessToken || !autoAiEdit}
              >
                {autoTuneLoading ? "Auto-tuning…" : "Auto-tune now"}
              </Button>
              <Button type="submit" disabled={savingSettings || !accessToken}>
                {savingSettings ? "Saving…" : "Save settings"}
              </Button>
            </div>
          </form>
        </Card>

        {/* Linked Accounts */}
        <Card
          title="Linked Accounts"
          subtitle="Choose which trading accounts this strategy should run on."
        >
          {loadingLinks && <p>Loading linked accounts…</p>}

          {!loadingLinks && assignments.length === 0 && (
            <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
              This strategy is not linked to any trading accounts yet.
            </p>
          )}

          {!loadingLinks && assignments.length > 0 && (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "0.45rem",
                marginBottom: "0.8rem",
              }}
            >
              {assignments.map((a) => {
                const overrideValue =
                  overrideDrafts[a.id] ??
                  (a.risk_per_trade_override_pct != null
                    ? String(a.risk_per_trade_override_pct)
                    : "");

                const usingOverride =
                  a.risk_per_trade_override_pct !== null &&
                  a.risk_per_trade_override_pct !== "" &&
                  a.risk_per_trade_override_pct !== undefined;

                // NEW: derive account and broker labels from accounts list (fallback to assignment fields)
                const linkedAccount = accounts.find((acc) => acc.id === a.account);

                const accountLabel =
                  linkedAccount?.name ||
                  a.account_name ||
                  `Account #${a.account}`;

                const brokerLabel =
                  linkedAccount?.broker_name ||
                  a.broker_name ||
                  "Broker";

                return (
                  <div
                    key={a.id}
                    style={{
                      border: "1px solid #222838",
                      borderRadius: 8,
                      padding: "0.75rem 0.9rem",
                      background: "rgba(7, 12, 30, 0.9)",
                      display: "flex",
                      flexDirection: "column",
                      gap: "0.4rem",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        gap: "0.75rem",
                      }}
                    >
                      <div>
                        <div style={{ fontWeight: 500, color: "#f1f5ff" }}>
                          {accountLabel}
                        </div>
                        <div style={{ fontSize: "0.8rem", color: "#9ca3af" }}>
                          {brokerLabel} · Linked{" "}
                          {new Date(a.created_at).toLocaleString()}
                        </div>
                        <div style={{ fontSize: "0.78rem", marginTop: "0.15rem" }}>
                          <span
                            style={{
                              display: "inline-flex",
                              alignItems: "center",
                              gap: 6,
                              color: a.is_active ? "#4ade80" : "#9ca3af",
                            }}
                          >
                            ● {a.is_active ? "Active" : "Paused"}
                          </span>
                        </div>
                      </div>

                      <div
                        style={{
                          display: "flex",
                          flexDirection: "row",
                          gap: "0.4rem",
                          alignItems: "center",
                        }}
                      >
                        <Button
                          type="button"
                          variant="secondary"
                          onClick={() =>
                            handleToggleAssignmentActive(a.id, a.is_active)
                          }
                          disabled={togglingId === a.id || !accessToken}
                          style={{ fontSize: "0.8rem", padding: "0.3rem 0.8rem" }}
                        >
                          {togglingId === a.id
                            ? "Updating…"
                            : a.is_active
                            ? "Pause"
                            : "Enable"}
                        </Button>
                        <Button
                          type="button"
                          variant="secondary"
                          onClick={() => handleOpenTestTrade(a)}
                          disabled={testTradeLoadingId === a.id || !accessToken}
                          style={{ fontSize: "0.8rem", padding: "0.3rem 0.8rem" }}
                        >
                          {testTradeLoadingId === a.id
                            ? "Queuing…"
                            : "Test trade job"}
                        </Button>
                        <Button
                          variant="secondary"
                          type="button"
                          onClick={() => handleRemoveLink(a.id)}
                          disabled={removingId === a.id}
                          style={{ fontSize: "0.8rem", padding: "0.3rem 0.8rem" }}
                        >
                          {removingId === a.id ? "Removing…" : "Remove"}
                        </Button>
                      </div>
                    </div>

                    {/* risk override editor (unchanged) */}
                    <div
                      style={{
                        display: "flex",
                        flexWrap: "wrap",
                        alignItems: "center",
                        gap: 8,
                        marginTop: "0.25rem",
                      }}
                    >
                      <span
                        style={{
                          fontSize: "0.78rem",
                          color: "#9ca3af",
                        }}
                      >
                        Risk override (% per trade):
                      </span>
                      <input
                        type="number"
                        min={0}
                        max={100}
                        step={0.1}
                        value={overrideValue}
                        onChange={(e) => handleOverrideChange(a.id, e.target.value)}
                        placeholder={
                          strategy?.risk_per_trade_pct
                            ? String(strategy.risk_per_trade_pct)
                            : "e.g. 1.0"
                        }
                        style={{
                          width: 80,
                          padding: "0.3rem 0.5rem",
                          borderRadius: 6,
                          border: "1px solid rgba(148,163,184,0.65)",
                          background: "rgba(15,23,42,0.9)",
                          color: "#e5f4ff",
                          fontSize: "0.8rem",
                          outline: "none",
                        }}
                      />
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() => handleSaveOverride(a.id)}
                        disabled={savingOverrideId === a.id || !accessToken}
                        style={{ padding: "0.25rem 0.6rem", fontSize: "0.8rem" }}
                      >
                        {savingOverrideId === a.id ? "Saving…" : "Save"}
                      </Button>
                      <span
                        style={{
                          fontSize: "0.75rem",
                          color: usingOverride ? "#facc15" : "#6b7280",
                        }}
                      >
                        {usingOverride
                          ? `Override: ${a.risk_per_trade_override_pct}% (strategy default ${
                              strategy?.risk_per_trade_pct ?? "—"
                            }%)`
                          : strategy?.risk_per_trade_pct
                          ? `Using strategy default (${strategy.risk_per_trade_pct}% per trade)`
                          : "No default risk set on strategy"}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* Link new account */}
          <form onSubmit={handleLinkAccount}>
            {accounts.length === 0 ? (
              <p style={{ fontSize: "0.85rem", color: "#cbd5f5" }}>
                You don’t have any trading accounts yet. Go to the{" "}
                <span style={{ fontWeight: 600 }}>Accounts</span> page to add one.
              </p>
            ) : (
              <>
                <label
                  htmlFor="account-select"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Link to another account
                </label>
                <div
                  style={{
                    display: "flex",
                    gap: "0.6rem",
                    alignItems: "center",
                    flexWrap: "wrap",
                  }}
                >
                  <select
                    id="account-select"
                    value={selectedAccountId}
                    onChange={(e) =>
                      setSelectedAccountId(
                        e.target.value ? Number(e.target.value) : ""
                      )
                    }
                    style={{
                      minWidth: 220,
                      padding: "0.45rem 0.6rem",
                      borderRadius: 8,
                      border: "1px solid rgba(148,163,184,0.65)",
                      background: "rgba(3,7,18,0.9)",
                      color: "#e5f4ff",
                      fontSize: "0.85rem",
                      outline: "none",
                    }}
                  >
                    <option value="">Select an account…</option>
                    {accounts.map((acc) => (
                      <option key={acc.id} value={acc.id}>
                        {acc.name} ({acc.broker_name})
                      </option>
                    ))}
                  </select>

                  <Button
                    type="submit"
                    disabled={linking || !accessToken}
                    style={{ padding: "0.45rem 1.1rem" }}
                  >
                    {linking ? "Linking…" : "Link account"}
                  </Button>
                </div>
              </>
            )}
          </form>
        </Card>

        <Card
          title="Backtest launcher"
          subtitle="Spin up a backtest using this strategy’s settings."
        >
          {btError && <Alert type="error">{btError}</Alert>}

          {!strategy ? (
            <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
              Loading strategy before launching a backtest...
            </p>
          ) : (
            <form onSubmit={handleLaunchBacktest}>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
                  gap: "0.75rem 1.25rem",
                }}
              >
                <div>
                  <label
                    htmlFor="bt-symbol"
                    style={{
                      display: "block",
                      fontSize: "0.85rem",
                      color: "#cbd5f5",
                      marginBottom: "0.25rem",
                    }}
                  >
                    Symbol
                  </label>
                  <input
                    id="bt-symbol"
                    type="text"
                    value={btSymbol}
                    onChange={(e) => setBtSymbol(e.target.value)}
                    placeholder="e.g. EURUSD"
                    style={{
                      width: "100%",
                      padding: "0.6rem 0.8rem",
                      borderRadius: 8,
                      border: "1px solid rgba(148,163,184,0.65)",
                      background: "rgba(3,7,18,0.9)",
                      color: "#e5f4ff",
                      fontSize: "0.9rem",
                      outline: "none",
                      boxSizing: "border-box",
                    }}
                  />
                </div>

                <div>
                  <label
                    htmlFor="bt-timeframe"
                    style={{
                      display: "block",
                      fontSize: "0.85rem",
                      color: "#cbd5f5",
                      marginBottom: "0.25rem",
                    }}
                  >
                    Timeframe
                  </label>
                  <input
                    id="bt-timeframe"
                    type="text"
                    value={btTimeframe}
                    onChange={(e) => setBtTimeframe(e.target.value)}
                    placeholder="e.g. H4"
                    style={{
                      width: "100%",
                      padding: "0.6rem 0.8rem",
                      borderRadius: 8,
                      border: "1px solid rgba(148,163,184,0.65)",
                      background: "rgba(3,7,18,0.9)",
                      color: "#e5f4ff",
                      fontSize: "0.9rem",
                      outline: "none",
                      boxSizing: "border-box",
                    }}
                  />
                </div>

                <div>
                  <label
                    htmlFor="bt-date-from"
                    style={{
                      display: "block",
                      fontSize: "0.85rem",
                      color: "#cbd5f5",
                      marginBottom: "0.25rem",
                    }}
                  >
                    Date from
                  </label>
                  <input
                    id="bt-date-from"
                    type="date"
                    value={btDateFrom}
                    onChange={(e) => setBtDateFrom(e.target.value)}
                    style={{
                      width: "100%",
                      padding: "0.6rem 0.8rem",
                      borderRadius: 8,
                      border: "1px solid rgba(148,163,184,0.65)",
                      background: "rgba(3,7,18,0.9)",
                      color: "#e5f4ff",
                      fontSize: "0.9rem",
                      outline: "none",
                      boxSizing: "border-box",
                    }}
                  />
                </div>

                <div>
                  <label
                    htmlFor="bt-date-to"
                    style={{
                      display: "block",
                      fontSize: "0.85rem",
                      color: "#cbd5f5",
                      marginBottom: "0.25rem",
                    }}
                  >
                    Date to
                  </label>
                  <input
                    id="bt-date-to"
                    type="date"
                    value={btDateTo}
                    onChange={(e) => setBtDateTo(e.target.value)}
                    style={{
                      width: "100%",
                      padding: "0.6rem 0.8rem",
                      borderRadius: 8,
                      border: "1px solid rgba(148,163,184,0.65)",
                      background: "rgba(3,7,18,0.9)",
                      color: "#e5f4ff",
                      fontSize: "0.9rem",
                      outline: "none",
                      boxSizing: "border-box",
                    }}
                  />
                </div>

                <div>
                  <label
                    htmlFor="bt-initial-balance"
                    style={{
                      display: "block",
                      fontSize: "0.85rem",
                      color: "#cbd5f5",
                      marginBottom: "0.25rem",
                    }}
                  >
                    Initial balance
                  </label>
                  <input
                    id="bt-initial-balance"
                    type="number"
                    min={0}
                    step={1}
                    value={btInitialBalance}
                    onChange={(e) => setBtInitialBalance(e.target.value)}
                    style={{
                      width: "100%",
                      padding: "0.6rem 0.8rem",
                      borderRadius: 8,
                      border: "1px solid rgba(148,163,184,0.65)",
                      background: "rgba(3,7,18,0.9)",
                      color: "#e5f4ff",
                      fontSize: "0.9rem",
                      outline: "none",
                      boxSizing: "border-box",
                    }}
                  />
                </div>
              </div>

              <div
                style={{
                  marginTop: "1rem",
                  display: "flex",
                  justifyContent: "flex-end",
                }}
              >
                <Button type="submit" disabled={btLaunching || !accessToken}>
                  {btLaunching ? "Launching…" : "Launch backtest"}
                </Button>
              </div>
            </form>
          )}
        </Card>

        <Card title="Recent backtests" subtitle="Latest runs for this strategy">
          {recentRunsError && <Alert type="error">{recentRunsError}</Alert>}
          {recentRunsLoading && (
            <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
              Loading recent backtests…
            </p>
          )}
          {!recentRunsLoading && recentRuns.length === 0 && !recentRunsError && (
            <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
              No backtests found yet. Use the launcher above to run one.
            </p>
          )}

          {!recentRunsLoading && recentRuns.length > 0 && (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "0.5rem",
              }}
            >
              {/* existing recent backtests mapping remains unchanged */}
              {recentRuns.map((run) => (
                <div
                  key={run.id}
                  style={{
                    display: "grid",
                    gridTemplateColumns:
                      "minmax(0, 2fr) minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1fr)",
                    gap: "0.25rem 1rem",
                    padding: "0.4rem 0.6rem",
                    borderRadius: 8,
                    border: "1px solid #111827",
                    background: "rgba(7, 12, 30, 0.9)",
                  }}
                >
                  <div>
                    <div style={{ fontSize: "0.9rem", color: "#e5f4ff" }}>
                      {run.config_name}
                    </div>
                    <div style={{ fontSize: "0.8rem", color: "#9ca3af" }}>
                      {run.symbol} · {run.timeframe}
                    </div>
                  </div>
                  <div style={{ fontSize: "0.8rem", color: "#9ca3af" }}>
                    {run.started_at
                      ? new Date(run.started_at).toLocaleString()
                      : "—"}
                  </div>
                  <div
                    style={{
                      fontSize: "0.8rem",
                      color:
                        run.status === "SUCCESS" || run.status === "COMPLETED"
                          ? "#4ade80"
                          : run.status === "FAILED"
                          ? "#f97373"
                          : "#e5e7eb",
                      fontWeight: 500,
                    }}
                  >
                    {run.status}
                  </div>
                  <div style={{ textAlign: "right" }}>
                    <Button
                      variant="secondary"
                      type="button"
                      onClick={() => router.push(`/backtests/${run.id}`)}
                    >
                      View
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card
          title="Recent execution jobs"
          subtitle="MT5 actions queued for this strategy across linked accounts"
        >
          {execJobsError && <Alert type="error">{execJobsError}</Alert>}

          {execJobsLoading && (
            <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
              Loading execution jobs…
            </p>
          )}

          {!execJobsLoading && !execJobsError && execJobs.length === 0 && (
            <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
              No execution jobs for this strategy yet.
            </p>
          )}

          {!execJobsLoading && !execJobsError && execJobs.length > 0 && (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "0.4rem",
              }}
            >
              {execJobs.map((job) => {
                const statusColor =
                  job.status === "SUCCESS"
                    ? "#4ade80"
                    : job.status === "FAILED"
                    ? "#f97373"
                    : "#e5e7eb";

                let message: string | undefined;
                if (
                  job.result &&
                  typeof job.result === "object" &&
                  "message" in job.result
                ) {
                  const maybeMessage = asRecord(job.result).message;
                  if (typeof maybeMessage === "string") {
                    message = maybeMessage;
                  }
                }

                let title = job.job_type;
                let titleColor = "#e5f4ff";

                if (
                  job.job_type === "OPEN_TRADE" &&
                  job.payload &&
                  typeof job.payload === "object"
                ) {
                  const payload = asRecord(job.payload);
                  const symbolVal = payload["symbol"];
                  const directionVal = payload["direction"];
                  const symbol =
                    readString(symbolVal)?.trim() !== ""
                      ? readString(symbolVal)?.trim()
                      : undefined;
                  const directionRaw =
                    readString(directionVal)?.trim() !== ""
                      ? readString(directionVal)!.trim().toUpperCase()
                      : undefined;

                  if (symbol || directionRaw) {
                    title = [directionRaw, symbol].filter(Boolean).join(" ");
                  } else {
                    title = "Open trade";
                  }

                  if (directionRaw === "BUY") {
                    titleColor = "#4ade80";
                  } else if (directionRaw === "SELL") {
                    titleColor = "#f97373";
                  }
                }

                return (
                  <div
                    key={job.id}
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      padding: "0.35rem 0.5rem",
                      borderRadius: 8,
                      border: "1px solid #111827",
                      background: "rgba(7, 12, 30, 0.9)",
                      fontSize: "0.8rem",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                      }}
                    >
                      <span
                        style={{
                          fontSize: "0.8rem",
                          color: titleColor,
                        }}
                      >
                        {title}
                      </span>
                      <span
                        style={{
                          fontSize: "0.8rem",
                          color: statusColor,
                          fontWeight: 600,
                        }}
                      >
                        {job.status}
                      </span>
                    </div>
                    <div
                      style={{
                        fontSize: "0.75rem",
                        color: "#9ca3af",
                        marginTop: "0.15rem",
                      }}
                    >
                      Account #{job.account} ·{" "}
                      {new Date(job.created_at).toLocaleString()}
                    </div>
                    {message && (
                      <div
                        style={{
                          fontSize: "0.75rem",
                          color: "#cbd5f5",
                          marginTop: "0.15rem",
                        }}
                      >
                        {message}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </Card>

        {/* Change history */}
        <Card
          title="Change history"
          subtitle="Recent manual edits and AI auto-tunes for this strategy."
        >
          {loadingLogs && <p>Loading history…</p>}

          {!loadingLogs && changeLogs.length === 0 && (
            <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
              No changes recorded yet.
            </p>
          )}

          {!loadingLogs && changeLogs.length > 0 && (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "0.6rem",
              }}
            >
              {changeLogs.map((log) => {
                const sourceLabel =
                  log.source === "AI_AUTO_TUNE" ? "AI auto-tune" : "Manual edit";

                const actorLabel = log.changed_by_email || "AI";

                const before: Record<string, unknown> =
                  log.before_settings ?? {};
                const after: Record<string, unknown> = log.after_settings ?? {};
                const trackedKeys = [
                  "timeframe",
                  "symbol_universe",
                  "magic_number",
                  "ma_fast_period",
                  "ma_slow_period",
                  "ma_type",
                ];

                const changes: string[] = [];
                trackedKeys.forEach((key) => {
                  const beforeVal = before[key];
                  const afterVal = after[key];
                  if (beforeVal !== afterVal) {
                    const from =
                      beforeVal === undefined || beforeVal === null
                        ? "—"
                        : String(beforeVal);
                    const to =
                      afterVal === undefined || afterVal === null
                        ? "—"
                        : String(afterVal);
                    const keyLabelMap: Record<string, string> = {
                      timeframe: "Timeframe",
                      symbol_universe: "Symbols",
                      magic_number: "Magic number",
                      ma_fast_period: "Fast MA",
                      ma_slow_period: "Slow MA",
                      ma_type: "MA type",
                    };
                    const label = keyLabelMap[key] || key;
                    changes.push(`${label}: ${from} → ${to}`);
                  }
                });

                return (
                  <div
                    key={log.id}
                    style={{
                      borderRadius: 8,
                      border: "1px solid #222838",
                      padding: "0.6rem 0.7rem",
                      background: "rgba(7,12,30,0.9)",
                      fontSize: "0.85rem",
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
                      <div>
                        <span style={{ color: "#e5f4ff" }}>{sourceLabel}</span>{" "}
                        <span style={{ color: "#9ca3af" }}>by {actorLabel}</span>
                      </div>
                      <div
                        style={{
                          fontSize: "0.78rem",
                          color: "#9ca3af",
                        }}
                      >
                        {new Date(log.created_at).toLocaleString()}
                      </div>
                    </div>
                    {changes.length > 0 ? (
                      <ul
                        style={{
                          margin: 0,
                          paddingLeft: "1.1rem",
                          color: "#cbd5f5",
                        }}
                      >
                        {changes.map((c, idx) => (
                          <li key={idx}>{c}</li>
                        ))}
                      </ul>
                    ) : (
                      <p
                        style={{
                          margin: 0,
                          color: "#7c8ca4",
                          fontStyle: "italic",
                        }}
                      >
                        No tracked field changes in this entry.
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </Card>

        <Card
          title="AI strategy insights"
          subtitle="Suggestions based on your recent backtests and risk settings"
        >
          {insightsError && <Alert type="error">{insightsError}</Alert>}

          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: "0.75rem",
              gap: "0.75rem",
              flexWrap: "wrap",
            }}
          >
            <p
              style={{
                fontSize: "0.85rem",
                color: "#9ca3af",
                margin: 0,
              }}
            >
              Use AI to get a high-level read on how this strategy is behaving and
              where to focus improvements.
            </p>
            <Button
              type="button"
              disabled={insightsLoading || !accessToken || !strategy}
              onClick={handleFetchInsights}
              style={{ padding: "0.45rem 1.1rem", fontSize: "0.85rem" }}
            >
              {insightsLoading ? "Loading insights…" : "Refresh AI insights"}
            </Button>
          </div>

          {!insights && !insightsLoading && !insightsError && (
            <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
              No insights yet. Click the Refresh AI insights button to generate
              them.
            </p>
          )}

          {insights && (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "0.75rem",
              }}
            >
              <div>
                <div
                  style={{
                    fontSize: "0.8rem",
                    color: "#9ca3af",
                    marginBottom: "0.25rem",
                  }}
                >
                  Summary
                </div>
                <div style={{ fontSize: "0.9rem", color: "#e5f4ff" }}>
                  {insights.summary}
                </div>
              </div>

              {insights.risk_assessment && (
                <div>
                  <div
                    style={{
                      fontSize: "0.8rem",
                      color: "#9ca3af",
                      marginBottom: "0.25rem",
                    }}
                  >
                    Risk assessment
                  </div>
                  <div style={{ fontSize: "0.9rem", color: "#e5f4ff" }}>
                    {insights.risk_assessment}
                  </div>
                </div>
              )}

              {insights.recommendations.length > 0 && (
                <div>
                  <div
                    style={{
                      fontSize: "0.8rem",
                      color: "#9ca3af",
                      marginBottom: "0.25rem",
                    }}
                  >
                    Recommendations
                  </div>
                  <ul
                    style={{
                      paddingLeft: "1.1rem",
                      margin: 0,
                      fontSize: "0.9rem",
                      color: "#e5f4ff",
                      display: "flex",
                      flexDirection: "column",
                      gap: "0.35rem",
                    }}
                  >
                    {insights.recommendations.map((rec, idx) => (
                      <li key={idx}>{rec}</li>
                    ))}
                  </ul>
                </div>
              )}

              {insights.notes && insights.notes.trim() && (
                <div>
                  <div
                    style={{
                      fontSize: "0.8rem",
                      color: "#9ca3af",
                      marginBottom: "0.25rem",
                    }}
                  >
                    Notes
                  </div>
                  <div style={{ fontSize: "0.9rem", color: "#e5f4ff" }}>
                    {insights.notes}
                  </div>
                </div>
              )}
            </div>
          )}
        </Card>
      </div>
  );
}
