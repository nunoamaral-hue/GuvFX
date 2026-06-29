"use client";

import React, { useMemo } from "react";
import type { Lang } from "@/lib/i18n";
import { t } from "@/lib/i18n";

type EquityPoint = { timestamp?: string; equity: number } | number;

interface LossObservationsProps {
  equityCurve: EquityPoint[];
  lang: Lang;
}

type Observation = {
  key: string;
  severity: "info" | "warning" | "neutral";
};

/**
 * LossObservations — Heuristic tagging of loss patterns.
 * Compliance-safe: observational only, no advice, no conclusions.
 * Short bullet points with clear disclaimer.
 */
export function LossObservations({ equityCurve, lang }: LossObservationsProps) {
  const observations = useMemo((): Observation[] => {
    if (!equityCurve || equityCurve.length < 5) return [];

    const values = equityCurve.map((p) => (typeof p === "number" ? p : p.equity));
    const results: Observation[] = [];

    // Calculate period-over-period changes
    const changes: number[] = [];
    for (let i = 1; i < values.length; i++) {
      changes.push(values[i] - values[i - 1]);
    }

    const losses = changes.filter((c) => c < 0);
    const gains = changes.filter((c) => c > 0);
    const totalPeriods = changes.length;

    // 1. Loss frequency analysis
    const lossRatio = losses.length / totalPeriods;
    if (lossRatio > 0.6) {
      results.push({ key: "backtests.run.obsHighLossFrequency", severity: "warning" });
    } else if (lossRatio < 0.3 && losses.length > 0) {
      results.push({ key: "backtests.run.obsLowLossFrequency", severity: "info" });
    }

    // 2. Loss clustering (consecutive losses)
    let currentStreak = 0;
    let maxStreak = 0;
    let clusterCount = 0;

    for (const change of changes) {
      if (change < 0) {
        currentStreak++;
        if (currentStreak > maxStreak) maxStreak = currentStreak;
      } else {
        if (currentStreak >= 3) clusterCount++;
        currentStreak = 0;
      }
    }
    if (currentStreak >= 3) clusterCount++;

    if (maxStreak >= 5) {
      results.push({ key: "backtests.run.obsExtendedLossStreak", severity: "warning" });
    } else if (clusterCount >= 2) {
      results.push({ key: "backtests.run.obsLossClustering", severity: "warning" });
    } else if (maxStreak <= 2 && losses.length > 3) {
      results.push({ key: "backtests.run.obsLossesDistributed", severity: "info" });
    }

    // 3. Drawdown analysis
    let runningMax = values[0];
    let currentDDStart = 0;
    let maxDDDuration = 0;
    let inDrawdown = false;

    for (let i = 0; i < values.length; i++) {
      if (values[i] > runningMax) {
        if (inDrawdown) {
          const duration = i - currentDDStart;
          if (duration > maxDDDuration) maxDDDuration = duration;
        }
        runningMax = values[i];
        inDrawdown = false;
      } else if (values[i] < runningMax) {
        if (!inDrawdown) {
          inDrawdown = true;
          currentDDStart = i;
        }
      }
    }
    // Check if still in drawdown at end
    if (inDrawdown) {
      const duration = values.length - currentDDStart;
      if (duration > maxDDDuration) maxDDDuration = duration;
    }

    const ddDurationRatio = maxDDDuration / totalPeriods;
    if (ddDurationRatio > 0.5) {
      results.push({ key: "backtests.run.obsExtendedDrawdownPhase", severity: "warning" });
    } else if (ddDurationRatio > 0.25) {
      results.push({ key: "backtests.run.obsModerateDrawdownPhase", severity: "neutral" });
    }

    // 4. Loss magnitude vs gain magnitude
    if (losses.length > 0 && gains.length > 0) {
      const avgLoss = Math.abs(losses.reduce((a, b) => a + b, 0) / losses.length);
      const avgGain = gains.reduce((a, b) => a + b, 0) / gains.length;

      if (avgLoss > avgGain * 1.5) {
        results.push({ key: "backtests.run.obsLargeLossMagnitude", severity: "warning" });
      } else if (avgLoss < avgGain * 0.5) {
        results.push({ key: "backtests.run.obsSmallLossMagnitude", severity: "info" });
      }
    }

    // 5. Recovery patterns
    let quickRecoveries = 0;
    let slowRecoveries = 0;
    let ddStart = -1;

    runningMax = values[0];
    for (let i = 0; i < values.length; i++) {
      if (values[i] >= runningMax) {
        if (ddStart !== -1) {
          const recoveryTime = i - ddStart;
          if (recoveryTime <= 3) quickRecoveries++;
          else slowRecoveries++;
          ddStart = -1;
        }
        runningMax = values[i];
      } else if (ddStart === -1) {
        ddStart = i;
      }
    }

    if (slowRecoveries > quickRecoveries && slowRecoveries >= 2) {
      results.push({ key: "backtests.run.obsSlowRecovery", severity: "neutral" });
    }

    return results.slice(0, 4); // Limit to 4 observations
  }, [equityCurve]);

  if (observations.length === 0) {
    return null;
  }

  const severityColors = {
    info: "#22c55e",
    warning: "#f59e0b",
    neutral: "#64748b",
  };

  return (
    <div
      style={{
        padding: "0.75rem",
        background: "rgba(15, 23, 42, 0.6)",
        borderRadius: 6,
        border: "1px solid #1e293b",
      }}
    >
      {/* Title */}
      <h4
        style={{
          fontSize: "0.85rem",
          color: "#e5f4ff",
          margin: "0 0 0.5rem 0",
          fontWeight: 500,
        }}
      >
        {t(lang, "backtests.run.lossObservationsTitle")}
      </h4>

      {/* Observation bullets */}
      <ul
        style={{
          margin: 0,
          padding: 0,
          listStyle: "none",
          display: "flex",
          flexDirection: "column",
          gap: "0.35rem",
        }}
      >
        {observations.map((obs, i) => (
          <li
            key={i}
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: "0.5rem",
              fontSize: "0.8rem",
              color: "#c9d7f2",
            }}
          >
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: "50%",
                background: severityColors[obs.severity],
                marginTop: "0.35rem",
                flexShrink: 0,
              }}
            />
            <span>{t(lang, obs.key)}</span>
          </li>
        ))}
      </ul>

      {/* Disclaimer */}
      <p
        style={{
          fontSize: "0.68rem",
          color: "#64748b",
          margin: "0.5rem 0 0 0",
          fontStyle: "italic",
          lineHeight: 1.4,
        }}
      >
        {t(lang, "backtests.run.lossObservationsDisclaimer")}
      </p>
    </div>
  );
}
