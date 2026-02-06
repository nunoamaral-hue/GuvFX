"use client";

import React from "react";
import type { Lang } from "@/lib/i18n";
import { t } from "@/lib/i18n";
import { EquityCurveChart } from "./EquityCurveChart";
import { DrawdownTimeline } from "./DrawdownTimeline";
import { LossObservations } from "./LossObservations";
import { SessionHeatmap } from "./SessionHeatmap";

type EquityPoint = { timestamp?: string; equity: number } | number;

interface RunDetailPanelProps {
  equityCurve: EquityPoint[];
  maxDrawdownPct?: number;
  totalReturnPct?: number;
  observedHitRatePct?: number;
  totalTrades?: number;
  lang: Lang;
}

/**
 * RunDetailPanel — Expandable panel for run-level inspection.
 * Charts-first layout with observational metrics.
 * Compliance-safe: no performance claims.
 */
export function RunDetailPanel({
  equityCurve,
  maxDrawdownPct,
  totalReturnPct,
  observedHitRatePct,
  totalTrades,
  lang,
}: RunDetailPanelProps) {
  if (!equityCurve || equityCurve.length < 3) {
    return (
      <div
        style={{
          padding: "1rem",
          textAlign: "center",
          color: "#64748b",
          fontSize: "0.85rem",
          border: "1px dashed #333a4d",
          borderRadius: 6,
          background: "rgba(15, 23, 42, 0.4)",
        }}
      >
        {t(lang, "backtests.run.noDataForRun")}
      </div>
    );
  }

  // Check if we have timestamp data for session analysis
  const hasTimestamps = equityCurve.some(
    (p) => typeof p === "object" && "timestamp" in p && p.timestamp
  );

  // Normalize for session heatmap
  const normalizedCurve = equityCurve.map((p) =>
    typeof p === "number" ? { equity: p } : p
  ) as Array<{ timestamp?: string; equity: number }>;

  // Calculate longest drawdown duration
  const values = equityCurve.map((p) => (typeof p === "number" ? p : p.equity));
  let runningMax = values[0];
  let maxDDDuration = 0;
  let currentDDStart = -1;

  for (let i = 0; i < values.length; i++) {
    if (values[i] >= runningMax) {
      if (currentDDStart !== -1) {
        const duration = i - currentDDStart;
        if (duration > maxDDDuration) maxDDDuration = duration;
      }
      runningMax = values[i];
      currentDDStart = -1;
    } else if (currentDDStart === -1) {
      currentDDStart = i;
    }
  }
  if (currentDDStart !== -1) {
    const duration = values.length - currentDDStart;
    if (duration > maxDDDuration) maxDDDuration = duration;
  }

  const labelStyle: React.CSSProperties = {
    fontSize: "0.72rem",
    color: "#9ca3af",
    marginBottom: "0.15rem",
  };

  const valueStyle: React.CSSProperties = {
    fontSize: "1rem",
    fontWeight: 500,
    color: "#e5f4ff",
  };

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "1rem",
        padding: "0.75rem",
        background: "rgba(7, 12, 30, 0.7)",
        borderRadius: 8,
        border: "1px solid #1e293b",
      }}
    >
      {/* Charts section - TOP priority */}
      <div>
        <h4
          style={{
            fontSize: "0.82rem",
            color: "#9ca3af",
            margin: "0 0 0.5rem 0",
            fontWeight: 400,
          }}
        >
          {t(lang, "backtests.run.chartsTitle")}
        </h4>

        {/* Equity curve with drawdown overlay */}
        <div
          style={{
            padding: "0.5rem",
            background: "rgba(15, 23, 42, 0.5)",
            borderRadius: 6,
            marginBottom: "0.75rem",
          }}
        >
          <EquityCurveChart
            equityCurve={equityCurve}
            lang={lang}
            showDrawdownOverlay
          />
        </div>

        {/* Drawdown timeline */}
        <div
          style={{
            padding: "0.5rem",
            background: "rgba(15, 23, 42, 0.5)",
            borderRadius: 6,
          }}
        >
          <DrawdownTimeline equityCurve={equityCurve} lang={lang} height={140} />
        </div>
      </div>

      {/* Observational metrics - SECONDARY (below charts) */}
      <div>
        <h4
          style={{
            fontSize: "0.82rem",
            color: "#9ca3af",
            margin: "0 0 0.5rem 0",
            fontWeight: 400,
          }}
        >
          {t(lang, "backtests.run.observedMetricsTitle")}
        </h4>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))",
            gap: "0.75rem",
          }}
        >
          {/* Observed return */}
          <div
            style={{
              padding: "0.5rem 0.75rem",
              background: "rgba(15, 23, 42, 0.5)",
              borderRadius: 6,
              border: "1px solid #1e293b",
            }}
          >
            <div style={labelStyle}>{t(lang, "backtests.observedReturn")}</div>
            <div
              style={{
                ...valueStyle,
                color:
                  typeof totalReturnPct === "number"
                    ? totalReturnPct >= 0
                      ? "#4ade80"
                      : "#f87171"
                    : "#e5f4ff",
              }}
            >
              {typeof totalReturnPct === "number"
                ? `${totalReturnPct >= 0 ? "+" : ""}${totalReturnPct.toFixed(2)}%`
                : "—"}
            </div>
          </div>

          {/* Max drawdown */}
          <div
            style={{
              padding: "0.5rem 0.75rem",
              background: "rgba(239, 68, 68, 0.08)",
              borderRadius: 6,
              border: "1px solid rgba(239, 68, 68, 0.2)",
            }}
          >
            <div style={labelStyle}>{t(lang, "backtests.maxDrawdown")}</div>
            <div style={{ ...valueStyle, color: "#f87171" }}>
              {typeof maxDrawdownPct === "number"
                ? `${maxDrawdownPct.toFixed(2)}%`
                : "—"}
            </div>
          </div>

          {/* Observed hit rate */}
          <div
            style={{
              padding: "0.5rem 0.75rem",
              background: "rgba(15, 23, 42, 0.5)",
              borderRadius: 6,
              border: "1px solid #1e293b",
            }}
          >
            <div style={labelStyle}>{t(lang, "backtests.observedWinRate")}</div>
            <div style={valueStyle}>
              {typeof observedHitRatePct === "number"
                ? `${observedHitRatePct.toFixed(1)}%`
                : "—"}
            </div>
          </div>

          {/* Longest DD duration */}
          <div
            style={{
              padding: "0.5rem 0.75rem",
              background: "rgba(15, 23, 42, 0.5)",
              borderRadius: 6,
              border: "1px solid #1e293b",
            }}
          >
            <div style={labelStyle}>{t(lang, "backtests.run.longestDDDuration")}</div>
            <div style={valueStyle}>
              {maxDDDuration > 0
                ? `${maxDDDuration} ${t(lang, "backtests.run.periods")}`
                : "—"}
            </div>
          </div>

          {/* Total trades (if available) */}
          {typeof totalTrades === "number" && (
            <div
              style={{
                padding: "0.5rem 0.75rem",
                background: "rgba(15, 23, 42, 0.5)",
                borderRadius: 6,
                border: "1px solid #1e293b",
              }}
            >
              <div style={labelStyle}>{t(lang, "backtests.run.totalTrades")}</div>
              <div style={valueStyle}>{totalTrades}</div>
            </div>
          )}
        </div>

        {/* Disclaimer for metrics */}
        <p
          style={{
            fontSize: "0.68rem",
            color: "#64748b",
            margin: "0.5rem 0 0 0",
            fontStyle: "italic",
          }}
        >
          {t(lang, "backtests.run.metricsDisclaimer")}
        </p>
      </div>

      {/* Loss observations (heuristic) */}
      <LossObservations equityCurve={equityCurve} lang={lang} />

      {/* Session heatmap (if timestamps available) */}
      {hasTimestamps && (
        <div
          style={{
            padding: "0.5rem",
            background: "rgba(15, 23, 42, 0.5)",
            borderRadius: 6,
          }}
        >
          <SessionHeatmap equityCurve={normalizedCurve} lang={lang} />
        </div>
      )}
    </div>
  );
}
