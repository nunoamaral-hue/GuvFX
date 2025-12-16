"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { apiFetch } from "@/lib/api";
import type { BacktestConfig } from "@/types/backtests";
import { AppShell } from "@/components/AppShell";
import { Card } from "@/components/ui/Card";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";

type BacktestMetricsSummary = Record<string, unknown> & {
  total_return_pct?: number;
  max_drawdown_pct?: number;
  win_rate_pct?: number;
  num_trades?: number;
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

const coerceNumber = (value: unknown, fallback = 0) => {
  if (typeof value === "number") return value;
  if (typeof value === "string") {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : fallback;
  }
  return fallback;
};

export default function BacktestsPage() {
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
    if (!accessToken) return;

    const fetchData = async () => {
      setLoading(true);
      setError(null);
      setInfo(null);
      try {
        const [cfgs, sums] = await Promise.all([
          apiFetch<BacktestConfig[]>("/api/backtests/configs/", {}, accessToken),
          apiFetch<BacktestSummary[]>(
            "/api/analytics/strategy-backtests/",
            {},
            accessToken
          ),
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
    if (!accessToken) return;
    setError(null);
    setInfo(null);
    setRunningId(configId);

    try {
      await apiFetch(
        "/api/backtests/runs/",
        {
          method: "POST",
          body: JSON.stringify({ config: configId }),
        },
        accessToken
      );
      setInfo("Backtest run created. Process it with the worker when ready.");

      const sums = await apiFetch<BacktestSummary[]>(
        "/api/analytics/strategy-backtests/",
        {},
        accessToken
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
          : "Failed to create backtest run.";
      setError(message);
    } finally {
      setRunningId(null);
    }
  };

  const handleProcessPending = async () => {
    if (!accessToken) return;
    setError(null);
    setInfo(null);
    setProcessingPending(true);

    try {
      const res = await apiFetch<{ processed_runs: number; processed_at: string }>(
        "/api/backtests/process-pending/",
        { method: "POST" },
        accessToken
      );

      setInfo(`Processed ${res.processed_runs} pending backtest run(s).`);
      setLastProcessedAt(new Date(res.processed_at).toLocaleString());

      // Refresh summaries after processing
      const sums = await apiFetch<BacktestSummary[]>(
        "/api/analytics/strategy-backtests/",
        {},
        accessToken
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
    <AppShell>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Backtests</h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
          Manage your backtest configurations, launch runs, and review performance.
        </p>

        {error && <Alert type="error">{error}</Alert>}
        {info && <Alert type="info">{info}</Alert>}

        <Card title="Backtest Configurations">
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
                    Last processed: {" "}
                    <span style={{ color: "#e5f4ff" }}>{lastProcessedAt}</span>
                  </>
                ) : (
                  "Pending runs have not been processed yet in this session."
                )}
              </div>
              <Button
                type="button"
                onClick={handleProcessPending}
                disabled={processingPending || !accessToken}
                style={{ padding: "0.45rem 1.1rem", fontSize: "0.85rem" }}
              >
                {processingPending ? "Processing…" : "Process pending runs"}
              </Button>
            </div>
          )}

          {!accessToken && (
            <p style={{ fontStyle: "italic", fontSize: "0.9rem" }}>
              No token found. Please log in again.
            </p>
          )}

          {loading && <p>Loading backtests...</p>}

          {!loading && configs.length === 0 && accessToken && !error && (
            <p style={{ fontSize: "0.9rem" }}>
              No backtest configs found yet. Create them via the backend/admin for now.
            </p>
          )}

          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: "0.75rem",
            }}
          >
            {configs.map((cfg) => {
              const summary = summaries[cfg.id];
<<<<<<< Updated upstream
              const metrics: BacktestMetricsSummary =
                summary?.last_metrics ?? ({} as BacktestMetricsSummary);
              /* eslint-disable @typescript-eslint/no-explicit-any */
              const winRate = (metrics as any).win_rate_pct ?? (metrics as any).win_rate ?? 0;
              const totalReturn = (metrics as any).total_return_pct ?? (metrics as any).total_return ?? 0;
              const maxDD = (metrics as any).max_drawdown_pct ?? (metrics as any).max_drawdown ?? 0;
              /* eslint-enable @typescript-eslint/no-explicit-any */
              void winRate; // intentionally unused
=======
              const metrics = summary?.last_metrics ?? {};
              const winRate = coerceNumber(
                metrics.win_rate_pct ?? metrics.win_rate
              );
              void winRate; // intentionally unused
              const totalReturn = coerceNumber(
                metrics.total_return_pct ?? metrics.total_return
              );
              const maxDD = coerceNumber(
                metrics.max_drawdown_pct ?? metrics.max_drawdown
              );
>>>>>>> Stashed changes

              return (
                <div
                  key={cfg.id}
                  style={{
                    border: "1px solid #222838",
                    borderRadius: 8,
                    padding: "0.75rem 1rem",
                    background: "rgba(7, 12, 30, 0.9)",
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
                        {cfg.name}
                      </h3>
                      <p
                        style={{
                          margin: 0,
                          fontSize: "0.8rem",
                          color: "#8fa0b7",
                        }}
                      >
                        <span style={labelStyle}>Strategy:</span>
                        <span style={valueStyle}>
                          {summary?.strategy_name
                            ? `${summary.strategy_name} (#${summary.strategy_id})`
                            : `#${cfg.strategy}`}
                        </span>
                      </p>
                    </div>
                    <Badge color={statusBadgeColor(summary?.last_status ?? null)}>
                      {summary?.last_status ?? "No runs"}
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
                      <span style={{ color: "#7c8ca4" }}>No description</span>
                    )}
                  </p>

                  {/* Info grid */}
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns:
                        "repeat(auto-fit, minmax(220px, 1fr))",
                      gap: "0.25rem 1.5rem",
                      fontSize: "0.86rem",
                    }}
                  >
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>Symbol:</span>
                      <span style={valueStyle}>{cfg.symbol}</span>
                    </p>
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>Timeframe:</span>
                      <span style={valueStyle}>{cfg.timeframe}</span>
                    </p>
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>Period:</span>
                      <span style={valueStyle}>
                        {cfg.date_from} → {cfg.date_to}
                      </span>
                    </p>
                    <p style={{ margin: 0 }}>
                      <span style={labelStyle}>Initial balance:</span>
                      <span style={valueStyle}>{cfg.initial_balance}</span>
                    </p>
                  </div>

                  {/* Summary metrics */}
                  {summary && (
                    <div
                      style={{
                        marginTop: "0.35rem",
                        fontSize: "0.84rem",
                        color: "#c9d7f2",
                      }}
                    >
                      <p style={{ margin: 0 }}>
                        <span style={labelStyle}>Runs:</span>
                        <span style={valueStyle}>{summary.num_runs}</span>
                        {summary.last_run_created_at && (
                          <>
                            {" "}
                            &nbsp;|&nbsp;
                            <span style={labelStyle}>Last run:</span>
                            <span style={valueStyle}>
                              {new Date(
                                summary.last_run_created_at
                              ).toLocaleString()}
                            </span>
                          </>
                        )}
                      </p>
                      {typeof totalReturn === "number" &&
                        typeof maxDD === "number" && (
                          <p style={{ margin: 0 }}>
                            <span style={labelStyle}>Total return:</span>
                            <span style={valueStyle}>
                              {totalReturn.toFixed(2)}%
                            </span>
                            {"  "}
                            &nbsp;|&nbsp;
                            <span style={labelStyle}>Max DD:</span>
                            <span style={valueStyle}>
                              {maxDD.toFixed(2)}%
                            </span>
                          </p>
                        )}
                    </div>
                  )}

                  {/* Actions */}
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
                      disabled={!accessToken || runningId === cfg.id}
                    >
                      {runningId === cfg.id ? "Creating run..." : "Run backtest"}
                    </Button>

                    <Link
                      href={`/backtests/${cfg.id}`}
                      style={{
                        fontSize: "0.8rem",
                        color: "#4ab3ff",
                        textDecoration: "none",
                      }}
                    >
                      View details →
                    </Link>
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      </div>
    </AppShell>
  );
}
