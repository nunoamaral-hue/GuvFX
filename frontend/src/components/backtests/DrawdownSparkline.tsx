"use client";

import React, { useMemo } from "react";

type EquityPoint = { timestamp?: string; equity: number } | number;

interface DrawdownSparklineProps {
  equityCurve: EquityPoint[];
  width?: number;
  height?: number;
  strokeColor?: string;
  fillColor?: string;
}

/**
 * DrawdownSparkline — Mini SVG visualization of drawdown over time.
 * Charts-first approach: shows shape of underwater periods without numeric emphasis.
 * Compliance-safe: observational only, no performance claims.
 */
export function DrawdownSparkline({
  equityCurve,
  width = 120,
  height = 32,
  strokeColor = "#ef4444",
  fillColor = "rgba(239, 68, 68, 0.15)",
}: DrawdownSparklineProps) {
  const drawdownData = useMemo(() => {
    if (!equityCurve || equityCurve.length < 2) return [];

    // Normalize equity values
    const values = equityCurve.map((p) =>
      typeof p === "number" ? p : p.equity
    );

    // Calculate running max and drawdown percentage
    let runningMax = values[0];
    const drawdowns: number[] = [];

    for (const val of values) {
      if (val > runningMax) runningMax = val;
      const dd = runningMax > 0 ? ((runningMax - val) / runningMax) * 100 : 0;
      drawdowns.push(dd);
    }

    return drawdowns;
  }, [equityCurve]);

  if (drawdownData.length < 2) {
    return (
      <div
        style={{
          width,
          height,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: "0.7rem",
          color: "#64748b",
        }}
      >
        —
      </div>
    );
  }

  // Scale to SVG coordinates
  const maxDD = Math.max(...drawdownData, 1); // At least 1% to avoid division by zero
  const padding = 2;
  const chartWidth = width - padding * 2;
  const chartHeight = height - padding * 2;

  const points = drawdownData.map((dd, i) => {
    const x = padding + (i / (drawdownData.length - 1)) * chartWidth;
    const y = padding + (dd / maxDD) * chartHeight; // Drawdown goes down from top
    return `${x},${y}`;
  });

  const linePath = `M${points.join(" L")}`;
  const areaPath = `M${padding},${padding} L${points.join(" L")} L${
    padding + chartWidth
  },${padding} Z`;

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      style={{ display: "block" }}
      aria-label="Drawdown sparkline"
    >
      {/* Fill area */}
      <path d={areaPath} fill={fillColor} />
      {/* Line */}
      <path d={linePath} fill="none" stroke={strokeColor} strokeWidth={1.5} />
      {/* Zero line at top */}
      <line
        x1={padding}
        y1={padding}
        x2={width - padding}
        y2={padding}
        stroke="#334155"
        strokeWidth={0.5}
        strokeDasharray="2,2"
      />
    </svg>
  );
}
