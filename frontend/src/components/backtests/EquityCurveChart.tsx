"use client";

import React, { useMemo } from "react";
import type { Lang } from "@/lib/i18n";
import { t } from "@/lib/i18n";

type EquityPoint = { timestamp?: string; equity: number } | number;

interface EquityCurveChartProps {
  equityCurve: EquityPoint[];
  lang: Lang;
  width?: number;
  height?: number;
  showDrawdownOverlay?: boolean;
}

/**
 * EquityCurveChart — Charts-first equity visualization with optional drawdown overlay.
 * Compliance-safe: observational only, no performance claims.
 */
export function EquityCurveChart({
  equityCurve,
  lang,
  width = 600,
  height = 200,
  showDrawdownOverlay = true,
}: EquityCurveChartProps) {
  const { values, drawdowns, minVal, maxVal, maxDD } = useMemo(() => {
    if (!equityCurve || equityCurve.length < 2) {
      return { values: [], drawdowns: [], minVal: 0, maxVal: 0, maxDD: 0 };
    }

    const vals = equityCurve.map((p) => (typeof p === "number" ? p : p.equity));
    const minV = Math.min(...vals);
    const maxV = Math.max(...vals);

    // Calculate drawdowns
    let runningMax = vals[0];
    const dds: number[] = [];
    for (const val of vals) {
      if (val > runningMax) runningMax = val;
      const dd = runningMax > 0 ? ((runningMax - val) / runningMax) * 100 : 0;
      dds.push(dd);
    }

    return {
      values: vals,
      drawdowns: dds,
      minVal: minV,
      maxVal: maxV,
      maxDD: Math.max(...dds, 0),
    };
  }, [equityCurve]);

  if (values.length < 2) {
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
        {t(lang, "backtests.run.noEquityCurve")}
      </div>
    );
  }

  const padding = { top: 20, right: 16, bottom: 28, left: 56 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const valueRange = maxVal - minVal || 1;

  // Generate equity line path
  const equityPoints = values.map((val, i) => {
    const x = padding.left + (i / (values.length - 1)) * chartWidth;
    const y = padding.top + chartHeight - ((val - minVal) / valueRange) * chartHeight;
    return { x, y };
  });
  const equityPath = `M${equityPoints.map((p) => `${p.x},${p.y}`).join(" L")}`;

  // Generate drawdown overlay path (inverted, from top)
  const ddScale = maxDD > 0 ? maxDD : 10;
  const ddHeight = chartHeight * 0.3; // 30% of chart for DD overlay
  const ddPoints = drawdowns.map((dd, i) => {
    const x = padding.left + (i / (drawdowns.length - 1)) * chartWidth;
    const y = padding.top + (dd / ddScale) * ddHeight;
    return { x, y };
  });
  const ddAreaPath = `M${padding.left},${padding.top} L${ddPoints
    .map((p) => `${p.x},${p.y}`)
    .join(" L")} L${padding.left + chartWidth},${padding.top} Z`;

  // Y-axis labels for equity
  const yLabels = [minVal, (minVal + maxVal) / 2, maxVal].map((val) => ({
    val,
    y: padding.top + chartHeight - ((val - minVal) / valueRange) * chartHeight,
  }));

  return (
    <div style={{ width: "100%" }}>
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
              fontSize="9"
            >
              {label.val.toFixed(0)}
            </text>
          </g>
        ))}

        {/* Drawdown overlay (semi-transparent red area from top) */}
        {showDrawdownOverlay && (
          <path d={ddAreaPath} fill="rgba(239, 68, 68, 0.15)" />
        )}

        {/* Equity line */}
        <path
          d={equityPath}
          fill="none"
          stroke="#3b82f6"
          strokeWidth={2}
          strokeLinejoin="round"
        />

        {/* Start/end markers */}
        <circle
          cx={equityPoints[0].x}
          cy={equityPoints[0].y}
          r={3}
          fill="#3b82f6"
        />
        <circle
          cx={equityPoints[equityPoints.length - 1].x}
          cy={equityPoints[equityPoints.length - 1].y}
          r={3}
          fill={values[values.length - 1] >= values[0] ? "#22c55e" : "#ef4444"}
        />

        {/* X-axis label */}
        <text
          x={padding.left + chartWidth / 2}
          y={height - 6}
          textAnchor="middle"
          fill="#64748b"
          fontSize="9"
        >
          {t(lang, "backtests.run.dataWindow")}
        </text>

        {/* Y-axis label */}
        <text
          x={10}
          y={padding.top + chartHeight / 2}
          textAnchor="middle"
          fill="#64748b"
          fontSize="9"
          transform={`rotate(-90, 10, ${padding.top + chartHeight / 2})`}
        >
          {t(lang, "backtests.run.equityLabel")}
        </text>
      </svg>

      {/* Legend */}
      <div
        style={{
          display: "flex",
          justifyContent: "center",
          gap: "1.5rem",
          fontSize: "0.72rem",
          color: "#9ca3af",
          marginTop: "0.25rem",
        }}
      >
        <span style={{ display: "flex", alignItems: "center", gap: "0.3rem" }}>
          <span
            style={{
              width: 12,
              height: 2,
              background: "#3b82f6",
              borderRadius: 1,
            }}
          />
          {t(lang, "backtests.run.equityCurveLegend")}
        </span>
        {showDrawdownOverlay && (
          <span style={{ display: "flex", alignItems: "center", gap: "0.3rem" }}>
            <span
              style={{
                width: 12,
                height: 8,
                background: "rgba(239, 68, 68, 0.3)",
                borderRadius: 1,
              }}
            />
            {t(lang, "backtests.run.drawdownOverlayLegend")}
          </span>
        )}
      </div>
    </div>
  );
}
