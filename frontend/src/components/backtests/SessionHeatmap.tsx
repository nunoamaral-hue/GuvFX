"use client";

import React, { useMemo } from "react";
import type { Lang } from "@/lib/i18n";
import { t } from "@/lib/i18n";

type EquityPoint = { timestamp?: string; equity: number };

interface SessionHeatmapProps {
  equityCurve: EquityPoint[];
  lang: Lang;
}

type SessionBucket = {
  name: string;
  labelKey: string;
  hourRange: [number, number]; // UTC hours
  totalChange: number;
  count: number;
  avgChange: number;
};

/**
 * SessionHeatmap — Visual breakdown of equity changes by trading session.
 * Shows which sessions (Tokyo, London, NY) had more losses.
 * Compliance-safe: observational only, helps user understand behavior patterns.
 */
export function SessionHeatmap({ equityCurve, lang }: SessionHeatmapProps) {
  const sessions = useMemo((): SessionBucket[] | null => {
    // Need timestamps to bucket by session
    const pointsWithTime = equityCurve.filter(
      (p): p is EquityPoint & { timestamp: string } =>
        typeof p === "object" && !!p.timestamp
    );

    if (pointsWithTime.length < 3) return null;

    // Define session buckets (UTC hours)
    const buckets: SessionBucket[] = [
      {
        name: "Tokyo",
        labelKey: "backtests.diagnostics.sessionTokyo",
        hourRange: [0, 8],
        totalChange: 0,
        count: 0,
        avgChange: 0,
      },
      {
        name: "London",
        labelKey: "backtests.diagnostics.sessionLondon",
        hourRange: [8, 16],
        totalChange: 0,
        count: 0,
        avgChange: 0,
      },
      {
        name: "New York",
        labelKey: "backtests.diagnostics.sessionNewYork",
        hourRange: [16, 24],
        totalChange: 0,
        count: 0,
        avgChange: 0,
      },
    ];

    // Calculate changes and bucket by hour
    for (let i = 1; i < pointsWithTime.length; i++) {
      const change = pointsWithTime[i].equity - pointsWithTime[i - 1].equity;
      const date = new Date(pointsWithTime[i].timestamp);
      const hour = date.getUTCHours();

      for (const bucket of buckets) {
        if (hour >= bucket.hourRange[0] && hour < bucket.hourRange[1]) {
          bucket.totalChange += change;
          bucket.count++;
          break;
        }
      }
    }

    // Calculate averages
    for (const bucket of buckets) {
      bucket.avgChange = bucket.count > 0 ? bucket.totalChange / bucket.count : 0;
    }

    return buckets;
  }, [equityCurve]);

  if (!sessions) {
    return (
      <div
        style={{
          padding: "0.75rem",
          fontSize: "0.8rem",
          color: "#64748b",
          border: "1px dashed #333a4d",
          borderRadius: 6,
          textAlign: "center",
        }}
      >
        {t(lang, "backtests.diagnostics.noSessionData")}
      </div>
    );
  }

  // Find min/max for color scaling
  const changes = sessions.map((s) => s.totalChange);
  const maxAbs = Math.max(Math.abs(Math.min(...changes)), Math.abs(Math.max(...changes)), 1);

  const getColor = (val: number) => {
    const intensity = Math.min(Math.abs(val) / maxAbs, 1);
    if (val < 0) {
      // Red for losses
      return `rgba(239, 68, 68, ${0.15 + intensity * 0.4})`;
    } else if (val > 0) {
      // Green for gains
      return `rgba(34, 197, 94, ${0.1 + intensity * 0.3})`;
    }
    return "rgba(100, 116, 139, 0.1)";
  };

  return (
    <div>
      {/* Title */}
      <div
        style={{
          fontSize: "0.82rem",
          color: "#9ca3af",
          marginBottom: "0.5rem",
        }}
      >
        {t(lang, "backtests.diagnostics.sessionBreakdownTitle")}
      </div>

      {/* Session bars */}
      <div
        style={{
          display: "flex",
          gap: "0.5rem",
        }}
      >
        {sessions.map((session) => (
          <div
            key={session.name}
            style={{
              flex: 1,
              padding: "0.6rem 0.5rem",
              borderRadius: 6,
              background: getColor(session.totalChange),
              border: "1px solid #1e293b",
              textAlign: "center",
            }}
          >
            <div
              style={{
                fontSize: "0.75rem",
                color: "#9ca3af",
                marginBottom: "0.25rem",
              }}
            >
              {t(lang, session.labelKey)}
            </div>
            <div
              style={{
                fontSize: "0.9rem",
                fontWeight: 500,
                color:
                  session.totalChange < 0
                    ? "#f87171"
                    : session.totalChange > 0
                    ? "#4ade80"
                    : "#94a3b8",
              }}
            >
              {session.totalChange >= 0 ? "+" : ""}
              {session.totalChange.toFixed(0)}
            </div>
            <div
              style={{
                fontSize: "0.68rem",
                color: "#64748b",
                marginTop: "0.15rem",
              }}
            >
              {session.count} {t(lang, "backtests.diagnostics.periods")}
            </div>
          </div>
        ))}
      </div>

      {/* Disclaimer */}
      <div
        style={{
          fontSize: "0.7rem",
          color: "#64748b",
          marginTop: "0.5rem",
          fontStyle: "italic",
        }}
      >
        {t(lang, "backtests.diagnostics.sessionDisclaimer")}
      </div>
    </div>
  );
}
