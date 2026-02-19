"use client";

import type { CSSProperties } from "react";
import { useEffect, useState } from "react";
import { useRouter, useParams } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Alert } from "@/components/ui/Alert";
import { t, detectLang, type Lang } from "@/lib/i18n";
import type { BacktestConfig } from "@/types/backtests";

// =============================================================================
// Types
// =============================================================================

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
  ma_fast_period: number | null;
  ma_slow_period: number | null;
  ma_type: string | null;
  auto_optimize_by_ai: boolean;
  created_at: string;
  updated_at: string;
};

// =============================================================================
// Live Status Types
// =============================================================================

type LiveStatusCheck = {
  name: string;
  status: "PASS" | "FAIL" | "WARN";
  detail: string;
};

type LiveStatusResponse = {
  overall: "PASS" | "FAIL" | "DEGRADED";
  strategy_id: number;
  account_id: number;
  checked_at: string;
  checks: LiveStatusCheck[];
};

type StrategyAssignmentBrief = {
  id: number;
  account: number;
  is_active: boolean;
};

// =============================================================================
// Readiness Check Helper
// =============================================================================

type ReadinessItem = {
  key: string;
  passed: boolean;
};

const checkReadiness = (strategy: Strategy): ReadinessItem[] => {
  return [
    {
      key: "strategy.readiness.hasName",
      passed: Boolean(strategy.name && strategy.name.trim().length > 0),
    },
    {
      key: "strategy.readiness.hasSymbol",
      passed: Boolean(
        strategy.symbol_universe && strategy.symbol_universe.trim().length > 0
      ),
    },
    {
      key: "strategy.readiness.hasTimeframe",
      passed: Boolean(strategy.timeframe && strategy.timeframe.trim().length > 0),
    },
    {
      key: "strategy.readiness.hasEntryLogic",
      passed: Boolean(strategy.entry_logic && strategy.entry_logic.trim().length > 0),
    },
    {
      key: "strategy.readiness.hasExitLogic",
      passed: Boolean(strategy.exit_logic && strategy.exit_logic.trim().length > 0),
    },
  ];
};

const isTestReady = (strategy: Strategy): boolean => {
  const items = checkReadiness(strategy);
  return items.every((item) => item.passed);
};

// =============================================================================
// Styles
// =============================================================================

const labelStyle: CSSProperties = {
  color: "#9db0c9",
  fontSize: "0.85rem",
  marginRight: 4,
};

const valueStyle: CSSProperties = {
  color: "#f0f6ff",
  fontSize: "0.9rem",
};

const checkItemStyle = (passed: boolean): CSSProperties => ({
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
  padding: "0.4rem 0",
  color: passed ? "#4ade80" : "#f97373",
  fontSize: "0.9rem",
});

const checkIconStyle = (passed: boolean): CSSProperties => ({
  width: 18,
  height: 18,
  borderRadius: "50%",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  backgroundColor: passed ? "rgba(74, 222, 128, 0.15)" : "rgba(249, 115, 115, 0.15)",
  color: passed ? "#4ade80" : "#f97373",
  fontSize: "0.75rem",
  fontWeight: 600,
});

// =============================================================================
// Component
// =============================================================================

