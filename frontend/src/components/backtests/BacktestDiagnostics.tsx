"use client";

import React from "react";
import type { Lang } from "@/lib/i18n";
import { t } from "@/lib/i18n";
import { DrawdownTimeline } from "./DrawdownTimeline";
import { SessionHeatmap } from "./SessionHeatmap";
import { LossClusteringBadge } from "./LossClusteringBadge";

type EquityPoint = { timestamp?: string; equity: number } | number;

interface BacktestDiagnosticsProps {
  equityCurve: EquityPoint[];
  maxDrawdownPct?: number;
  lang: Lang;
}

/**
 * BacktestDiagnostics — Container component for loss-focused diagnostic visualizations.
 * Integrates: DrawdownTimeline, SessionHeatmap, LossClusteringBadge.
 *
 * Compliance-safe: All visualizations are observational/diagnostic.
 * No performance claims, no guarantees, no profit framing.
 */
export function BacktestDiagnostics({
  equityCurve,
  maxDrawdownPct,
  lang,
}: BacktestDiagnosticsProps) {
  if (!equityCurve || equityCurve.length < 2) {
    return (
      <div
        style={{
          padding: "1rem",
          textAlign: "center",
          color: "#64748b",
          fontSize: "0.85rem",
          border: "1px dashed #333a4d",
          borderRadius: 8,
        }}
      >
        {t(lang, "backtests.diagnostics.noDataAvailable")}
      </div>
    );
  }

  // Check if we have timestamp data for session analysis
  const hasTimestamps = equityCurve.some(
    (p) => typeof p === "object" && "timestamp" in p && p.timestamp
  );

  // Normalize to EquityPoint format for session heatmap
  const normalizedCurve = equityCurve.map((p) =>
    typeof p === "number" ? { equity: p } : p
  ) as Array<{ timestamp?: string; equity: number }>;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "1.25rem",
      }}
    >
      {/* Section header */}
      <div>
        <h3
          style={{
            fontSize: "1rem",
            color: "#e5f4ff",
            margin: 0,
            marginBottom: "0.25rem",
          }}
        >
          {t(lang, "backtests.diagnostics.title")}
        </h3>
        <p
          style={{
            fontSize: "0.78rem",
            color: "#64748b",
            margin: 0,
          }}
        >
          {t(lang, "backtests.diagnostics.subtitle")}
        </p>
      </div>

      {/* Top row: Max DD + Clustering badge */}
      <div
        style={{
          display: "flex",
          gap: "1rem",
          flexWrap: "wrap",
        }}
      >
        {/* Max drawdown highlight */}
        {typeof maxDrawdownPct === "number" && (
          <div
            style={{
              padding: "0.6rem 1rem",
              borderRadius: 6,
              background: "rgba(239, 68, 68, 0.1)",
              border: "1px solid rgba(239, 68, 68, 0.3)",
              minWidth: 140,
            }}
          >
            <div
              style={{
                fontSize: "0.72rem",
                color: "#9ca3af",
                marginBottom: "0.15rem",
              }}
            >
              {t(lang, "backtests.maxDrawdown")}
            </div>
            <div
              style={{
                fontSize: "1.25rem",
                fontWeight: 600,
                color: "#f87171",
              }}
            >
              {maxDrawdownPct.toFixed(2)}%
            </div>
          </div>
        )}

        {/* Loss clustering indicator */}
        <div style={{ flex: 1, minWidth: 200 }}>
          <LossClusteringBadge equityCurve={equityCurve} lang={lang} />
        </div>
      </div>

      {/* Drawdown timeline chart */}
      <div
        style={{
          padding: "0.75rem",
          background: "rgba(7, 12, 30, 0.6)",
          borderRadius: 8,
          border: "1px solid #1e293b",
        }}
      >
        <DrawdownTimeline equityCurve={equityCurve} lang={lang} />
      </div>

      {/* Session heatmap (only if timestamps available) */}
      {hasTimestamps && (
        <div
          style={{
            padding: "0.75rem",
            background: "rgba(7, 12, 30, 0.6)",
            borderRadius: 8,
            border: "1px solid #1e293b",
          }}
        >
          <SessionHeatmap equityCurve={normalizedCurve} lang={lang} />
        </div>
      )}

      {/* Diagnostic disclaimer */}
      <div
        style={{
          fontSize: "0.72rem",
          color: "#64748b",
          padding: "0.5rem 0.75rem",
          background: "rgba(100, 116, 139, 0.08)",
          borderRadius: 4,
          lineHeight: 1.5,
        }}
      >
        {t(lang, "backtests.diagnostics.disclaimer")}
      </div>
    </div>
  );
}
