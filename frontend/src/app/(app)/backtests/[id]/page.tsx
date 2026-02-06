"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { apiFetch } from "@/lib/api";
import type { BacktestConfig, BacktestRun } from "@/types/backtests";
import { Card } from "@/components/ui/Card";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { useLang } from "@/components/AppShell";
import { t } from "@/lib/i18n";
import { RunDetailPanel } from "@/components/backtests";

export default function BacktestDetailPage() {
  const lang = useLang();
  const params = useParams();
  const configId = Number(params?.id);

  const [accessToken, setAccessToken] = useState<string>("");
  const [config, setConfig] = useState<BacktestConfig | null>(null);
  const [runs, setRuns] = useState<BacktestRun[]>([]);
  const [loadingConfig, setLoadingConfig] = useState(false);
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedRunId, setExpandedRunId] = useState<number | null>(null);

  const labelStyle: React.CSSProperties = {
    color: "#8fa0b7",
    fontSize: "0.84rem",
    marginRight: 4,
  };

  const valueStyle: React.CSSProperties = {
    color: "#e9f4ff",
    fontSize: "0.86rem",
  };

  // Load token
  useEffect(() => {
    if (typeof window !== "undefined") {
      const stored = window.localStorage.getItem("guvfx_access_token");
      if (stored) {
        setAccessToken(stored);
      }
    }
  }, []);

  // Fetch config
  useEffect(() => {
    if (!accessToken || !configId) return;

    const fetchConfig = async () => {
      setLoadingConfig(true);
      setError(null);
      try {
        const data = await apiFetch<BacktestConfig>(
          `/api/backtests/configs/${configId}/`,
          {}
        );
        setConfig(data);
      } catch (err: unknown) {
        console.error(err);
        const message =
          err instanceof Error
            ? err.message
            : "Failed to load backtest configuration.";
        setError(message);
      } finally {
        setLoadingConfig(false);
      }
    };

    fetchConfig();
  }, [accessToken, configId]);

  // Fetch runs and filter
  useEffect(() => {
    if (!accessToken || !configId) return;

    const fetchRuns = async () => {
      setLoadingRuns(true);
      setError(null);
      try {
        const allRuns = await apiFetch<BacktestRun[]>("/api/backtests/runs/", {});
        const filtered = allRuns.filter((r) => r.config === configId);
        // Sort by created_at descending (newest first)
        filtered.sort(
          (a, b) =>
            new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
        );
        setRuns(filtered);
      } catch (err: unknown) {
        console.error(err);
        const message =
          err instanceof Error ? err.message : "Failed to load backtest runs.";
        setError(message);
      } finally {
        setLoadingRuns(false);
      }
    };

    fetchRuns();
  }, [accessToken, configId]);

  const getStatusBadgeColor = (
    status: BacktestRun["status"]
  ): "green" | "blue" | "gray" | "red" => {
    if (status === "COMPLETED" || status === "SUCCESS") return "green";
    if (status === "RUNNING") return "blue";
    if (status === "PENDING") return "gray";
    if (status === "FAILED") return "red";
    return "gray";
  };

  const getStatusLabel = (status: BacktestRun["status"]): string => {
    switch (status) {
      case "PENDING":
        return t(lang, "backtests.run.statusQueued");
      case "RUNNING":
        return t(lang, "backtests.run.statusRunning");
      case "COMPLETED":
      case "SUCCESS":
        return t(lang, "backtests.run.statusCompleted");
      case "FAILED":
        return t(lang, "backtests.run.statusFailed");
      default:
        return status;
    }
  };

  const toggleRunExpand = (runId: number) => {
    setExpandedRunId(expandedRunId === runId ? null : runId);
  };

  return (
    <div style={{ maxWidth: 960, margin: "0 auto" }}>
      {/* Page header with disclaimers */}
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>
        {t(lang, "backtests.detailTitle")}
      </h1>
      <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "0.5rem" }}>
        {t(lang, "backtests.detailSubtitle")}
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

      {/* Config card */}
      <Card
        title={config ? config.name : `${t(lang, "backtests.configId")}${configId}`}
        subtitle={config?.description || undefined}
      >
        {loadingConfig && (
          <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
            {t(lang, "backtests.loading")}
          </p>
        )}

        {config && (
          <div style={{ fontSize: "0.95rem" }}>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
                gap: "0.35rem 1.6rem",
              }}
            >
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>{t(lang, "backtests.symbolLabel")}</span>
                <span style={valueStyle}>{config.symbol}</span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>{t(lang, "backtests.timeframeLabel")}</span>
                <span style={valueStyle}>{config.timeframe}</span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>{t(lang, "backtests.periodLabel")}</span>
                <span style={valueStyle}>
                  {config.date_from} → {config.date_to}
                </span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>{t(lang, "backtests.initialBalanceLabel")}</span>
                <span style={valueStyle}>{config.initial_balance}</span>
              </p>
            </div>

            <p
              style={{
                fontSize: "0.78rem",
                color: "#7c8ca4",
                marginTop: "0.6rem",
              }}
            >
              {t(lang, "backtests.run.createdAt")}{" "}
              <span style={{ color: "#c9def7" }}>
                {new Date(config.created_at).toLocaleString()}
              </span>
            </p>
          </div>
        )}
      </Card>

      {/* Runs card */}
      <Card title={t(lang, "backtests.run.runsCardTitle")}>
        {loadingRuns && (
          <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
            {t(lang, "backtests.run.loadingRuns")}
          </p>
        )}

        {!loadingRuns && runs.length === 0 && accessToken && !error && (
          <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
            {t(lang, "backtests.run.noRunsYet")}
          </p>
        )}

        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "0.75rem",
          }}
        >
          {runs.map((run) => {
            const metrics = run.metrics || {};
            const totalReturn = metrics.total_return_pct;
            const maxDD = metrics.max_drawdown_pct;
            const winRate = metrics.win_rate_pct;
            const totalTrades = metrics.total_trades;
            const isExpanded = expandedRunId === run.id;
            const hasEquityCurve =
              metrics.equity_curve &&
              Array.isArray(metrics.equity_curve) &&
              metrics.equity_curve.length > 2;

            return (
              <div
                key={run.id}
                style={{
                  border: "1px solid #222838",
                  borderRadius: 8,
                  background: "rgba(7, 12, 30, 0.9)",
                  overflow: "hidden",
                }}
              >
                {/* Run header (always visible) */}
                <div
                  style={{
                    padding: "0.75rem 1rem",
                    cursor: hasEquityCurve ? "pointer" : "default",
                    transition: "background 0.15s",
                  }}
                  onClick={() => hasEquityCurve && toggleRunExpand(run.id)}
                  onMouseEnter={(e) => {
                    if (hasEquityCurve) {
                      e.currentTarget.style.background = "rgba(15, 23, 50, 0.9)";
                    }
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = "transparent";
                  }}
                >
                  {/* Top row: Run ID, status */}
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      marginBottom: "0.3rem",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                      <h3
                        style={{
                          fontSize: "0.95rem",
                          margin: 0,
                          color: "#f1f5ff",
                        }}
                      >
                        {t(lang, "backtests.run.title")} #{run.id}
                      </h3>
                      {hasEquityCurve && (
                        <span
                          style={{
                            fontSize: "0.72rem",
                            color: "#4ab3ff",
                          }}
                        >
                          {isExpanded
                            ? t(lang, "backtests.run.collapseDetails")
                            : t(lang, "backtests.run.expandDetails")}
                        </span>
                      )}
                    </div>
                    <Badge color={getStatusBadgeColor(run.status)}>
                      {getStatusLabel(run.status)}
                    </Badge>
                  </div>

                  {/* Dates row */}
                  <p
                    style={{
                      margin: 0,
                      fontSize: "0.8rem",
                      color: "#8fa0b7",
                    }}
                  >
                    <span style={labelStyle}>{t(lang, "backtests.run.createdAt")}</span>
                    <span style={valueStyle}>
                      {new Date(run.created_at).toLocaleString()}
                    </span>
                    {run.started_at && (
                      <>
                        {" "}
                        &nbsp;|&nbsp;
                        <span style={labelStyle}>{t(lang, "backtests.run.startedAt")}</span>
                        <span style={valueStyle}>
                          {new Date(run.started_at).toLocaleString()}
                        </span>
                      </>
                    )}
                    {run.finished_at && (
                      <>
                        {" "}
                        &nbsp;|&nbsp;
                        <span style={labelStyle}>{t(lang, "backtests.run.completedAt")}</span>
                        <span style={valueStyle}>
                          {new Date(run.finished_at).toLocaleString()}
                        </span>
                      </>
                    )}
                  </p>

                  {/* Config basics */}
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
                      gap: "0.25rem 1.5rem",
                      fontSize: "0.85rem",
                      marginTop: "0.35rem",
                    }}
                  >
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>{t(lang, "backtests.symbolLabel")}</span>
                      <span style={valueStyle}>{run.symbol}</span>
                    </p>
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>{t(lang, "backtests.timeframeLabel")}</span>
                      <span style={valueStyle}>{run.timeframe}</span>
                    </p>
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>{t(lang, "backtests.run.dataWindow")}</span>
                      <span style={valueStyle}>
                        {run.date_from} → {run.date_to}
                      </span>
                    </p>
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>{t(lang, "backtests.initialBalanceLabel")}</span>
                      <span style={valueStyle}>{run.initial_balance}</span>
                    </p>
                  </div>

                  {/* Error message if failed */}
                  {run.error_message && (
                    <p
                      style={{
                        fontSize: "0.8rem",
                        color: "#ff9b9b",
                        margin: "0.35rem 0 0",
                      }}
                    >
                      <span style={labelStyle}>{t(lang, "backtests.run.errorLabel")}</span>
                      <span>{run.error_message}</span>
                    </p>
                  )}

                  {/* Quick metrics summary (collapsed view) */}
                  {!isExpanded && typeof totalReturn === "number" && (
                    <div
                      style={{
                        marginTop: "0.4rem",
                        fontSize: "0.82rem",
                        color: "#c9d7f2",
                        display: "flex",
                        flexWrap: "wrap",
                        gap: "0.5rem 1rem",
                      }}
                    >
                      <span>
                        <span style={labelStyle}>{t(lang, "backtests.observedReturn")}:</span>
                        <span
                          style={{
                            color: totalReturn >= 0 ? "#4ade80" : "#f87171",
                          }}
                        >
                          {totalReturn >= 0 ? "+" : ""}
                          {totalReturn.toFixed(2)}%
                        </span>
                      </span>
                      {typeof maxDD === "number" && (
                        <span>
                          <span style={labelStyle}>{t(lang, "backtests.maxDrawdown")}:</span>
                          <span style={{ color: "#f87171" }}>{maxDD.toFixed(2)}%</span>
                        </span>
                      )}
                      {typeof winRate === "number" && (
                        <span>
                          <span style={labelStyle}>{t(lang, "backtests.observedWinRate")}:</span>
                          <span style={valueStyle}>{winRate.toFixed(1)}%</span>
                        </span>
                      )}
                    </div>
                  )}
                </div>

                {/* Expandable detail panel */}
                {isExpanded && hasEquityCurve && (
                  <div
                    style={{
                      borderTop: "1px solid #1e293b",
                      padding: "0.75rem",
                    }}
                  >
                    <RunDetailPanel
                      equityCurve={metrics.equity_curve!}
                      maxDrawdownPct={maxDD}
                      totalReturnPct={totalReturn}
                      observedHitRatePct={winRate}
                      totalTrades={totalTrades}
                      lang={lang}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </Card>
    </div>
  );
}
