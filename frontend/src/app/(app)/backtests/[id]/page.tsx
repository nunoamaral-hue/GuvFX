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
import { BacktestDiagnostics, DrawdownSparkline } from "@/components/backtests";

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
          {});
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
        const allRuns = await apiFetch<BacktestRun[]>(
          "/api/backtests/runs/",
          {});
        const filtered = allRuns.filter((r) => r.config === configId);
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

  const statusBadgeColor = (
    status: BacktestRun["status"]
  ): "green" | "blue" | "gray" => {
    if (status === "COMPLETED") return "green";
    if (status === "RUNNING" || status === "PENDING") return "blue";
    return "gray";
  };

  return (
      <div style={{ maxWidth: 900, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>
          {t(lang, "backtests.detailTitle")}
        </h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "0.5rem" }}>
          {t(lang, "backtests.detailSubtitle")}
        </p>
        <p style={{ fontSize: "0.75rem", color: "#64748b", marginBottom: "0.35rem" }}>
          {t(lang, "legal.microDisclaimer")}
        </p>
        <p style={{ fontSize: "0.72rem", color: "#64748b", marginBottom: "1rem", lineHeight: 1.5 }}>
          {t(lang, "backtests.disclaimerLine1")}
        </p>

        {error && <Alert type="error">{error}</Alert>}

        <Card
          title={config ? config.name : `Config #${configId}`}
          subtitle={config?.description || undefined}
        >
          {!accessToken && (
            <p style={{ fontStyle: "italic", fontSize: "0.9rem" }}>
              
            </p>
          )}

          {loadingConfig && <p>Loading configuration...</p>}

          {config && (
            <div style={{ fontSize: "0.95rem" }}>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
                  gap: "0.35rem 1.6rem",
                }}
              >
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Symbol:</span>
                  <span style={valueStyle}>{config.symbol}</span>
                </p>
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Timeframe:</span>
                  <span style={valueStyle}>{config.timeframe}</span>
                </p>
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Period:</span>
                  <span style={valueStyle}>
                    {config.date_from} → {config.date_to}
                  </span>
                </p>
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Initial balance:</span>
                  <span style={valueStyle}>{config.initial_balance}</span>
                </p>
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Risk per trade (%):</span>
                  <span style={valueStyle}>
                    {config.risk_per_trade_pct ?? "—"}
                  </span>
                </p>
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Slippage (points):</span>
                  <span style={valueStyle}>
                    {config.slippage_points ?? "—"}
                  </span>
                </p>
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Commission / lot:</span>
                  <span style={valueStyle}>
                    {config.commission_per_lot ?? "—"}
                  </span>
                </p>
              </div>

              <p
                style={{
                  fontSize: "0.78rem",
                  color: "#7c8ca4",
                  marginTop: "0.6rem",
                }}
              >
                Created:{" "}
                <span style={{ color: "#c9def7" }}>
                  {new Date(config.created_at).toLocaleString()}
                </span>
              </p>
            </div>
          )}
        </Card>

        <Card title="Runs">
          {loadingRuns && <p>Loading runs...</p>}

          {!loadingRuns && runs.length === 0 && accessToken && !error && (
            <p style={{ fontSize: "0.9rem" }}>
              No runs found for this configuration yet.
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

              return (
                <div
                  key={run.id}
                  style={{
                    border: "1px solid #222838",
                    borderRadius: 8,
                    padding: "0.75rem 1rem",
                    background: "rgba(7, 12, 30, 0.9)",
                  }}
                >
                  {/* header */}
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      marginBottom: "0.3rem",
                    }}
                  >
                    <div>
                      <h3
                        style={{
                          fontSize: "0.95rem",
                          margin: 0,
                          color: "#f1f5ff",
                        }}
                      >
                        Run #{run.id}
                      </h3>
                      <p
                        style={{
                          margin: 0,
                          fontSize: "0.8rem",
                          color: "#8fa0b7",
                        }}
                      >
                        <span style={labelStyle}>Created:</span>
                        <span style={valueStyle}>
                          {new Date(run.created_at).toLocaleString()}
                        </span>
                      </p>
                    </div>
                    <Badge color={statusBadgeColor(run.status)}>{run.status}</Badge>
                  </div>

                  {/* basics */}
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns:
                        "repeat(auto-fit, minmax(220px, 1fr))",
                      gap: "0.25rem 1.5rem",
                      fontSize: "0.85rem",
                    }}
                  >
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>Symbol:</span>
                      <span style={valueStyle}>{run.symbol}</span>
                    </p>
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>Timeframe:</span>
                      <span style={valueStyle}>{run.timeframe}</span>
                    </p>
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>Period:</span>
                      <span style={valueStyle}>
                        {run.date_from} → {run.date_to}
                      </span>
                    </p>
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>Initial balance:</span>
                      <span style={valueStyle}>{run.initial_balance}</span>
                    </p>
                  </div>

                  {/* timing / error */}
                  {run.started_at && (
                    <p
                      style={{
                        fontSize: "0.8rem",
                        color: "#7c8ca4",
                        margin: "0.2rem 0 0",
                      }}
                    >
                      <span style={labelStyle}>Started:</span>
                      <span style={valueStyle}>
                        {new Date(run.started_at).toLocaleString()}
                      </span>
                      {run.finished_at && (
                        <>
                          {" "}
                          &nbsp;|&nbsp;
                          <span style={labelStyle}>Finished:</span>
                          <span style={valueStyle}>
                            {new Date(run.finished_at).toLocaleString()}
                          </span>
                        </>
                      )}
                    </p>
                  )}

                  {run.error_message && (
                    <p
                      style={{
                        fontSize: "0.8rem",
                        color: "#ff9b9b",
                        margin: "0.2rem 0 0",
                      }}
                    >
                      <span style={labelStyle}>Error:</span>
                      <span>{run.error_message}</span>
                    </p>
                  )}

                  {/* metrics (observational) + sparkline */}
                  {typeof totalReturn === "number" && (
                    <div
                      style={{
                        marginTop: "0.35rem",
                        fontSize: "0.84rem",
                        color: "#c9d7f2",
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "flex-start",
                        gap: "1rem",
                      }}
                    >
                      <div>
                        <p style={{ margin: 0 }}>
                          <span style={labelStyle}>{t(lang, "backtests.observedReturn")}:</span>
                          <span style={valueStyle}>
                            {totalReturn.toFixed(2)}%
                          </span>
                        </p>
                        {typeof maxDD === "number" && (
                          <p style={{ margin: 0 }}>
                            <span style={labelStyle}>{t(lang, "backtests.maxDrawdown")}:</span>
                            <span style={valueStyle}>
                              {maxDD.toFixed(2)}%
                            </span>
                          </p>
                        )}
                        {typeof winRate === "number" && (
                          <p style={{ margin: 0 }}>
                            <span style={labelStyle}>{t(lang, "backtests.observedWinRate")}:</span>
                            <span style={valueStyle}>
                              {winRate.toFixed(2)}%
                            </span>
                          </p>
                        )}
                      </div>
                      {/* Drawdown sparkline */}
                      {metrics.equity_curve && Array.isArray(metrics.equity_curve) && metrics.equity_curve.length > 2 && (
                        <div style={{ textAlign: "right" }}>
                          <div style={{ fontSize: "0.7rem", color: "#64748b", marginBottom: "0.15rem" }}>
                            Drawdown
                          </div>
                          <DrawdownSparkline
                            equityCurve={metrics.equity_curve}
                            width={100}
                            height={28}
                          />
                        </div>
                      )}
                    </div>
                  )}

                  {/* Full diagnostics panel for completed runs with equity data */}
                  {run.status === "COMPLETED" &&
                    metrics.equity_curve &&
                    Array.isArray(metrics.equity_curve) &&
                    metrics.equity_curve.length > 5 && (
                      <div style={{ marginTop: "0.75rem" }}>
                        <BacktestDiagnostics
                          equityCurve={metrics.equity_curve}
                          maxDrawdownPct={maxDD}
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