export default function StrategyControlPage() {
  const params = useParams();
  const strategyId = Number(params?.id);
  const router = useRouter();

  const [lang, setLang] = useState<Lang>("en");
  const [strategy, setStrategy] = useState<Strategy | null>(null);
  const [loadingStrategy, setLoadingStrategy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Linked backtest configs
  const [linkedConfigs, setLinkedConfigs] = useState<BacktestConfig[]>([]);
  const [loadingConfigs, setLoadingConfigs] = useState(false);

  // Detect language
  useEffect(() => {
    setLang(detectLang());
  }, []);

  // Fetch strategy
  useEffect(() => {
    if (!strategyId || Number.isNaN(strategyId)) return;

    const fetchStrategy = async () => {
      setLoadingStrategy(true);
      setError(null);
      try {
        const data = await apiFetch<Strategy>(
          `/api/strategies/strategies/${strategyId}/`,
          {}
        );
        setStrategy(data);
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
  }, [strategyId]);

  // Fetch linked backtest configs
  useEffect(() => {
    if (!strategyId || Number.isNaN(strategyId)) return;

    const fetchConfigs = async () => {
      setLoadingConfigs(true);
      try {
        const res = await apiFetch<BacktestConfig[] | { results: BacktestConfig[] }>(
          `/api/backtests/configs/?strategy=${strategyId}`,
          {}
        );
        // Handle paginated or array response
        const configs = Array.isArray(res) ? res : res.results || [];
        setLinkedConfigs(configs);
      } catch (err: unknown) {
        console.error("Failed to fetch linked configs:", err);
        // Non-fatal - just show empty
        setLinkedConfigs([]);
      } finally {
        setLoadingConfigs(false);
      }
    };

    fetchConfigs();
  }, [strategyId]);

  // Live status state
  const [liveStatus, setLiveStatus] = useState<LiveStatusResponse | null>(null);
  const [liveStatusLoading, setLiveStatusLoading] = useState(false);

  // Lazy-load live status: find active assignment → fetch live-status
  useEffect(() => {
    if (!strategyId || Number.isNaN(strategyId)) return;
    let cancelled = false;

    const fetchLiveStatus = async () => {
      setLiveStatusLoading(true);
      try {
        // 1. Get active assignments for this strategy
        const assignments = await apiFetch<
          StrategyAssignmentBrief[] | { results: StrategyAssignmentBrief[] }
        >(`/api/strategies/assignments/?strategy=${strategyId}`, {});
        const list = Array.isArray(assignments) ? assignments : assignments.results || [];
        const active = list.find((a) => a.is_active);
        if (!active || cancelled) {
          if (!cancelled) setLiveStatus(null);
          return;
        }

        // 2. Fetch live-status for the active assignment's account
        const statusData = await apiFetch<LiveStatusResponse>(
          `/api/strategies/strategies/${strategyId}/execution/live-status/?account_id=${active.account}`,
          {}
        );
        if (!cancelled) setLiveStatus(statusData);
      } catch {
        if (!cancelled) setLiveStatus(null);
      } finally {
        if (!cancelled) setLiveStatusLoading(false);
      }
    };

    fetchLiveStatus();
    return () => { cancelled = true; };
  }, [strategyId]);

  // Guard for invalid strategyId
  if (Number.isNaN(strategyId)) {
    return (
      <div style={{ maxWidth: 900, margin: "0 auto" }}>
        <Alert type="error">Invalid strategy ID.</Alert>
      </div>
    );
  }

  const readinessItems = strategy ? checkReadiness(strategy) : [];
  const testReady = strategy ? isTestReady(strategy) : false;

  return (
    <div style={{ maxWidth: 900, margin: "0 auto" }}>
      {/* Header */}
      <div style={{ marginBottom: "1.5rem" }}>
        <button
          onClick={() => router.push("/strategies")}
          style={{
            marginBottom: "0.5rem",
            color: "#9db0c9",
            background: "transparent",
            border: "none",
            cursor: "pointer",
            fontSize: "0.85rem",
            padding: "0.25rem 0",
          }}
        >
          {t(lang, "strategy.actions.backToList")}
        </button>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem", color: "#f0f6ff" }}>
          {t(lang, "strategy.control.title")}
        </h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "0.5rem" }}>
          {t(lang, "strategy.control.subtitle")}
        </p>
        {/* Legal disclaimer */}
        <p
          style={{
            fontSize: "0.78rem",
            color: "#7c8ca4",
            backgroundColor: "rgba(148,163,184,0.08)",
            padding: "0.5rem 0.75rem",
            borderRadius: 6,
            border: "1px solid rgba(148,163,184,0.15)",
          }}
        >
          {t(lang, "strategy.disclaimer")}
        </p>
      </div>

      {error && <Alert type="error">{error}</Alert>}

      {/* Live Status Badge */}
      {!liveStatusLoading && liveStatus && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.75rem",
            padding: "0.6rem 1rem",
            borderRadius: 8,
            marginBottom: "1rem",
            backgroundColor:
              liveStatus.overall === "PASS"
                ? "rgba(74, 222, 128, 0.08)"
                : liveStatus.overall === "DEGRADED"
                  ? "rgba(251, 191, 36, 0.08)"
                  : "rgba(249, 115, 115, 0.08)",
            border: `1px solid ${
              liveStatus.overall === "PASS"
                ? "rgba(74, 222, 128, 0.25)"
                : liveStatus.overall === "DEGRADED"
                  ? "rgba(251, 191, 36, 0.25)"
                  : "rgba(249, 115, 115, 0.25)"
            }`,
          }}
        >
          <Badge
            color={
              liveStatus.overall === "PASS"
                ? "green"
                : liveStatus.overall === "DEGRADED"
                  ? "yellow"
                  : "red"
            }
          >
            {liveStatus.overall === "PASS"
              ? "LIVE"
              : liveStatus.overall === "DEGRADED"
                ? "DEGRADED"
                : "OFFLINE"}
          </Badge>
          <span style={{ fontSize: "0.82rem", color: "#b7c5dd" }}>
            {liveStatus.checks
              .filter((c) => c.status !== "PASS")
              .map((c) => c.detail)
              .join(" · ") || "All systems operational"}
          </span>
          <span style={{ fontSize: "0.72rem", color: "#64748b", marginLeft: "auto" }}>
            Checked {new Date(liveStatus.checked_at).toLocaleTimeString()}
          </span>
        </div>
      )}

      {/* Strategy Definition Card */}
      <Card
        title={t(lang, "strategy.definition.title")}
        subtitle={t(lang, "strategy.definition.subtitle")}
      >
        {loadingStrategy && (
          <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>Loading…</p>
        )}

        {strategy && (
          <div style={{ fontSize: "0.95rem" }}>
            {/* Header row: name + status badge */}
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                marginBottom: "0.75rem",
              }}
            >
              <h2
                style={{
                  fontSize: "1.4rem",
                  margin: 0,
                  color: "#f0f6ff",
                }}
              >
                {strategy.name}
              </h2>
              <Badge color={strategy.is_active ? "green" : "gray"}>
                {strategy.is_active
                  ? t(lang, "strategy.definition.statusActive")
                  : t(lang, "strategy.definition.statusInactive")}
              </Badge>
            </div>

            {/* Description */}
            <p style={{ marginBottom: "0.6rem" }}>
              <span style={labelStyle}>
                {t(lang, "strategy.definition.descriptionLabel")}:
              </span>
              <span style={valueStyle}>
                {strategy.description || (
                  <span style={{ color: "#7c8ca4" }}>
                    {t(lang, "strategy.definition.noDescription")}
                  </span>
                )}
              </span>
            </p>

            {/* Key info grid */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
                gap: "0.5rem 1.5rem",
                marginBottom: "0.75rem",
              }}
            >
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>
                  {t(lang, "strategy.definition.styleLabel")}:
                </span>
                <span style={valueStyle}>{strategy.style || "—"}</span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>
                  {t(lang, "strategy.definition.symbolsLabel")}:
                </span>
                <span style={valueStyle}>{strategy.symbol_universe || "—"}</span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>
                  {t(lang, "strategy.definition.timeframeLabel")}:
                </span>
                <span style={valueStyle}>{strategy.timeframe || "—"}</span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>
                  {t(lang, "strategy.definition.riskLabel")}:
                </span>
                <span style={valueStyle}>
                  {strategy.risk_per_trade_pct != null
                    ? `${strategy.risk_per_trade_pct}%`
                    : "—"}
                </span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>
                  {t(lang, "strategy.definition.magicLabel")}:
                </span>
                <span style={valueStyle}>{strategy.magic_number ?? "—"}</span>
              </p>
            </div>

            {/* Entry/Exit Logic */}
            {(strategy.entry_logic || strategy.exit_logic) && (
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr",
                  gap: "1rem",
                  marginBottom: "0.75rem",
                }}
              >
                {strategy.entry_logic && (
                  <div>
                    <span
                      style={{
                        ...labelStyle,
                        display: "block",
                        marginBottom: "0.25rem",
                      }}
                    >
                      {t(lang, "strategy.definition.entryLogicLabel")}:
                    </span>
                    <div
                      style={{
                        backgroundColor: "rgba(15,23,42,0.5)",
                        padding: "0.5rem 0.75rem",
                        borderRadius: 6,
                        fontSize: "0.85rem",
                        color: "#e5f4ff",
                        whiteSpace: "pre-wrap",
                        maxHeight: 100,
                        overflow: "auto",
                      }}
                    >
                      {strategy.entry_logic}
                    </div>
                  </div>
                )}
                {strategy.exit_logic && (
                  <div>
                    <span
                      style={{
                        ...labelStyle,
                        display: "block",
                        marginBottom: "0.25rem",
                      }}
                    >
                      {t(lang, "strategy.definition.exitLogicLabel")}:
                    </span>
                    <div
                      style={{
                        backgroundColor: "rgba(15,23,42,0.5)",
                        padding: "0.5rem 0.75rem",
                        borderRadius: 6,
                        fontSize: "0.85rem",
                        color: "#e5f4ff",
                        whiteSpace: "pre-wrap",
                        maxHeight: 100,
                        overflow: "auto",
                      }}
                    >
                      {strategy.exit_logic}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Notes */}
            {strategy.notes && (
              <p style={{ marginBottom: "0.6rem" }}>
                <span style={labelStyle}>
                  {t(lang, "strategy.definition.notesLabel")}:
                </span>
                <span style={{ ...valueStyle, color: "#cbd5f5" }}>
                  {strategy.notes}
                </span>
              </p>
            )}

            {/* Timestamp */}
            <p
              style={{
                fontSize: "0.78rem",
                color: "#7c8ca4",
                marginTop: "0.5rem",
              }}
            >
              {t(lang, "strategy.definition.createdLabel")}:{" "}
              <span style={{ color: "#c9def7" }}>
                {new Date(strategy.created_at).toLocaleString()}
              </span>
            </p>
          </div>
        )}
      </Card>

      {/* Readiness Checklist */}
      <Card
        title={t(lang, "strategy.readiness.title")}
        subtitle={t(lang, "strategy.readiness.subtitle")}
      >
        {!strategy && loadingStrategy && (
          <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>Loading…</p>
        )}

        {strategy && (
          <>
            <div style={{ marginBottom: "1rem" }}>
              {readinessItems.map((item) => (
                <div key={item.key} style={checkItemStyle(item.passed)}>
                  <span style={checkIconStyle(item.passed)}>
                    {item.passed ? "✓" : "×"}
                  </span>
                  <span>{t(lang, item.key)}</span>
                </div>
              ))}
            </div>

            {/* Status indicator */}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "0.75rem",
                padding: "0.75rem 1rem",
                borderRadius: 8,
                backgroundColor: testReady
                  ? "rgba(74, 222, 128, 0.1)"
                  : "rgba(249, 115, 115, 0.1)",
                border: `1px solid ${testReady ? "rgba(74, 222, 128, 0.3)" : "rgba(249, 115, 115, 0.3)"}`,
              }}
            >
              <Badge color={testReady ? "green" : "red"}>
                {testReady
                  ? t(lang, "strategy.readiness.ready")
                  : t(lang, "strategy.readiness.notReady")}
              </Badge>
              <span
                style={{
                  fontSize: "0.85rem",
                  color: testReady ? "#a7f3d0" : "#fca5a5",
                }}
              >
                {testReady
                  ? t(lang, "strategy.readiness.readyHint")
                  : t(lang, "strategy.readiness.notReadyHint")}
              </span>
            </div>
          </>
        )}
      </Card>

      {/* Linked Backtests */}
      <Card
        title={t(lang, "strategy.linkedBacktests.title")}
        subtitle={t(lang, "strategy.linkedBacktests.subtitle")}
      >
        {loadingConfigs && (
          <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
            {t(lang, "strategy.linkedBacktests.loading")}
          </p>
        )}

        {!loadingConfigs && linkedConfigs.length === 0 && (
          <div style={{ textAlign: "center", padding: "1.5rem 0" }}>
            <p
              style={{
                fontSize: "0.95rem",
                color: "#9ca3af",
                marginBottom: "0.5rem",
              }}
            >
              {t(lang, "strategy.linkedBacktests.empty")}
            </p>
            <p style={{ fontSize: "0.85rem", color: "#7c8ca4" }}>
              {t(lang, "strategy.linkedBacktests.emptyHint")}
            </p>
          </div>
        )}

        {!loadingConfigs && linkedConfigs.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
            {linkedConfigs.map((config) => (
              <div
                key={config.id}
                onClick={() => router.push(`/backtests/${config.id}`)}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  padding: "0.75rem 1rem",
                  borderRadius: 8,
                  backgroundColor: "rgba(15,23,42,0.5)",
                  border: "1px solid rgba(148,163,184,0.2)",
                  cursor: "pointer",
                  transition: "background-color 0.15s",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.backgroundColor = "rgba(30,41,59,0.7)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.backgroundColor = "rgba(15,23,42,0.5)";
                }}
              >
                <div>
                  <div
                    style={{
                      fontWeight: 500,
                      color: "#f0f6ff",
                      marginBottom: "0.25rem",
                    }}
                  >
                    {config.name}
                  </div>
                  <div
                    style={{
                      fontSize: "0.8rem",
                      color: "#9db0c9",
                      display: "flex",
                      gap: "1rem",
                      flexWrap: "wrap",
                    }}
                  >
                    <span>
                      {t(lang, "strategy.linkedBacktests.symbolLabel")}{" "}
                      {config.symbol}
                    </span>
                    <span>
                      {t(lang, "strategy.linkedBacktests.timeframeLabel")}{" "}
                      {config.timeframe}
                    </span>
                    <span>
                      {t(lang, "strategy.linkedBacktests.periodLabel")}{" "}
                      {config.date_from} → {config.date_to}
                    </span>
                  </div>
                </div>
                <span style={{ color: "#60a5fa", fontSize: "0.85rem" }}>
                  {t(lang, "strategy.linkedBacktests.viewConfig")}
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Actions */}
      <Card>
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "0.75rem",
          }}
        >
          {/* Inline warning if not ready */}
          {!testReady && strategy && (
            <p
              style={{
                margin: 0,
                fontSize: "0.82rem",
                color: "#fbbf24",
                backgroundColor: "rgba(251, 191, 36, 0.08)",
                padding: "0.5rem 0.75rem",
                borderRadius: 6,
                border: "1px solid rgba(251, 191, 36, 0.2)",
              }}
            >
              {t(lang, "strategy.testWarningInline")}
            </p>
          )}

          <div
            style={{
              display: "flex",
              gap: "1rem",
              alignItems: "center",
              flexWrap: "wrap",
            }}
          >
            {/* Create Backtest CTA - ALWAYS enabled */}
            <Button
              variant="primary"
              onClick={() =>
                router.push(
                  `/backtests?create=true&strategy=${strategyId}${!testReady ? "&incomplete=true" : ""}`
                )
              }
            >
              {t(lang, "strategy.actions.createBacktest")}
            </Button>

            {/* Edit Strategy */}
            <Button
              variant="secondary"
              onClick={() => router.push(`/strategies/${strategyId}/edit`)}
            >
              {t(lang, "strategy.actions.editStrategy")}
            </Button>

            {/* Open strategy builder */}
            <Button
              variant="secondary"
              onClick={() => router.push("/strategies/create")}
            >
              {t(lang, "strategy.actions.openBuilder")}
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
