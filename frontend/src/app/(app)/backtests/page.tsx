"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { apiFetch } from "@/lib/api";
import type { BacktestConfig } from "@/types/backtests";
import { Card } from "@/components/ui/Card";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { useLang } from "@/components/AppShell";
import { t } from "@/lib/i18n";
import { DrawdownSparkline, LossClusteringBadge } from "@/components/backtests";

type EquityPoint = { timestamp?: string; equity: number } | number;

type Strategy = {
  id: number;
  name: string;
};

type BacktestMetricsSummary = Record<string, unknown> & {
  total_return_pct?: number;
  max_drawdown_pct?: number;
  win_rate_pct?: number;
  num_trades?: number;
  equity_curve?: EquityPoint[];
};

type BacktestSummary = {
  config_id: number;
  config_name: string;
  strategy_id: number;
  strategy_name: string;
  num_runs: number;
  last_status: string | null;
  last_run_created_at: string | null;
  last_metrics: BacktestMetricsSummary | null;
};

const TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"];

export default function BacktestsPage() {
  const lang = useLang();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [accessToken, setAccessToken] = useState<string>("");
  const [configs, setConfigs] = useState<BacktestConfig[]>([]);
  const [summaries, setSummaries] = useState<Record<number, BacktestSummary>>({});
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [loading, setLoading] = useState(false);
  const [runningId, setRunningId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [processingPending, setProcessingPending] = useState(false);
  const [lastProcessedAt, setLastProcessedAt] = useState<string | null>(null);

  // Create config modal state
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [creating, setCreating] = useState(false);
  const [formData, setFormData] = useState({
    name: "",
    description: "",
    strategy: "",
    symbol: "EURUSD",
    timeframe: "H1",
    date_from: "",
    date_to: "",
    initial_balance: "10000",
  });

  // Template selection state
  type TemplateInfo = { name: string; description: string; default_params: Record<string, number> };
  const [templates, setTemplates] = useState<TemplateInfo[]>([]);
  const [selectedTemplate, setSelectedTemplate] = useState("ema_trend");
  const [templateParams, setTemplateParams] = useState<Record<string, number>>({});

  // Fetch templates on mount
  useEffect(() => {
    (async () => {
      try {
        const data = await apiFetch<{ templates: TemplateInfo[] }>("/api/backtests/templates/", {});
        setTemplates(data.templates || []);
        const ema = (data.templates || []).find((t: TemplateInfo) => t.name === "ema_trend");
        if (ema) setTemplateParams(ema.default_params);
      } catch { /* non-blocking */ }
    })();
  }, []);

  // Update params when template changes
  const handleTemplateChange = (name: string) => {
    setSelectedTemplate(name);
    const tmpl = templates.find((t) => t.name === name);
    if (tmpl) setTemplateParams({ ...tmpl.default_params });
  };

  // Track if strategy is incomplete (from deep-link)
  const [strategyIncomplete, setStrategyIncomplete] = useState(false);

  // Track if we've already processed the deep-link to avoid re-triggering
  const deepLinkProcessedRef = useRef(false);

  const labelStyle: React.CSSProperties = {
    color: "#8fa0b7",
    fontSize: "0.84rem",
    marginRight: 4,
  };

  const valueStyle: React.CSSProperties = {
    color: "#e9f4ff",
    fontSize: "0.86rem",
  };

  // Load token on mount
  useEffect(() => {
    if (typeof window !== "undefined") {
      const stored = window.localStorage.getItem("guvfx_access_token");
      if (stored) {
        setAccessToken(stored);
      }
    }
  }, []);

  // Fetch configs + summaries + strategies
  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [cfgs, sums, strats] = await Promise.all([
        apiFetch<BacktestConfig[]>("/api/backtests/configs/", {}),
        apiFetch<BacktestSummary[]>("/api/analytics/strategy-backtests/", {}),
        apiFetch<Strategy[]>("/api/strategies/strategies/", {}),
      ]);

      setConfigs(cfgs);
      setStrategies(strats);
      const map: Record<number, BacktestSummary> = {};
      for (const s of sums) {
        map[s.config_id] = s;
      }
      setSummaries(map);
    } catch (err: unknown) {
      console.error(err);
      const message =
        err instanceof Error ? err.message : "Failed to load backtests.";
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (accessToken) {
      fetchData();
    }
  }, [accessToken, fetchData]);

  // Handle deep-link: ?create=true&strategy=<id>&incomplete=true
  // Opens modal and pre-selects strategy when strategies are loaded
  useEffect(() => {
    // Only process once, and only after strategies are loaded
    if (deepLinkProcessedRef.current) return;
    if (strategies.length === 0) return;

    const shouldCreate = searchParams.get("create") === "true";
    const strategyIdParam = searchParams.get("strategy");
    const isIncomplete = searchParams.get("incomplete") === "true";

    if (shouldCreate) {
      deepLinkProcessedRef.current = true;

      // Find the strategy if specified
      const strategyId = strategyIdParam ? parseInt(strategyIdParam, 10) : null;
      const matchedStrategy = strategyId
        ? strategies.find((s) => s.id === strategyId)
        : null;

      // Track if strategy is incomplete
      setStrategyIncomplete(isIncomplete);

      // Prefill form with strategy info
      if (matchedStrategy) {
        setFormData((prev) => ({
          ...prev,
          strategy: String(matchedStrategy.id),
          name: t(lang, "backtests.form.prefillName").replace(
            "{strategy}",
            matchedStrategy.name
          ),
        }));
      }

      // Open the modal
      setShowCreateModal(true);

      // Clear the URL params to avoid re-triggering on refresh
      // Use replaceState to avoid adding to history
      const url = new URL(window.location.href);
      url.searchParams.delete("create");
      url.searchParams.delete("strategy");
      url.searchParams.delete("incomplete");
      window.history.replaceState({}, "", url.pathname);
    }
  }, [strategies, searchParams, lang]);

  const handleCreateConfig = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setInfo(null);
    setCreating(true);

    try {
      // Encode template selection in description as JSON for backend
      const templateMeta = JSON.stringify({
        backtest_template: selectedTemplate,
        backtest_params: templateParams,
        bar_count: 1000,
      });

      await apiFetch("/api/backtests/configs/", {
        method: "POST",
        body: JSON.stringify({
          name: formData.name,
          description: templateMeta,
          strategy: parseInt(formData.strategy, 10),
          symbol: formData.symbol,
          timeframe: formData.timeframe,
          date_from: formData.date_from,
          date_to: formData.date_to,
          initial_balance: parseFloat(formData.initial_balance),
        }),
      });

      setInfo(t(lang, "backtests.form.success"));
      setShowCreateModal(false);
      setStrategyIncomplete(false);
      setFormData({
        name: "",
        description: "",
        strategy: "",
        symbol: "EURUSD",
        timeframe: "H1",
        date_from: "",
        date_to: "",
        initial_balance: "10000",
      });
      await fetchData();
    } catch (err: unknown) {
      console.error(err);
      const message =
        err instanceof Error ? err.message : t(lang, "backtests.form.error");
      setError(message);
    } finally {
      setCreating(false);
    }
  };

  const handleRunBacktest = async (configId: number) => {
    setError(null);
    setInfo(null);
    setRunningId(configId);

    try {
      await apiFetch("/api/backtests/runs/", {
        method: "POST",
        body: JSON.stringify({ config: configId }),
      });
      setInfo(t(lang, "backtests.runCreated"));

      // Refresh summaries
      const sums = await apiFetch<BacktestSummary[]>(
        "/api/analytics/strategy-backtests/",
        {}
      );
      const map: Record<number, BacktestSummary> = {};
      for (const s of sums) {
        map[s.config_id] = s;
      }
      setSummaries(map);
    } catch (err: unknown) {
      console.error(err);
      const message =
        err instanceof Error ? err.message : "Failed to create backtest run.";
      setError(message);
    } finally {
      setRunningId(null);
    }
  };

  const handleProcessPending = async () => {
    setError(null);
    setInfo(null);
    setProcessingPending(true);

    try {
      const res = await apiFetch<{ processed_runs: number; processed_at: string }>(
        "/api/backtests/process-pending/",
        { method: "POST" }
      );

      setInfo(
        t(lang, "backtests.processedRuns").replace("{count}", String(res.processed_runs))
      );
      setLastProcessedAt(new Date(res.processed_at).toLocaleString());

      // Refresh all data after processing
      await fetchData();
    } catch (err: unknown) {
      console.error(err);
      const message =
        err instanceof Error
          ? err.message
          : "Failed to process pending backtests.";
      setError(message);
    } finally {
      setProcessingPending(false);
    }
  };

  const statusBadgeColor = (status: string | null): "green" | "blue" | "gray" => {
    if (!status) return "gray";
    if (status === "COMPLETED") return "green";
    if (status === "RUNNING" || status === "PENDING") return "blue";
    return "gray";
  };

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      {/* Page header with disclaimers */}
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>
        {t(lang, "backtests.title")}
      </h1>
      <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "0.5rem" }}>
        {t(lang, "backtests.subtitle")}
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
        {t(lang, "backtests.disclaimerLine1")}
      </p>

      {error && <Alert type="error">{error}</Alert>}
      {info && <Alert type="info">{info}</Alert>}

      {/* Demo mode banner */}
      {!loading && configs.length > 0 && (
        <div
          style={{
            marginBottom: "1rem",
            padding: "0.65rem 0.85rem",
            background: "rgba(251, 191, 36, 0.08)",
            border: "1px solid rgba(251, 191, 36, 0.2)",
            borderRadius: 6,
            display: "flex",
            alignItems: "flex-start",
            gap: "0.5rem",
          }}
        >
          <span style={{ fontSize: "0.95rem", color: "#fbbf24", lineHeight: 1.4 }}>⚠</span>
          <div>
            <p
              style={{
                margin: 0,
                fontSize: "0.82rem",
                color: "#fbbf24",
                fontWeight: 500,
              }}
            >
              {t(lang, "backtests.demoDisclaimer")}
            </p>
            <p
              style={{
                margin: "0.15rem 0 0",
                fontSize: "0.74rem",
                color: "#d4a957",
                lineHeight: 1.4,
              }}
            >
              {t(lang, "backtests.demoNote")}
            </p>
          </div>
        </div>
      )}

      <Card title={t(lang, "backtests.configsCardTitle")}>
        {/* Header row with action buttons — ALWAYS visible */}
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "0.5rem",
            gap: "0.75rem",
            flexWrap: "wrap",
          }}
        >
          {/* Left: last processed info */}
          <div style={{ fontSize: "0.8rem", color: "#9ca3af" }}>
            {lastProcessedAt ? (
              <>
                {t(lang, "backtests.lastProcessed")}{" "}
                <span style={{ color: "#e5f4ff" }}>{lastProcessedAt}</span>
              </>
            ) : (
              t(lang, "backtests.pendingNotProcessed")
            )}
          </div>

          {/* Right: action buttons */}
          <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
            <Button
              type="button"
              data-testid="process-pending-btn"
              onClick={handleProcessPending}
              disabled={processingPending}
              style={{
                padding: "0.45rem 0.9rem",
                fontSize: "0.85rem",
                background: "transparent",
                border: "1px solid #334155",
                color: "#b7c5dd",
              }}
            >
              {processingPending
                ? t(lang, "backtests.processing")
                : t(lang, "backtests.processPending")}
            </Button>
            <Button
              type="button"
              data-testid="create-config-btn"
              onClick={() => setShowCreateModal(true)}
              style={{
                padding: "0.45rem 0.9rem",
                fontSize: "0.85rem",
                background: "linear-gradient(135deg, #3b82f6 0%, #2563eb 100%)",
                boxShadow: "0 0 12px rgba(59, 130, 246, 0.4)",
                border: "none",
              }}
            >
              {t(lang, "backtests.createConfig")}
            </Button>
          </div>
        </div>

        {/* Help line */}
        <p
          style={{
            fontSize: "0.75rem",
            color: "#64748b",
            margin: "0 0 0.75rem 0",
          }}
        >
          {t(lang, "backtests.headerHelpLine")}
        </p>

        {/* Loading state */}
        {loading && (
          <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
            {t(lang, "backtests.loading")}
          </p>
        )}

        {/* Empty state */}
        {!loading && configs.length === 0 && accessToken && !error && (
          <div
            style={{
              textAlign: "center",
              padding: "2rem 1rem",
              border: "1px dashed #333a4d",
              borderRadius: 8,
              background: "rgba(15, 20, 35, 0.6)",
            }}
          >
            <h3
              style={{
                fontSize: "1.1rem",
                color: "#e5f4ff",
                marginBottom: "0.5rem",
              }}
            >
              {t(lang, "backtests.emptyTitle")}
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
              {t(lang, "backtests.emptySubtitle")}
            </p>
            <div
              style={{
                display: "flex",
                gap: "0.75rem",
                justifyContent: "center",
                flexWrap: "wrap",
              }}
            >
              <Link href="/strategies/create">
                <Button type="button">{t(lang, "backtests.ctaCreateStrategy")}</Button>
              </Link>
              <Link href="/accounts">
                <Button
                  type="button"
                  style={{
                    background: "transparent",
                    border: "1px solid #334155",
                    color: "#e5f4ff",
                  }}
                >
                  {t(lang, "backtests.ctaLinkAccount")}
                </Button>
              </Link>
            </div>
          </div>
        )}

        {/* Config cards list */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "0.75rem",
          }}
        >
          {configs.map((cfg) => {
            const summary = summaries[cfg.id];
            const metrics: BacktestMetricsSummary =
              summary?.last_metrics ?? ({} as BacktestMetricsSummary);
            /* eslint-disable @typescript-eslint/no-explicit-any */
            const winRate =
              (metrics as any).win_rate_pct ?? (metrics as any).win_rate ?? null;
            const totalReturn =
              (metrics as any).total_return_pct ?? (metrics as any).total_return ?? null;
            const maxDD =
              (metrics as any).max_drawdown_pct ?? (metrics as any).max_drawdown ?? null;
            /* eslint-enable @typescript-eslint/no-explicit-any */

            const hasEquityCurve =
              metrics.equity_curve &&
              Array.isArray(metrics.equity_curve) &&
              metrics.equity_curve.length > 2;

            // Check if demo data (from metrics if available)
            /* eslint-disable @typescript-eslint/no-explicit-any */
            const isDemo = (metrics as any).demo === true;
            /* eslint-enable @typescript-eslint/no-explicit-any */

            return (
              <div
                key={cfg.id}
                role="button"
                tabIndex={0}
                onClick={(e) => {
                  // Only navigate if clicking on empty card area (not buttons, links, inputs, etc.)
                  const target = e.target as HTMLElement;
                  if (!target.closest("button, a, input, select, textarea")) {
                    router.push(`/backtests/${cfg.id}`);
                  }
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    const target = e.target as HTMLElement;
                    if (!target.closest("button, a, input, select, textarea")) {
                      e.preventDefault();
                      router.push(`/backtests/${cfg.id}`);
                    }
                  }
                }}
                style={{
                  border: "1px solid #222838",
                  borderRadius: 8,
                  padding: "0.75rem 1rem",
                  background: "rgba(7, 12, 30, 0.9)",
                  cursor: "pointer",
                  transition: "border-color 0.15s, background 0.15s",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.borderColor = "#3b82f6";
                  e.currentTarget.style.background = "rgba(15, 23, 50, 0.95)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.borderColor = "#222838";
                  e.currentTarget.style.background = "rgba(7, 12, 30, 0.9)";
                }}
              >
                  {/* Header row */}
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      marginBottom: "0.4rem",
                    }}
                  >
                    <div>
                      <h3
                        style={{
                          fontSize: "1.05rem",
                          margin: 0,
                          color: "#f1f5ff",
                        }}
                      >
                        {cfg.name || `${t(lang, "backtests.configId")}${cfg.id}`}
                      </h3>
                      <p
                        style={{
                          margin: 0,
                          fontSize: "0.8rem",
                          color: "#8fa0b7",
                        }}
                      >
                        <span style={labelStyle}>{t(lang, "backtests.strategyLabel")}</span>
                        <span style={valueStyle}>
                          {summary?.strategy_name
                            ? `${summary.strategy_name} (#${summary.strategy_id})`
                            : `#${cfg.strategy}`}
                        </span>
                      </p>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
                      {isDemo && (
                        <span
                          style={{
                            fontSize: "0.68rem",
                            padding: "0.1rem 0.45rem",
                            borderRadius: 4,
                            background: "rgba(251, 191, 36, 0.15)",
                            color: "#fbbf24",
                            border: "1px solid rgba(251, 191, 36, 0.3)",
                            fontWeight: 500,
                          }}
                        >
                          {t(lang, "backtests.demoBadge")}
                        </span>
                      )}
                      <Badge color={statusBadgeColor(summary?.last_status ?? null)}>
                        {summary?.last_status ?? t(lang, "backtests.noRuns")}
                      </Badge>
                    </div>
                  </div>

                  {/* Description */}
                  <p
                    style={{
                      fontSize: "0.86rem",
                      margin: "0.15rem 0 0.35rem 0",
                      color: "#d0e1ff",
                    }}
                  >
                    {cfg.description || (
                      <span style={{ color: "#7c8ca4" }}>
                        {t(lang, "backtests.noDescription")}
                      </span>
                    )}
                  </p>

                  {/* Info grid */}
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
                      gap: "0.25rem 1.5rem",
                      fontSize: "0.86rem",
                    }}
                  >
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>{t(lang, "backtests.symbolLabel")}</span>
                      <span style={valueStyle}>{cfg.symbol}</span>
                    </p>
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>{t(lang, "backtests.timeframeLabel")}</span>
                      <span style={valueStyle}>{cfg.timeframe}</span>
                    </p>
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>{t(lang, "backtests.periodLabel")}</span>
                      <span style={valueStyle}>
                        {cfg.date_from} → {cfg.date_to}
                      </span>
                    </p>
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>{t(lang, "backtests.initialBalanceLabel")}</span>
                      <span style={valueStyle}>{cfg.initial_balance}</span>
                    </p>
                  </div>

                  {/* Summary metrics & diagnostics */}
                  <div
                    style={{
                      marginTop: "0.5rem",
                      fontSize: "0.84rem",
                      color: "#c9d7f2",
                    }}
                  >
                    {/* Runs info */}
                    {summary && (
                      <p style={{ margin: "0 0 0.25rem 0" }}>
                        <span style={labelStyle}>{t(lang, "backtests.runsLabel")}</span>
                        <span style={valueStyle}>{summary.num_runs}</span>
                        {summary.last_run_created_at && (
                          <>
                            {" "}
                            &nbsp;|&nbsp;
                            <span style={labelStyle}>{t(lang, "backtests.lastRunLabel")}</span>
                            <span style={valueStyle}>
                              {new Date(summary.last_run_created_at).toLocaleString()}
                            </span>
                          </>
                        )}
                      </p>
                    )}

                    {/* Metrics row with diagnostics */}
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        gap: "0.75rem",
                        flexWrap: "wrap",
                      }}
                    >
                      {/* Left: Observational metrics */}
                      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem 1rem" }}>
                        <span>
                          <span style={labelStyle}>{t(lang, "backtests.observedReturn")}:</span>
                          <span style={valueStyle}>
                            {typeof totalReturn === "number" ? `${totalReturn.toFixed(2)}%` : "—"}
                          </span>
                        </span>
                        <span>
                          <span style={labelStyle}>{t(lang, "backtests.maxDrawdown")}:</span>
                          <span style={valueStyle}>
                            {typeof maxDD === "number" ? `${maxDD.toFixed(2)}%` : "—"}
                          </span>
                        </span>
                        <span>
                          <span style={labelStyle}>{t(lang, "backtests.observedWinRate")}:</span>
                          <span style={valueStyle}>
                            {typeof winRate === "number" ? `${winRate.toFixed(2)}%` : "—"}
                          </span>
                        </span>
                      </div>

                      {/* Right: Mini diagnostics (sparkline + clustering) */}
                      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                        {hasEquityCurve ? (
                          <>
                            <LossClusteringBadge
                              equityCurve={metrics.equity_curve!}
                              lang={lang}
                              compact
                            />
                            <DrawdownSparkline
                              equityCurve={metrics.equity_curve!}
                              width={80}
                              height={24}
                            />
                          </>
                        ) : (
                          <span
                            style={{
                              fontSize: "0.72rem",
                              color: "#64748b",
                              fontStyle: "italic",
                            }}
                          >
                            {t(lang, "backtests.noEquityData")}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>

                  {/* Actions row */}
                  <div
                    style={{
                      marginTop: "0.7rem",
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                    }}
                  >
                    <Button
                      onClick={() => handleRunBacktest(cfg.id)}
                      disabled={runningId === cfg.id}
                    >
                      {runningId === cfg.id
                        ? t(lang, "backtests.creatingRun")
                        : t(lang, "backtests.runBacktest")}
                    </Button>

                    <span
                      style={{
                        fontSize: "0.8rem",
                        color: "#4ab3ff",
                      }}
                    >
                      {t(lang, "backtests.viewConfig")}
                    </span>
                  </div>
                </div>
            );
          })}
        </div>
      </Card>

      {/* Create Config Modal */}
      {showCreateModal && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0, 0, 0, 0.7)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 1000,
          }}
          onClick={() => {
            setShowCreateModal(false);
            setStrategyIncomplete(false);
          }}
        >
          <div
            style={{
              background: "#0f1629",
              border: "1px solid #1e293b",
              borderRadius: 12,
              padding: "1.5rem",
              width: "100%",
              maxWidth: 520,
              maxHeight: "90vh",
              overflow: "auto",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <h2 style={{ margin: "0 0 0.25rem", fontSize: "1.25rem", color: "#f1f5ff" }}>
              {t(lang, "backtests.createConfigTitle")}
            </h2>
            <p style={{ margin: "0 0 1rem", fontSize: "0.82rem", color: "#9ca3af" }}>
              {t(lang, "backtests.createConfigSubtitle")}
            </p>

            {/* Warning banner if strategy is incomplete */}
            {strategyIncomplete && (
              <div
                style={{
                  marginBottom: "1rem",
                  padding: "0.6rem 0.8rem",
                  background: "rgba(251, 191, 36, 0.1)",
                  border: "1px solid rgba(251, 191, 36, 0.25)",
                  borderRadius: 6,
                  display: "flex",
                  alignItems: "flex-start",
                  gap: "0.5rem",
                }}
              >
                <span style={{ color: "#fbbf24", fontSize: "0.9rem", lineHeight: 1.4 }}>⚠</span>
                <p
                  style={{
                    margin: 0,
                    fontSize: "0.8rem",
                    color: "#fbbf24",
                    lineHeight: 1.45,
                  }}
                >
                  {t(lang, "backtests.modal.strategyIncompleteWarning")}
                </p>
              </div>
            )}

            <form onSubmit={handleCreateConfig}>
              {/* Name */}
              <div style={{ marginBottom: "0.85rem" }}>
                <label
                  style={{ display: "block", fontSize: "0.82rem", color: "#b7c5dd", marginBottom: "0.25rem" }}
                >
                  {t(lang, "backtests.form.nameLabel")} *
                </label>
                <input
                  type="text"
                  required
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  placeholder={t(lang, "backtests.form.namePlaceholder")}
                  style={{
                    width: "100%",
                    padding: "0.5rem 0.75rem",
                    background: "#1a2236",
                    border: "1px solid #334155",
                    borderRadius: 6,
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                  }}
                />
              </div>

              {/* Strategy Template */}
              <div style={{ marginBottom: "0.85rem" }}>
                <label style={{ display: "block", fontSize: "0.82rem", color: "#b7c5dd", marginBottom: "0.25rem" }}>
                  Strategy Template *
                </label>
                <select
                  value={selectedTemplate}
                  onChange={(e) => handleTemplateChange(e.target.value)}
                  style={{
                    width: "100%", padding: "0.5rem 0.75rem",
                    background: "#1a2236", border: "1px solid #334155",
                    borderRadius: 6, color: "#e5f4ff", fontSize: "0.9rem",
                  }}
                >
                  {templates.map((t) => (
                    <option key={t.name} value={t.name}>{t.description.split(".")[0]}</option>
                  ))}
                  {templates.length === 0 && <option value="ema_trend">EMA Trend (loading...)</option>}
                </select>
                <p style={{ fontSize: "0.72rem", color: "#64748b", margin: "0.25rem 0 0" }}>
                  Research Mode — results are simulated, not live execution
                </p>
              </div>

              {/* Template Parameters */}
              {Object.keys(templateParams).length > 0 && (
                <div style={{ marginBottom: "0.85rem", padding: "0.6rem", background: "rgba(74,179,255,0.04)", borderRadius: 8, border: "1px solid rgba(74,179,255,0.1)" }}>
                  <label style={{ display: "block", fontSize: "0.78rem", color: "#94a3b8", marginBottom: "0.4rem", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em" }}>
                    Parameters
                  </label>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.4rem" }}>
                    {Object.entries(templateParams).map(([key, val]) => (
                      <div key={key} style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
                        <label style={{ fontSize: "0.78rem", color: "#b7c5dd", minWidth: 90 }}>
                          {key.replace(/_/g, " ")}
                        </label>
                        <input
                          type="number"
                          value={val}
                          onChange={(e) => setTemplateParams({ ...templateParams, [key]: parseFloat(e.target.value) || 0 })}
                          step={key.includes("mult") ? 0.1 : 1}
                          style={{
                            width: 70, padding: "0.25rem 0.4rem",
                            background: "#0f172a", border: "1px solid #334155",
                            borderRadius: 4, color: "#e5f4ff", fontSize: "0.82rem",
                            textAlign: "center",
                          }}
                        />
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Strategy */}
              <div style={{ marginBottom: "0.85rem" }}>
                <label
                  style={{ display: "block", fontSize: "0.82rem", color: "#b7c5dd", marginBottom: "0.25rem" }}
                >
                  {t(lang, "backtests.form.strategyLabel")} *
                </label>
                {strategies.length === 0 ? (
                  <p style={{ fontSize: "0.82rem", color: "#f87171", margin: 0 }}>
                    {t(lang, "backtests.form.noStrategies")}
                  </p>
                ) : (
                  <select
                    required
                    value={formData.strategy}
                    onChange={(e) => setFormData({ ...formData, strategy: e.target.value })}
                    style={{
                      width: "100%",
                      padding: "0.5rem 0.75rem",
                      background: "#1a2236",
                      border: "1px solid #334155",
                      borderRadius: 6,
                      color: "#e5f4ff",
                      fontSize: "0.9rem",
                    }}
                  >
                    <option value="">{t(lang, "backtests.form.selectStrategy")}</option>
                    {strategies.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.name} (#{s.id})
                      </option>
                    ))}
                  </select>
                )}
              </div>

              {/* Symbol + Timeframe row */}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.75rem", marginBottom: "0.85rem" }}>
                <div>
                  <label
                    style={{ display: "block", fontSize: "0.82rem", color: "#b7c5dd", marginBottom: "0.25rem" }}
                  >
                    {t(lang, "backtests.form.symbolLabel")} *
                  </label>
                  <input
                    type="text"
                    required
                    value={formData.symbol}
                    onChange={(e) => setFormData({ ...formData, symbol: e.target.value.toUpperCase() })}
                    placeholder={t(lang, "backtests.form.symbolPlaceholder")}
                    style={{
                      width: "100%",
                      padding: "0.5rem 0.75rem",
                      background: "#1a2236",
                      border: "1px solid #334155",
                      borderRadius: 6,
                      color: "#e5f4ff",
                      fontSize: "0.9rem",
                    }}
                  />
                </div>
                <div>
                  <label
                    style={{ display: "block", fontSize: "0.82rem", color: "#b7c5dd", marginBottom: "0.25rem" }}
                  >
                    {t(lang, "backtests.form.timeframeLabel")} *
                  </label>
                  <select
                    required
                    value={formData.timeframe}
                    onChange={(e) => setFormData({ ...formData, timeframe: e.target.value })}
                    style={{
                      width: "100%",
                      padding: "0.5rem 0.75rem",
                      background: "#1a2236",
                      border: "1px solid #334155",
                      borderRadius: 6,
                      color: "#e5f4ff",
                      fontSize: "0.9rem",
                    }}
                  >
                    <option value="">{t(lang, "backtests.form.selectTimeframe")}</option>
                    {TIMEFRAMES.map((tf) => (
                      <option key={tf} value={tf}>
                        {tf}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              {/* Date range row */}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.75rem", marginBottom: "0.85rem" }}>
                <div>
                  <label
                    style={{ display: "block", fontSize: "0.82rem", color: "#b7c5dd", marginBottom: "0.25rem" }}
                  >
                    {t(lang, "backtests.form.dateFromLabel")} *
                  </label>
                  <input
                    type="date"
                    required
                    value={formData.date_from}
                    onChange={(e) => setFormData({ ...formData, date_from: e.target.value })}
                    style={{
                      width: "100%",
                      padding: "0.5rem 0.75rem",
                      background: "#1a2236",
                      border: "1px solid #334155",
                      borderRadius: 6,
                      color: "#e5f4ff",
                      fontSize: "0.9rem",
                    }}
                  />
                </div>
                <div>
                  <label
                    style={{ display: "block", fontSize: "0.82rem", color: "#b7c5dd", marginBottom: "0.25rem" }}
                  >
                    {t(lang, "backtests.form.dateToLabel")} *
                  </label>
                  <input
                    type="date"
                    required
                    value={formData.date_to}
                    onChange={(e) => setFormData({ ...formData, date_to: e.target.value })}
                    style={{
                      width: "100%",
                      padding: "0.5rem 0.75rem",
                      background: "#1a2236",
                      border: "1px solid #334155",
                      borderRadius: 6,
                      color: "#e5f4ff",
                      fontSize: "0.9rem",
                    }}
                  />
                </div>
              </div>

              {/* Initial Balance */}
              <div style={{ marginBottom: "1.25rem" }}>
                <label
                  style={{ display: "block", fontSize: "0.82rem", color: "#b7c5dd", marginBottom: "0.25rem" }}
                >
                  {t(lang, "backtests.form.initialBalanceLabel")} *
                </label>
                <input
                  type="number"
                  required
                  min="1"
                  step="0.01"
                  value={formData.initial_balance}
                  onChange={(e) => setFormData({ ...formData, initial_balance: e.target.value })}
                  placeholder={t(lang, "backtests.form.initialBalancePlaceholder")}
                  style={{
                    width: "100%",
                    padding: "0.5rem 0.75rem",
                    background: "#1a2236",
                    border: "1px solid #334155",
                    borderRadius: 6,
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                  }}
                />
              </div>

              {/* Actions */}
              <div style={{ display: "flex", gap: "0.75rem", justifyContent: "flex-end" }}>
                <Button
                  type="button"
                  onClick={() => {
                    setShowCreateModal(false);
                    setStrategyIncomplete(false);
                  }}
                  style={{
                    background: "transparent",
                    border: "1px solid #334155",
                    color: "#9ca3af",
                  }}
                >
                  {t(lang, "backtests.form.cancel")}
                </Button>
                <Button type="submit" disabled={creating || strategies.length === 0}>
                  {creating ? t(lang, "backtests.form.creating") : t(lang, "backtests.form.create")}
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
