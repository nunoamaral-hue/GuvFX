"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import { apiFetch } from "@/lib/api";
import type {
  BacktestConfig,
  BacktestRun,
  BacktestResultsResponse,
  ExecutionCandidateResponse,
  PromotionCandidate,
} from "@/types/backtests";
import { Card } from "@/components/ui/Card";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useLang } from "@/components/AppShell";
import { t } from "@/lib/i18n";
import { RunDetailPanel } from "@/components/backtests";
import { useAdminRole } from "@/components/admin/useAdminRole";

export default function BacktestDetailPage() {
  const lang = useLang();
  const params = useParams();
  const configId = Number(params?.id);

  // Auth is handled by HttpOnly cookies + AuthGate in (app)/layout.tsx
  // No localStorage token needed - apiFetch uses credentials: "include"
  const [config, setConfig] = useState<BacktestConfig | null>(null);
  const [runs, setRuns] = useState<BacktestRun[]>([]);
  const [loadingConfig, setLoadingConfig] = useState(false);
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [expandedRunId, setExpandedRunId] = useState<number | null>(null);
  const [runningBacktest, setRunningBacktest] = useState(false);
  const [processingPending, setProcessingPending] = useState(false);

  // B5/B7 Promotion state — keyed by run ID
  const [promotionData, setPromotionData] = useState<
    Record<number, { results: BacktestResultsResponse | null; loading: boolean; error: string | null }>
  >({});
  const [promotingRunId, setPromotingRunId] = useState<number | null>(null);

  // C1-FE: Admin role for review controls
  const adminCtx = useAdminRole();
  const canReview =
    adminCtx.authorized &&
    (adminCtx.role === "super_admin" || adminCtx.role === "ops_admin");
  const [reviewingCandidateId, setReviewingCandidateId] = useState<number | null>(null);

  // C2-FE: Execution candidate staging state
  const [stagingCandidateId, setStagingCandidateId] = useState<number | null>(null);

  const labelStyle: React.CSSProperties = {
    color: "#9db0c9",
    fontSize: "0.84rem",
    marginRight: 5,
  };

  const valueStyle: React.CSSProperties = {
    color: "#f0f6ff",
    fontSize: "0.86rem",
  };

  // Fetch config when configId is available
  useEffect(() => {
    if (!configId || isNaN(configId)) return;

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
  }, [configId]);

  // Helper to extract config ID from a run (handles various API shapes)
  const getRunConfigId = (run: BacktestRun): number | null => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const anyRun = run as any;

    // Try run.config (number or string)
    if (typeof run.config === "number") return run.config;
    if (typeof run.config === "string" && run.config) {
      const parsed = Number(run.config);
      if (!isNaN(parsed)) return parsed;
    }

    // Try run.config_id (number or string)
    if (typeof anyRun.config_id === "number") return anyRun.config_id;
    if (typeof anyRun.config_id === "string" && anyRun.config_id) {
      const parsed = Number(anyRun.config_id);
      if (!isNaN(parsed)) return parsed;
    }

    // Try nested run.config.id (object with id field)
    if (anyRun.config && typeof anyRun.config === "object") {
      const nestedId = anyRun.config.id;
      if (typeof nestedId === "number") return nestedId;
      if (typeof nestedId === "string" && nestedId) {
        const parsed = Number(nestedId);
        if (!isNaN(parsed)) return parsed;
      }
    }

    return null;
  };

  // Normalize API response: handle both array and paginated {count, results} shapes
  const normalizeRunsResponse = (
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    response: any
  ): BacktestRun[] => {
    if (Array.isArray(response)) {
      return response;
    }
    if (response && Array.isArray(response.results)) {
      return response.results;
    }
    // If response is an object but not paginated, it might be a single run
    if (response && typeof response === "object" && response.id) {
      return [response as BacktestRun];
    }
    return [];
  };

  // Fetch runs for this config with robust error handling and debug info
  // Auth is via HttpOnly cookies - no token check needed
  const fetchRuns = useCallback(async () => {
    if (!configId || isNaN(configId)) {
      return;
    }

    setLoadingRuns(true);

    let runsData: BacktestRun[] = [];

    try {
      // Strategy 1: Try filtered endpoint first
      try {
        const filteredUrl = `/api/backtests/runs/?config=${configId}`;
        const response = await apiFetch<unknown>(filteredUrl, {});
        const normalized = normalizeRunsResponse(response);

        // Filter client-side as well for safety
        runsData = normalized.filter((r) => {
          const runCfgId = getRunConfigId(r);
          return runCfgId === Number(configId);
        });
      } catch (filteredErr) {
        console.warn("Filtered endpoint failed, trying fallback:", filteredErr);

        // Strategy 2: Fallback to unfiltered endpoint
        try {
          const allUrl = "/api/backtests/runs/";
          const allResponse = await apiFetch<unknown>(allUrl, {});
          const allNormalized = normalizeRunsResponse(allResponse);

          // Filter client-side by config ID
          runsData = allNormalized.filter((r) => {
            const runCfgId = getRunConfigId(r);
            return runCfgId === Number(configId);
          });
        } catch (fallbackErr) {
          console.error("Fallback fetch also failed:", fallbackErr);
          throw fallbackErr;
        }
      }

      // Sort by created_at descending (newest first)
      runsData.sort(
        (a, b) =>
          new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
      );

      setRuns(runsData);
    } catch (err: unknown) {
      console.error("Failed to fetch runs:", err);
      const message =
        err instanceof Error ? err.message : "Failed to load backtest runs.";
      setError(message);
    } finally {
      setLoadingRuns(false);
    }
  }, [configId]);

  useEffect(() => {
    fetchRuns();
  }, [fetchRuns]);

  // Create a new run for this config
  const handleRunBacktest = async () => {
    if (!configId) return;
    setError(null);
    setInfo(null);
    setRunningBacktest(true);

    try {
      await apiFetch("/api/backtests/runs/", {
        method: "POST",
        body: JSON.stringify({ config: configId }),
      });
      setInfo(t(lang, "backtests.runCreated"));
      // Refresh runs list
      await fetchRuns();
    } catch (err: unknown) {
      console.error(err);
      const message =
        err instanceof Error ? err.message : "Failed to create backtest run.";
      setError(message);
    } finally {
      setRunningBacktest(false);
    }
  };

  // Process all pending runs
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

      // Refresh runs list after processing
      await fetchRuns();
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
    const isExpanding = expandedRunId !== runId;
    setExpandedRunId(isExpanding ? runId : null);

    // Scroll expanded panel into view after a brief delay for render
    if (isExpanding) {
      setTimeout(() => {
        const el = document.getElementById(`run-panel-${runId}`);
        if (el) {
          el.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
      }, 50);

      // Fetch B5 results (promotion data) if not already loaded
      if (!promotionData[runId]) {
        fetchPromotionData(runId);
      }
    }
  };

  // B7: Fetch B5 canonical results for a run (best-effort — 404 = no B5 job)
  const fetchPromotionData = async (runId: number) => {
    setPromotionData((prev) => ({
      ...prev,
      [runId]: { results: null, loading: true, error: null },
    }));

    try {
      const data = await apiFetch<BacktestResultsResponse>(
        `/api/backtests/results/${runId}/`,
        {}
      );
      setPromotionData((prev) => ({
        ...prev,
        [runId]: { results: data, loading: false, error: null },
      }));
    } catch {
      // 404 or other error — silently mark as unavailable
      setPromotionData((prev) => ({
        ...prev,
        [runId]: { results: null, loading: false, error: null },
      }));
    }
  };

  // B7: Promote a BacktestExecution
  const handlePromote = async (runId: number, executionId: number) => {
    setPromotingRunId(runId);
    setError(null);

    try {
      const data = await apiFetch<PromotionCandidate>(
        `/api/backtests/${executionId}/promote/`,
        { method: "POST" }
      );

      // Update local promotion data with the new/existing candidate
      setPromotionData((prev) => {
        const existing = prev[runId];
        if (!existing?.results) return prev;
        return {
          ...prev,
          [runId]: {
            ...existing,
            results: { ...existing.results, promotion_candidate: data },
          },
        };
      });

      setInfo("Promotion candidate created successfully.");
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to promote execution.";
      setError(message);
    } finally {
      setPromotingRunId(null);
    }
  };

  // B7: Promotion status chip color
  const getPromotionColor = (state: string): string => {
    switch (state) {
      case "pending":
        return "#fbbf24"; // amber
      case "approved":
        return "#4ade80"; // green
      case "rejected":
        return "#f87171"; // red
      default:
        return "#94a3b8"; // gray
    }
  };

  // C1-FE: Review a promotion candidate (approve/reject)
  const handleReview = async (
    runId: number,
    candidateId: number,
    decision: "approved" | "rejected"
  ) => {
    setReviewingCandidateId(candidateId);
    setError(null);

    try {
      const body: Record<string, string> = { decision };
      const updated = await apiFetch<PromotionCandidate>(
        `/api/backtests/candidates/${candidateId}/review/`,
        { method: "POST", body: JSON.stringify(body) }
      );

      // Update local promotion data with the reviewed candidate
      setPromotionData((prev) => {
        const existing = prev[runId];
        if (!existing?.results) return prev;
        return {
          ...prev,
          [runId]: {
            ...existing,
            results: { ...existing.results, promotion_candidate: updated },
          },
        };
      });

      setInfo(`Candidate ${decision} successfully.`);
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to review candidate.";
      setError(message);
    } finally {
      setReviewingCandidateId(null);
    }
  };

  // C2-FE: Stage an approved PromotionCandidate as ExecutionCandidate
  const handleStage = async (runId: number, promoCandidateId: number) => {
    setStagingCandidateId(promoCandidateId);
    setError(null);

    try {
      const data = await apiFetch<ExecutionCandidateResponse>(
        `/api/backtests/candidates/${promoCandidateId}/stage/`,
        { method: "POST" }
      );

      // Update local promotion data with execution_candidate
      setPromotionData((prev) => {
        const existing = prev[runId];
        if (!existing?.results) return prev;
        return {
          ...prev,
          [runId]: {
            ...existing,
            results: { ...existing.results, execution_candidate: data },
          },
        };
      });

      setInfo("Candidate marked ready for deployment.");
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to stage candidate.";
      setError(message);
    } finally {
      setStagingCandidateId(null);
    }
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
      {info && <Alert type="info">{info}</Alert>}

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
        {/* Action buttons row - always visible */}
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
              fontSize: "0.75rem",
              color: "#64748b",
              margin: 0,
            }}
          >
            {t(lang, "backtests.headerHelpLine")}
          </p>

          <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
            <Button
              type="button"
              onClick={handleProcessPending}
              disabled={processingPending}
              style={{
                padding: "0.4rem 0.75rem",
                fontSize: "0.82rem",
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
              onClick={handleRunBacktest}
              disabled={runningBacktest}
              style={{
                padding: "0.4rem 0.75rem",
                fontSize: "0.82rem",
                background: "linear-gradient(135deg, #3b82f6 0%, #2563eb 100%)",
                boxShadow: "0 0 10px rgba(59, 130, 246, 0.3)",
                border: "none",
              }}
            >
              {runningBacktest
                ? t(lang, "backtests.creatingRun")
                : t(lang, "backtests.runBacktest")}
            </Button>
          </div>
        </div>

        {loadingRuns && (
          <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
            {t(lang, "backtests.run.loadingRuns")}
          </p>
        )}

        {!loadingRuns && runs.length === 0 && !error && (
          <div
            style={{
              textAlign: "center",
              padding: "1.5rem 1rem",
              border: "1px dashed #333a4d",
              borderRadius: 8,
              background: "rgba(15, 20, 35, 0.6)",
            }}
          >
            <p
              style={{
                fontSize: "0.92rem",
                color: "#9ca3af",
                margin: 0,
              }}
            >
              {t(lang, "backtests.run.noRunsYet")}
            </p>
            <p
              style={{
                fontSize: "0.78rem",
                color: "#64748b",
                margin: "0.5rem 0 0",
              }}
            >
              {t(lang, "backtests.detailEmptyHint")}
            </p>
          </div>
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
            const numTrades = metrics.num_trades as number | undefined;
            const totalTrades = metrics.total_trades ?? numTrades;
            const isExpanded = expandedRunId === run.id;
            // equity_curve can be in run.equity_curve or metrics.equity_curve
            // Prefer metrics.equity_curve since that's where backend stores it
            const equityCurve = metrics.equity_curve || run.equity_curve;
            const hasEquityCurve =
              equityCurve &&
              Array.isArray(equityCurve) &&
              equityCurve.length >= 2;

            // Check if this is demo data
            const isDemo = metrics.demo === true;

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
                  {/* Top row: Run ID, status, demo badge */}
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

                  {/* No equity curve indicator */}
                  {!hasEquityCurve && run.status === "COMPLETED" && (
                    <p
                      style={{
                        fontSize: "0.78rem",
                        color: "#64748b",
                        margin: "0.4rem 0 0",
                        fontStyle: "italic",
                      }}
                    >
                      {t(lang, "backtests.run.noEquityCurve")}
                    </p>
                  )}
                </div>

                {/* Expandable detail panel */}
                {isExpanded && hasEquityCurve && (
                  <div
                    id={`run-panel-${run.id}`}
                    style={{
                      borderTop: "1px solid #1e293b",
                      padding: "0.75rem 0.75rem 1rem",
                    }}
                  >
                    {/* Demo disclaimer - compact */}
                    {isDemo && (
                      <div
                        style={{
                          marginBottom: "0.6rem",
                          padding: "0.35rem 0.6rem",
                          background: "rgba(251, 191, 36, 0.06)",
                          border: "1px solid rgba(251, 191, 36, 0.18)",
                          borderRadius: 5,
                          display: "flex",
                          alignItems: "center",
                          gap: "0.4rem",
                        }}
                      >
                        <span style={{ fontSize: "0.82rem", color: "#fbbf24" }}>
                          ⚠
                        </span>
                        <p
                          style={{
                            margin: 0,
                            fontSize: "0.72rem",
                            color: "#d4a957",
                            lineHeight: 1.35,
                          }}
                        >
                          <span style={{ color: "#fbbf24", fontWeight: 500 }}>
                            {t(lang, "backtests.run.demoLabel")}
                          </span>
                          {" — "}
                          {t(lang, "backtests.run.demoExplanation")}
                        </p>
                      </div>
                    )}

                    <RunDetailPanel
                      equityCurve={equityCurve!}
                      maxDrawdownPct={maxDD}
                      totalReturnPct={totalReturn}
                      observedHitRatePct={winRate}
                      totalTrades={totalTrades}
                      lang={lang}
                    />

                    {/* B7: Promotion section */}
                    {(() => {
                      const promo = promotionData[run.id];
                      if (!promo || promo.loading) {
                        return promo?.loading ? (
                          <div
                            style={{
                              marginTop: "0.75rem",
                              padding: "0.5rem 0.75rem",
                              fontSize: "0.8rem",
                              color: "#8fa0b7",
                            }}
                          >
                            Checking promotion status…
                          </div>
                        ) : null;
                      }

                      const results = promo.results;
                      if (!results || !results.execution_id) return null;

                      const candidate = results.promotion_candidate;
                      const execCandidate = results.execution_candidate;
                      const execId = results.execution_id;

                      return (
                        <div
                          style={{
                            marginTop: "0.75rem",
                            padding: "0.6rem 0.75rem",
                            background: "rgba(15, 20, 45, 0.7)",
                            border: "1px solid rgba(74, 179, 255, 0.12)",
                            borderRadius: 6,
                          }}
                        >
                          <div
                            style={{
                              display: "flex",
                              justifyContent: "space-between",
                              alignItems: "center",
                              gap: "0.75rem",
                              flexWrap: "wrap",
                            }}
                          >
                            <div>
                              <p
                                style={{
                                  margin: 0,
                                  fontSize: "0.82rem",
                                  color: "#c9d7f2",
                                  fontWeight: 500,
                                }}
                              >
                                Promotion Candidate
                              </p>
                              {candidate ? (
                                <>
                                  <div
                                    style={{
                                      display: "flex",
                                      alignItems: "center",
                                      gap: "0.5rem",
                                      marginTop: "0.25rem",
                                      flexWrap: "wrap",
                                    }}
                                  >
                                    {/* C2-FE: Show "Ready for Deployment" if staged, otherwise show promotion state */}
                                    {execCandidate ? (
                                      <span
                                        style={{
                                          fontSize: "0.72rem",
                                          padding: "0.15rem 0.5rem",
                                          borderRadius: 4,
                                          background: "rgba(56, 189, 248, 0.12)",
                                          color: "#38bdf8",
                                          border: "1px solid rgba(56, 189, 248, 0.35)",
                                          fontWeight: 500,
                                        }}
                                      >
                                        Ready for Deployment
                                      </span>
                                    ) : (
                                      <span
                                        style={{
                                          fontSize: "0.72rem",
                                          padding: "0.15rem 0.5rem",
                                          borderRadius: 4,
                                          background: `${getPromotionColor(candidate.state)}18`,
                                          color: getPromotionColor(candidate.state),
                                          border: `1px solid ${getPromotionColor(candidate.state)}40`,
                                          fontWeight: 500,
                                          textTransform: "capitalize",
                                        }}
                                      >
                                        {candidate.state}
                                      </span>
                                    )}
                                    <span
                                      style={{
                                        fontSize: "0.72rem",
                                        color: "#8fa0b7",
                                      }}
                                    >
                                      Created{" "}
                                      {new Date(candidate.created_at).toLocaleDateString("en-US", {
                                        year: "numeric",
                                        month: "short",
                                        day: "numeric",
                                      })}
                                    </span>
                                  </div>

                                  {/* C1-FE: Admin review controls (only when pending, not staged) */}
                                  {canReview && candidate.state === "pending" && !execCandidate && (
                                    <div
                                      style={{
                                        display: "flex",
                                        gap: "0.4rem",
                                        marginTop: "0.4rem",
                                      }}
                                    >
                                      <Button
                                        type="button"
                                        onClick={() =>
                                          handleReview(run.id, candidate.id, "approved")
                                        }
                                        disabled={reviewingCandidateId === candidate.id}
                                        style={{
                                          padding: "0.25rem 0.6rem",
                                          fontSize: "0.72rem",
                                          background: "rgba(74, 222, 128, 0.12)",
                                          border: "1px solid rgba(74, 222, 128, 0.35)",
                                          color: "#4ade80",
                                          borderRadius: 4,
                                          cursor:
                                            reviewingCandidateId === candidate.id
                                              ? "not-allowed"
                                              : "pointer",
                                          opacity:
                                            reviewingCandidateId === candidate.id ? 0.5 : 1,
                                        }}
                                      >
                                        {reviewingCandidateId === candidate.id
                                          ? "Reviewing…"
                                          : "Approve"}
                                      </Button>
                                      <Button
                                        type="button"
                                        onClick={() =>
                                          handleReview(run.id, candidate.id, "rejected")
                                        }
                                        disabled={reviewingCandidateId === candidate.id}
                                        style={{
                                          padding: "0.25rem 0.6rem",
                                          fontSize: "0.72rem",
                                          background: "rgba(248, 113, 113, 0.12)",
                                          border: "1px solid rgba(248, 113, 113, 0.35)",
                                          color: "#f87171",
                                          borderRadius: 4,
                                          cursor:
                                            reviewingCandidateId === candidate.id
                                              ? "not-allowed"
                                              : "pointer",
                                          opacity:
                                            reviewingCandidateId === candidate.id ? 0.5 : 1,
                                        }}
                                      >
                                        Reject
                                      </Button>
                                    </div>
                                  )}

                                  {/* C2-FE: Admin staging control (approved + not yet staged) */}
                                  {canReview &&
                                    candidate.state === "approved" &&
                                    !execCandidate && (
                                      <div style={{ marginTop: "0.4rem" }}>
                                        <Button
                                          type="button"
                                          onClick={() =>
                                            handleStage(run.id, candidate.id)
                                          }
                                          disabled={stagingCandidateId === candidate.id}
                                          style={{
                                            padding: "0.25rem 0.65rem",
                                            fontSize: "0.72rem",
                                            background: "rgba(56, 189, 248, 0.12)",
                                            border: "1px solid rgba(56, 189, 248, 0.35)",
                                            color: "#38bdf8",
                                            borderRadius: 4,
                                            cursor:
                                              stagingCandidateId === candidate.id
                                                ? "not-allowed"
                                                : "pointer",
                                            opacity:
                                              stagingCandidateId === candidate.id
                                                ? 0.5
                                                : 1,
                                          }}
                                        >
                                          {stagingCandidateId === candidate.id
                                            ? "Staging…"
                                            : "Mark Ready for Deployment"}
                                        </Button>
                                      </div>
                                    )}
                                </>
                              ) : (
                                <p
                                  style={{
                                    margin: "0.2rem 0 0",
                                    fontSize: "0.75rem",
                                    color: "#8fa0b7",
                                  }}
                                >
                                  No promotion candidate yet.
                                </p>
                              )}
                            </div>

                            {!candidate && (
                              <Button
                                type="button"
                                onClick={() => handlePromote(run.id, execId)}
                                disabled={promotingRunId === run.id}
                                style={{
                                  padding: "0.35rem 0.7rem",
                                  fontSize: "0.78rem",
                                  background:
                                    "linear-gradient(135deg, #8b5cf6 0%, #6d28d9 100%)",
                                  boxShadow: "0 0 8px rgba(139, 92, 246, 0.25)",
                                  border: "none",
                                  color: "#fff",
                                  borderRadius: 5,
                                  cursor:
                                    promotingRunId === run.id
                                      ? "not-allowed"
                                      : "pointer",
                                  opacity: promotingRunId === run.id ? 0.6 : 1,
                                }}
                              >
                                {promotingRunId === run.id
                                  ? "Promoting…"
                                  : "Promote Candidate"}
                              </Button>
                            )}
                          </div>
                        </div>
                      );
                    })()}
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
