"use client";

import React from "react";
import { StatusBadge } from "@/components/broker/StatusBadge";
import { EmptyState } from "@/components/broker/States";
import type { ValidationAttempt } from "@/types/broker";
import { formatWhen, healthStatusView, reasonMessage } from "@/lib/broker-status";

/** WP4.2 — the validation-attempt history (secret-safe fields only). Customer-safe reason wording;
 * never the raw reason code or operator diagnostics. */
const th: React.CSSProperties = {
  textAlign: "left", padding: "0.5rem 0.6rem", fontSize: "0.75rem", color: "#8fa0b7",
  borderBottom: "1px solid rgba(255,255,255,0.1)", whiteSpace: "nowrap",
};
const td: React.CSSProperties = {
  padding: "0.55rem 0.6rem", fontSize: "0.85rem", color: "#cbd5f5",
  borderBottom: "1px solid rgba(255,255,255,0.05)", verticalAlign: "top",
};

export const ValidationHistoryTable: React.FC<{ attempts: ValidationAttempt[] }> = ({ attempts }) => {
  if (!attempts || attempts.length === 0) {
    return <EmptyState title="No validation attempts yet" body="Run a connection test to validate this account." />;
  }
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 560 }}>
        <caption style={{ textAlign: "left", color: "#8fa0b7", fontSize: "0.8rem", padding: "0 0 0.5rem" }}>
          Validation history
        </caption>
        <thead>
          <tr>
            <th scope="col" style={th}>When</th>
            <th scope="col" style={th}>Result</th>
            <th scope="col" style={th}>Detail</th>
            <th scope="col" style={th}>Type</th>
            <th scope="col" style={th}>Server</th>
          </tr>
        </thead>
        <tbody>
          {attempts.map((a) => (
            <tr key={a.id}>
              <td style={td}>{formatWhen(a.created_at) || "—"}</td>
              <td style={td}><StatusBadge view={healthStatusView(a.status)} /></td>
              <td style={{ ...td, color: "#9fb0c8" }}>{reasonMessage(a.reason_code) || "—"}</td>
              <td style={td}>{a.is_demo === true ? "Demo" : a.is_demo === false ? "Live" : "—"}</td>
              <td style={td}>{a.server || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};
