"use client";

import React from "react";
import { EmptyState } from "@/components/broker/States";
import { CategoryBadge, ResolutionBadge, SeverityBadge } from "@/components/operations/OpsBadges";
import { formatWhen } from "@/lib/broker-status";
import type { OperationalEvent } from "@/types/operations";

/** WP5.3 — the read-only operational timeline (newest first). Row click opens the event detail. Renders
 * only mapped views + customer-safe summary/reason; never a raw diagnostic. */
const th: React.CSSProperties = {
  textAlign: "left", padding: "0.5rem 0.6rem", fontSize: "0.75rem", color: "#8fa0b7",
  borderBottom: "1px solid rgba(255,255,255,0.1)", whiteSpace: "nowrap",
};
const td: React.CSSProperties = {
  padding: "0.55rem 0.6rem", fontSize: "0.85rem", color: "#cbd5f5",
  borderBottom: "1px solid rgba(255,255,255,0.05)", verticalAlign: "top",
};

export const OpsTimelineTable: React.FC<{
  events: OperationalEvent[];
  onSelect?: (e: OperationalEvent) => void;
}> = ({ events, onSelect }) => {
  if (!events || events.length === 0) {
    return <EmptyState title="No events" body="No operational events match the current filters." />;
  }
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 720 }}>
        <caption style={{ textAlign: "left", color: "#8fa0b7", fontSize: "0.8rem", padding: "0 0 0.5rem" }}>
          Operational timeline (newest first)
        </caption>
        <thead>
          <tr>
            <th scope="col" style={th}>Time</th>
            <th scope="col" style={th}>Severity</th>
            <th scope="col" style={th}>Category</th>
            <th scope="col" style={th}>Summary</th>
            <th scope="col" style={th}>Reason</th>
            <th scope="col" style={th}>Status</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr
              key={e.id}
              onClick={onSelect ? () => onSelect(e) : undefined}
              tabIndex={onSelect ? 0 : undefined}
              onKeyDown={onSelect ? (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); onSelect(e); } } : undefined}
              aria-label={onSelect ? `View event: ${e.summary || e.event_type}` : undefined}
              style={{ cursor: onSelect ? "pointer" : "default" }}
            >
              <td style={td}>{formatWhen(e.timestamp) || "—"}</td>
              <td style={td}><SeverityBadge severity={e.severity} /></td>
              <td style={td}><CategoryBadge category={e.category} /></td>
              <td style={{ ...td, color: "#e9f4ff" }}>{e.summary || "—"}</td>
              <td style={{ ...td, color: "#9fb0c8" }}>{e.reason_code || "—"}</td>
              <td style={td}><ResolutionBadge resolved={e.resolved} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};
