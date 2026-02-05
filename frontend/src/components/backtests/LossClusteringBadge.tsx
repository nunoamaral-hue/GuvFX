"use client";

import React, { useMemo } from "react";
import type { Lang } from "@/lib/i18n";
import { t } from "@/lib/i18n";

type EquityPoint = { timestamp?: string; equity: number } | number;

interface LossClusteringBadgeProps {
  equityCurve: EquityPoint[];
  lang: Lang;
  compact?: boolean;
}

type ClusteringAnalysis = {
  hasConcentration: boolean;
  longestStreak: number;
  clusterCount: number;
  severity: "low" | "medium" | "high";
};

/**
 * LossClusteringBadge — Visual indicator of loss concentration patterns.
 * Helps users identify whether losses cluster together (streaks) or are distributed.
 * Compliance-safe: observational/diagnostic only, no performance claims.
 */
export function LossClusteringBadge({
  equityCurve,
  lang,
  compact = false,
}: LossClusteringBadgeProps) {
  const analysis = useMemo((): ClusteringAnalysis | null => {
    if (!equityCurve || equityCurve.length < 3) return null;

    const values = equityCurve.map((p) =>
      typeof p === "number" ? p : p.equity
    );

    // Calculate period-over-period changes
    const changes: number[] = [];
    for (let i = 1; i < values.length; i++) {
      changes.push(values[i] - values[i - 1]);
    }

    // Identify loss streaks (consecutive negative changes)
    let currentStreak = 0;
    let longestStreak = 0;
    let clusterCount = 0;

    for (const change of changes) {
      if (change < 0) {
        currentStreak++;
        if (currentStreak > longestStreak) {
          longestStreak = currentStreak;
        }
      } else {
        if (currentStreak >= 3) {
          clusterCount++;
        }
        currentStreak = 0;
      }
    }
    // Check final streak
    if (currentStreak >= 3) {
      clusterCount++;
    }

    // Determine severity based on streak length and cluster count
    const hasConcentration = longestStreak >= 3 || clusterCount >= 2;
    let severity: "low" | "medium" | "high" = "low";

    if (longestStreak >= 7 || clusterCount >= 4) {
      severity = "high";
    } else if (longestStreak >= 5 || clusterCount >= 2) {
      severity = "medium";
    }

    return {
      hasConcentration,
      longestStreak,
      clusterCount,
      severity,
    };
  }, [equityCurve]);

  if (!analysis) {
    return null;
  }

  const severityColors = {
    low: { bg: "rgba(34, 197, 94, 0.12)", border: "#22c55e", text: "#4ade80" },
    medium: { bg: "rgba(245, 158, 11, 0.12)", border: "#f59e0b", text: "#fbbf24" },
    high: { bg: "rgba(239, 68, 68, 0.12)", border: "#ef4444", text: "#f87171" },
  };

  const colors = severityColors[analysis.severity];
  const labelKey = analysis.hasConcentration
    ? `backtests.diagnostics.clustering${analysis.severity.charAt(0).toUpperCase() + analysis.severity.slice(1)}`
    : "backtests.diagnostics.clusteringDistributed";

  if (compact) {
    return (
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "0.25rem",
          padding: "0.15rem 0.4rem",
          fontSize: "0.7rem",
          borderRadius: 4,
          background: colors.bg,
          border: `1px solid ${colors.border}`,
          color: colors.text,
        }}
        title={t(lang, labelKey)}
      >
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: colors.text,
          }}
        />
        {analysis.severity === "low"
          ? "—"
          : analysis.longestStreak > 0
          ? `${analysis.longestStreak}×`
          : ""}
      </span>
    );
  }

  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: "0.5rem",
        padding: "0.5rem 0.75rem",
        borderRadius: 6,
        background: colors.bg,
        border: `1px solid ${colors.border}`,
      }}
    >
      {/* Severity indicator */}
      <div
        style={{
          width: 10,
          height: 10,
          borderRadius: "50%",
          background: colors.text,
          marginTop: "0.2rem",
          flexShrink: 0,
        }}
      />

      <div style={{ flex: 1 }}>
        <div
          style={{
            fontSize: "0.8rem",
            fontWeight: 500,
            color: colors.text,
            marginBottom: "0.15rem",
          }}
        >
          {t(lang, labelKey)}
        </div>
        <div style={{ fontSize: "0.75rem", color: "#9ca3af" }}>
          {analysis.longestStreak > 0 && (
            <span style={{ marginRight: "0.75rem" }}>
              {t(lang, "backtests.diagnostics.longestStreak")}:{" "}
              <span style={{ color: "#e5f4ff" }}>{analysis.longestStreak}</span>
            </span>
          )}
          {analysis.clusterCount > 0 && (
            <span>
              {t(lang, "backtests.diagnostics.clusterCount")}:{" "}
              <span style={{ color: "#e5f4ff" }}>{analysis.clusterCount}</span>
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
