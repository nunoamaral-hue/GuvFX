"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
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

export default function BacktestsPage() {
  const lang = useLang();
  const [accessToken, setAccessToken] = useState<string>("");
  const [configs, setConfigs] = useState<BacktestConfig[]>([]);
  const [summaries, setSummaries] = useState<Record<number, BacktestSummary>>({});
  const [loading, setLoading] = useState(false);
  const [runningId, setRunningId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [processingPending, setProcessingPending] = useState(false);
  const [lastProcessedAt, setLastProcessedAt] = useState<string | null>(null);

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

  // Fetch configs + summaries
  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      setError(null);
      setInfo(null);
      try {
        const [cfgs, sums] = await Promise.all([
          apiFetch<BacktestConfig[]>("/api/backtests/configs/", {}),
          apiFetch<BacktestSummary[]>("/api/analytics/strategy-backtests/", {}),
        ]);

        setConfigs(cfgs);
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
    };

    fetchData();
  }, [accessToken]);

  const handleRunBacktest = async (configId: number) => {
    setError(null);
    setInfo(null);
    setRunningId(configId);

    try {
      await apiFetch("/api/backtests/runs/", {
        method: "POST",
        body: JSON.stringify({ config: configId }),
      });
      setInfo("Backtest run created. Process it with the worker when ready.");

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

      setInfo(`Processed ${res.processed_runs} pending backtest run(s).`);
      setLastProcessedAt(new Date(res.processed_at).toLocaleString());

      // Refresh summaries after processing
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

      <Card title={t(lang, "backtests.configsCardTitle")}>
        {/* Processing controls */}
        {accessToken && (
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
            <Button
              type="button"
              onClick={handleProcessPending}
              disabled={processingPending || !accessToken}
              style={{ padding: "0.45rem 1.1rem", fontSize: "0.85rem" }}
            >
              {processingPending
                ? t(lang, "backtests.processing")
                : t(lang, "backtests.processPending")}
            </Button>
          </div>
        )}

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

            return (
              <Link
                key={cfg.id}
                href={`/backtests/${cfg.id}`}
                style={{ textDecoration: "none", color: "inherit" }}
              >
                <div
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
                    <Badge color={statusBadgeColor(summary?.last_status ?? null)}>
                      {summary?.last_status ?? t(lang, "backtests.noRuns")}
                    </Badge>
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
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        handleRunBacktest(cfg.id);
                      }}
                      disabled={!accessToken || runningId === cfg.id}
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
              </Link>
            );
          })}
        </div>
      </Card>
    </div>
  );
}
