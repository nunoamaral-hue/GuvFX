"use client";

import React, { useMemo } from "react";
import type { Lang } from "@/lib/i18n";
import { t } from "@/lib/i18n";

type EquityPoint = { timestamp?: string; equity: number } | number;

interface DrawdownTimelineProps {
  equityCurve: EquityPoint[];
  lang: Lang;
  width?: number;
  height?: number;
}

type DrawdownPeriod = {
  startIdx: number;
  endIdx: number;
  peakValue: number;
  troughValue: number;
  drawdownPct: number;
  recoveryIdx: number | null;
};

/**
 * DrawdownTimeline — Full visualization of drawdown periods.
 * Shows underwater periods as shaded regions with depth indication.
 * Compliance-safe: observational language only.
 */
export function DrawdownTimeline({
  equityCurve,
  lang,
  width = 600,
  height = 180,
}: DrawdownTimelineProps) {
  const { drawdowns, maxDrawdown, periods } = useMemo(() => {
    if (!equityCurve || equityCurve.length < 2) {
      return { drawdowns: [], maxDrawdown: 0, periods: [] };
    }

    const values = equityCurve.map((p) =>
      typeof p === "number" ? p : p.equity
    );

    let runningMax = values[0];
    const drawdowns: number[] = [];
    const periods: DrawdownPeriod[] = [];
    let currentPeriod: Partial<DrawdownPeriod> | null = null;

    for (let i = 0; i < values.length; i++) {
      const val = values[i];

      if (val > runningMax) {
        // New high — close any open period
        if (currentPeriod && currentPeriod.startIdx !== undefined) {
          currentPeriod.recoveryIdx = i;
          periods.push(currentPeriod as DrawdownPeriod);
          currentPeriod = null;
        }
        runningMax = val;
      }

      const dd = runningMax > 0 ? ((runningMax - val) / runningMax) * 100 : 0;
      drawdowns.push(dd);

      // Track drawdown periods
      if (dd > 0 && !currentPeriod) {
        currentPeriod = {
          startIdx: i,
          peakValue: runningMax,
          troughValue: val,
          drawdownPct: dd,
          endIdx: i,
          recoveryIdx: null,
        };
      } else if (dd > 0 && currentPeriod) {
        currentPeriod.endIdx = i;
        if (dd > (currentPeriod.drawdownPct ?? 0)) {
          currentPeriod.drawdownPct = dd;
          currentPeriod.troughValue = val;
        }
      }
    }

    // Close any unclosed period
    if (currentPeriod && currentPeriod.startIdx !== undefined) {
      periods.push(currentPeriod as DrawdownPeriod);
    }

    return {
      drawdowns,
      maxDrawdown: Math.max(...drawdowns, 0),
      periods: periods.filter((p) => p.drawdownPct > 1), // Only significant periods (>1%)
    };
  }, [equityCurve]);

  if (drawdowns.length < 2) {
    return (
      <div
        style={{
          width: "100%",
          height,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: "0.85rem",
          color: "#64748b",
          border: "1px dashed #333a4d",
          borderRadius: 6,
        }}
      >
        {t(lang, "backtests.diagnostics.noEquityData")}
      </div>
    );
  }

  const padding = { top: 24, right: 16, bottom: 32, left: 48 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const effectiveMax = Math.max(maxDrawdown, 5); // At least 5% scale

  // Generate path
  const points = drawdowns.map((dd, i) => {
    const x = padding.left + (i / (drawdowns.length - 1)) * chartWidth;
    const y = padding.top + (dd / effectiveMax) * chartHeight;
    return { x, y, dd };
  });

  const linePath = `M${points.map((p) => `${p.x},${p.y}`).join(" L")}`;
  const areaPath = `M${padding.left},${padding.top} L${points
    .map((p) => `${p.x},${p.y}`)
    .join(" L")} L${padding.left + chartWidth},${padding.top} Z`;

  // Y-axis labels
  const yLabels = [0, effectiveMax / 2, effectiveMax].map((val) => ({
    val,
    y: padding.top + (val / effectiveMax) * chartHeight,
  }));

  return (
    <div style={{ width: "100%" }}>
      {/* Chart title */}
      <div
        style={{
          fontSize: "0.82rem",
          color: "#9ca3af",
          marginBottom: "0.5rem",
        }}
      >
        {t(lang, "backtests.diagnostics.drawdownTimelineTitle")}
      </div>

      <svg
        width="100%"
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="xMidYMid meet"
        style={{ display: "block", maxWidth: "100%" }}
      >
        {/* Grid lines */}
        {yLabels.map((label, i) => (
          <g key={i}>
            <line
              x1={padding.left}
              y1={label.y}
              x2={padding.left + chartWidth}
              y2={label.y}
              stroke="#1e293b"
              strokeWidth={1}
            />
            <text
              x={padding.left - 8}
              y={label.y + 4}
              textAnchor="end"
              fill="#64748b"
              fontSize="10"
            >
              {label.val.toFixed(0)}%
            </text>
          </g>
        ))}

        {/* Highlight significant drawdown periods */}
        {periods.slice(0, 3).map((period, i) => {
          const x1 =
            padding.left + (period.startIdx / (drawdowns.length - 1)) * chartWidth;
          const x2 =
            padding.left +
            ((period.recoveryIdx ?? period.endIdx) / (drawdowns.length - 1)) *
              chartWidth;
          return (
            <rect
              key={i}
              x={x1}
              y={padding.top}
              width={Math.max(x2 - x1, 2)}
              height={chartHeight}
              fill="rgba(239, 68, 68, 0.08)"
            />
          );
        })}

        {/* Fill area */}
        <path d={areaPath} fill="rgba(239, 68, 68, 0.2)" />

        {/* Line */}
        <path
          d={linePath}
          fill="none"
          stroke="#ef4444"
          strokeWidth={2}
          strokeLinejoin="round"
        />

        {/* Zero line */}
        <line
          x1={padding.left}
          y1={padding.top}
          x2={padding.left + chartWidth}
          y2={padding.top}
          stroke="#334155"
          strokeWidth={1}
        />

        {/* X-axis label */}
        <text
          x={padding.left + chartWidth / 2}
          y={height - 8}
          textAnchor="middle"
          fill="#64748b"
          fontSize="10"
        >
          {t(lang, "backtests.diagnostics.timeAxis")}
        </text>

        {/* Y-axis label */}
        <text
          x={12}
          y={padding.top + chartHeight / 2}
          textAnchor="middle"
          fill="#64748b"
          fontSize="10"
          transform={`rotate(-90, 12, ${padding.top + chartHeight / 2})`}
        >
          {t(lang, "backtests.diagnostics.drawdownAxis")}
        </text>
      </svg>

      {/* Period annotations */}
      {periods.length > 0 && (
        <div
          style={{
            marginTop: "0.5rem",
            fontSize: "0.78rem",
            color: "#9ca3af",
          }}
        >
          <span style={{ color: "#64748b" }}>
            {t(lang, "backtests.diagnostics.significantPeriods")}:
          </span>{" "}
          {periods.slice(0, 3).map((p, i) => (
            <span key={i} style={{ marginRight: "0.75rem" }}>
              <span style={{ color: "#ef4444" }}>
                {p.drawdownPct.toFixed(1)}%
              </span>
              {p.recoveryIdx ? (
                <span style={{ color: "#64748b" }}> (recovered)</span>
              ) : (
                <span style={{ color: "#f59e0b" }}> (open)</span>
              )}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
